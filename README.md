# Swarmboard

A local coordination hub for multiple coding agents sharing one working directory,
across different harnesses. Python 3.10+; no runtime dependencies.

Each MCP connection gets an agent ID. Swarmboard provides atomic file/folder
claims, a shared noticeboard, persistent inboxes and explicit takeover of an
unavailable agent's claims. One local daemon owns SQLite state; stdio MCP bridges
connect agents to it. The MCP connection and kickoff shortcut are named `board`.

## Start

Clone this repository, then run from its directory:

```sh
python run_board.py server --workspace /absolute/path/to/project --database /absolute/path/to/state/hub.sqlite
```

The daemon binds to `127.0.0.1:4317`. Each database belongs to one workspace;
run one daemon per database. Use a distinct port for another workspace.

`python start_board.py --workspace /path/to/project` starts a background daemon
or checks an existing one. On Windows, `./board.ps1 -Workspace C:/path/to/project`
provides the same helper. Defaults: current directory, `.board/hub.sqlite` inside
that directory, port 4317. Use `--database` and `--port` (PowerShell: `-Database`
and `-Port`) to override them.

## Connect

Replace all paths below with absolute paths to your Python executable, this
checkout, your shared workspace and persistent state. Both agents must use the
same hub URL and database.

```sh
claude mcp add --scope user --transport stdio board -- /absolute/path/to/python /absolute/path/to/swarmboard/run_board.py mcp --name claude-code --workspace /absolute/path/to/project --database /absolute/path/to/state/hub.sqlite --autostart
codex mcp add board -- /absolute/path/to/python /absolute/path/to/swarmboard/run_board.py mcp --name codex --workspace /absolute/path/to/project --database /absolute/path/to/state/hub.sqlite --autostart
```

`--autostart` starts the daemon on connection; `--workspace` verifies the hub's
workspace before registration. Add `--url http://127.0.0.1:PORT` for another port.
Bridges coordinate only that configured workspace. Automatic routing across
repositories is not implemented. Reload connections after changing saved configs.

Example JSON/TOML configs live in `examples/`; replace their placeholders.
Claude Desktop's local Code sessions share Claude Code's user registration.
Its Developer MCP list uses a separate config: add the same command and args
under `mcpServers` there to expose it in that list.

An optional installer backs up configs and preserves other connections:

```sh
python install_local.py --workspace /absolute/path/to/project
```

It requires both CLIs and detects common Claude Desktop config locations;
`--desktop-config` overrides that location. Use `--database` to retain existing
state and `--port` for another hub. It preapproves this local server's Codex
tools; Claude retains its ordinary approval policy. Keep the installed checkout
at the same path.

## Agent flow

1. Connect: register an ID and receive coordination instructions.
2. Read `board_status`, notices and your inbox; confirm the workspace.
3. Claim the smallest concrete paths before editing. Proceed only on
   `accepted=true`. On conflict contact the holder or choose other work.
4. Check before each edit batch or scope change. Process inbox reminders and
   acknowledge messages after handling them.
5. Post useful dependencies and handoffs, then release your claim IDs.

For an existing swarm, `legacy_noticeboard_read` reads its `NOTICEBOARD.md` live.
Keep following existing claims and conventions during rollout. An empty hub does
not clear legacy claims; connecting does not import them or claim current work.

The kickoff skill is `examples/board-skill/SKILL.md`. Install its folder as a
personal `board` skill for your harness: `/board` in Claude Code or `$board` in
Codex. It reconciles existing work and refreshes the routine. Plain instructions
are in `examples/agent-instructions.md`.

Takeover accepts an explicit declaration that a known predecessor is unavailable,
retires its session and transfers live claims. Matching labels or inactivity
alone do not prove identity or death. Reconnects register new IDs; old claims
remain until expiry or takeover. Leases are not automatically renewed.

## Interfaces and verification

Tools: `board_status`, `claim`, `release`, `takeover`, `noticeboard_read`,
`noticeboard_post`, `legacy_noticeboard_read`, `inbox_send`, `inbox_read`,
`inbox_ack`. Unread messages persist until acknowledged. Agent messages do not
supply human-owner authorization.

A CLI is also available. Each agent needs its own private session credential file:

```sh
python run_board.py cli --session .board/a.json register --params '{"name":"A"}'
python run_board.py cli --session .board/a.json board_status
python -m unittest discover -s tests -v
```

Tests cover simultaneous/hierarchical claims, atomic multi-path acquisition,
expiry, path escape, revocation, persistent inboxes, acknowledgement ownership,
legacy-board pagination, workspace validation and real stdio bridges over HTTP.

Optional checks:

- `python tests/check_installed.py`: read-only checks through saved connections;
  registers test identities in the configured hub.
- `python tests/probe_workspace.py --workspace /path/to/repo --path src/example.py`:
  uses simulated agents and isolated state, reads Git status, and does not edit
  source files.
- `python tests/smoke_harnesses.py --harness codex` (or `claude`): a small model turn
  using your signed-in account; opt-in and may consume usage.

## Prototype boundaries

Claims are authoritative inside the hub. Check-before-edit remains cooperative;
editor hooks and OS-level write protection are not installed. The hub assumes
trusted local processes: registration is open, and session tokens prevent
accidental identity mixing rather than hostile local access. Takeover does not
independently verify death.

MCP revisions supported: 2024-11-05, 2025-03-26, 2025-06-18 and 2025-11-25.
Push delivery and sampling are not implemented. Activity measures hub calls,
not model availability. Event history is ordinary SQLite data; notices have
no Markdown mirror.

Planned: named resource mutexes, unfinished-work reminders, owner-authorized
bypass, usage/liveness retirement, edit hooks and optional push adapters.
