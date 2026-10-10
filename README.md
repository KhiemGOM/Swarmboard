# Swarmboard

A local coordination hub for multiple coding agents sharing one working directory,
across different harnesses. Python 3.10+; no runtime dependencies.

Each MCP connection gets an agent ID. Swarmboard provides atomic file/folder
claims, named resource mutexes, durable commit custody, a job board, a shared noticeboard,
persistent inboxes and explicit takeover of an
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
5. Post useful dependencies and handoffs. File release requires `commit_hash`
   or `held_reason`; HELD frees the lock but keeps custody and reminders.
   Use `custody_resolve` to record a later commit. Expiry also keeps custody.

Shared resources use `mutex_claim` with agreed IDs such as `db:core_test`,
`port:3101`, `build:web/dist`, `git:index` or `misc:fixture`. Resource conflicts
are refused by the hub, but agents cooperate before using actual resources.
Hold `git:index` throughout staging, committing and index synchronization.
Resource-only release needs a completion `reason`, not a Git commit. IDs are
case-insensitive, exact matches and workspace-scoped; aliases and resources
shared across separate workspaces are not automatically coordinated.

Accepting overlapping file work also accepts its unfinished custody. Takeover
transfers outstanding custody as well as live claims. Commit hashes are recorded
completion evidence, not independent proof of tests, push, or commit contents.

The SQLite noticeboard is the single board; there is no live Markdown adapter.
For a one-time move from an old board:

```sh
python migrate_noticeboard.py --workspace /path/to/project --database /path/to/state/hub.sqlite --source /path/to/project/old-board.md --archive /path/to/private/archive --lines 30 --retire-source
```

The importer archives the full file, imports its last 30 lines as historical
notices, verifies insertion and then retires the source. Repeating an identical
snapshot does not duplicate notices. Historical labels are not authenticated
agent IDs or live locks. Update repository instructions and reconnect existing
sessions before continuing under the new board rules.

The kickoff skill is `examples/board-skill/SKILL.md`. Install its folder as a
personal `board` skill for your harness: `/board` in Claude Code or `$board` in
Codex. It reconciles existing work and refreshes the routine. Plain instructions
are in `examples/agent-instructions.md`.

Takeover accepts an explicit declaration that a known predecessor is unavailable,
retires its session and transfers live claims. Matching labels or inactivity
alone do not prove identity or death. Reconnects register new IDs; old claims
remain until expiry or takeover. Leases are not automatically renewed.

## Job board

Post work through MCP `job_post` or the CLI. Every job has a title, description
and acceptance criteria. Jobs share information about authorized work; they do
not grant permission to expand the owner's scope. A worker reserves an OPEN job
with `job_claim`, then obtains separate file/resource locks. One worker wins
concurrent claims. Save the returned `lease_token`: updates, handoff and completion
require this token, preventing an expired worker from finishing a reassigned job.

The default job lease is 30 minutes (maximum two hours). `job_update` saves progress
and renews it; no background renewal silently holds work indefinitely. `job_yield`
returns it to OPEN with a handoff. Finishing and yielding do not release file or
resource locks; handle their custody separately.

`job_finish` requires a result summary and check records. Code jobs also require
`commit_hash`; noncode jobs can deliver a summary without a commit. Each check
contains `command`, `result` (`passed` or `not_applicable`) and `evidence` (results,
log/artifact references, or why a check does not apply). Failed checks cannot
finish a job. Records persist alongside the reported commit; Swarmboard does not
execute arbitrary commands or independently certify the commit or test evidence.

Finish when the original acceptance criteria and repository commit policy are
met. There is no need to wait for user silence. A topic change does not imply
completion or authorize a commit. Additional work can be posted separately with
`parent_job_id` linking it to the original job.

Clean MCP stdin closure or a bound harness `SessionEnd` records disconnection and
reopens unfinished jobs. Lease expiry recovers crashed or silent workers on the
next successful hub call. Saved progress, previous owner, event history and file
custody remain. Old sessions with unfinished custody but no job get a recovery
review job after disconnection, file-lease expiry or explicit takeover. These
jobs require inspecting the prior scope and changes, not blindly committing them.
File locks still apply, and explicit takeover remains separate. A disconnected
session can return, but must reclaim jobs with fresh tokens. Last hub contact is
shown in status; it cannot distinguish thinking, quota exhaustion or a dead model.

Quick posting with your own CLI session (separate from an MCP agent):

```sh
python run_board.py cli --session .board/owner.json register --params '{"name":"owner"}'
python run_board.py cli --session .board/owner.json job_post --title "Fix login" --description "Repair the reported login failure" --acceptance "Login regression passes"
python run_board.py cli --session .board/owner.json job_list
```

## Inspect ownership without claiming

`board_status` supports focused MCP queries:

```json
{"target_agent_id": "self", "include_rules": false}
{"target_agent_id": "EXACT_AGENT_ID", "include_rules": false}
{"paths": ["src/example.py"], "include_rules": false}
{"resources": ["git:index", "db:core_test"], "include_rules": false}
```

The agent filter applies to agents, active claims, custody and owned job counts.
File queries match overlapping parent/child file or folder locks and custody;
resource queries match exact IDs. Entity queries return only related holders,
with `matches` showing each query's claim IDs and custody paths, even when empty.
Combine paths/resources with an agent filter to narrow further. These queries
do not acquire or renew mutexes or transfer custody. An empty result is a snapshot;
obtain an accepted claim before writing. HELD custody remains visible after a
mutex is released or its owner retires. Paths must stay inside the workspace.

Omit filters for the existing workspace-wide view. `include_rules=false` skips
repeated coordination rules. Workspace, caller identity and caller inbox/job
reminders remain available regardless of whose entries are requested.
`job_counts_scope` distinguishes caller/target job counts from workspace counts;
path/resource queries do not filter jobs. Hooks automatically request self-only
status without repeated rules. Existing MCP connections need reloading to discover
the new arguments after updating the hub.

## Optional harness hooks

```sh
python install_hooks.py --workspace /absolute/path/to/project --database /absolute/path/to/state/hub.sqlite
```

This backs up and merges hooks into `~/.codex/hooks.json` and Claude Code's
`~/.claude/settings.json`, preserving other handlers and settings. Restart/reload
the harness and call `board_status` first. Its `PostToolUse` hook binds the native
session to the existing MCP identity, storing credentials privately next to the
database. No extra agent is registered. Use the exact database and URL configured
for MCP; add `--url` for a different port. Hooks ignore other workspaces.
Codex requires review/trust of new hook definitions through `/hooks`; installed
definitions are skipped until trusted. Its hooks feature must also be enabled.
On Windows, Codex command hooks use PowerShell invocation and literal argument
quoting (including paths with spaces). Claude uses its direct executable/args
form. These are different launch formats; a successful direct Python run alone
does not verify a Codex shell hook.

`SessionStart` reinforces the routine. `PreToolUse` checks supported file edits
(Claude Edit/Write/MultiEdit/NotebookEdit and Codex apply_patch), including patch
move destinations. Missing or expired claims are denied. Recognized Git index
operations require `git:index`. Status/notices are refreshed before checks;
inbox, custody and job reminders are included. `Stop` warns about unfinished work
without forcing commits or blocking the user. `SessionEnd` reports disconnection.

Shell writes and shared resources remain cooperative: hooks cannot infer every
script's effects, Git aliases or writes by existing processes. Other editing MCPs
and specialized/aggregate tool paths are not covered by the file guard. Binding
requires a native `mcp__board__*` tool result; if a harness wraps calls in an
aggregate tool, use its native board tool or coordinate explicitly. Codex hook
payloads use the parent's session ID for subagents; this adapter does not establish
separate subagent identities. Desktop availability depends on its embedded harness.
See [Codex hooks](https://learn.chatgpt.com/docs/hooks) and
[Claude Code hooks](https://code.claude.com/docs/en/hooks).

## Interfaces and verification

Tools: `board_status`, `claim`, `release`, `takeover`, `noticeboard_read`,
`mutex_claim`, `custody_resolve`, `noticeboard_post`, `inbox_send`, `inbox_read`,
`inbox_ack`. Unread messages persist until acknowledged. Agent messages do not
supply human-owner authorization.

The noticeboard holds durable general workspace information. Use inboxes for
direct requests/replies, blockers and time-sensitive availability or handoffs
when another agent is waiting. If an ASAP answer is expected, send the authorized
reply there promptly; a notice does not deliver it. If peer-contact authorization
is unclear, ask whether the user wants to alert that agent once; continue an
authorized exchange without repeated permission requests. Hooks preview unread
messages during supported tool activity (including board calls) and at Stop,
without acknowledging them or guaranteeing idle-session wakeup.

Install `examples/board-skill/` as the personal `board` skill for each harness.
Use `/board mutex` in Claude Code or `$board mutex` in Codex to review the agent's own locks.
It releases idle resource locks with a reason and idle file locks with a commit
hash or HELD handoff. It preserves active process locks and unfinished custody;
it never takes another agent's locks based on age. Stop hooks also prompt this
review when locks remain. This is an agent-guided review, not automatic eviction.

### Retire an agent, even with no model quota

`/board retire` (Codex: `$board retire`) retires the current agent. Add an exact
agent ID to retire another agent. This is distinct from takeover: all mutexes are
released and unfinished jobs return to OPEN for other workers to claim, rather
than becoming the caller's work. Unfinished file custody becomes HELD and follows
the next accepted file claim. Completed jobs/commits, saved progress and inboxes
remain intact. The retired ID immediately loses hub access and must stop work.
Repeated retirement is harmless; unknown IDs are refused. Processes are not killed.

Without a model or running daemon, use the terminal from this checkout:

```powershell
./board.ps1 status
./board.ps1 retire AGENT_ID -Reason 'Quota exhausted; owner releasing this agent'
./board.ps1 mutex
./board.ps1 jobs
```

Cross-platform equivalent: `python boardctl.py retire AGENT_ID --reason "Quota exhausted"`.
Terminal commands cannot infer a chat's current agent; supply its exact ID from
status. They execute locally against SQLite, without API calls, model quota,
registration or another agent's session credentials. All human commands live
under board; there is no standalone `/mutex` skill.

The installer saves private defaults in `.board/operator.json` for the configured
workspace/database/port. Pass `-Workspace`, `-Database`, `-Port` (Python:
`--workspace`, `--database`, `--port`) to override them. Explicit workspace changes
do not reuse another workspace's saved database. A missing/mismatched database
is refused before retirement; no empty board is silently created. The local
operator is trusted, just like other local processes with access to this database.

Job tools: `job_post`, `job_list`, `job_claim`, `job_update`, `job_yield`,
`job_finish`. CLI also offers `heartbeat` and `disconnect`.

A CLI is also available. Each agent needs its own private session credential file:

```sh
python run_board.py cli --session .board/a.json register --params '{"name":"A"}'
python run_board.py cli --session .board/a.json board_status
python -m unittest discover -s tests -v
```

Tests cover simultaneous/hierarchical claims, atomic multi-path acquisition,
expiry, path escape, revocation, persistent inboxes, acknowledgement ownership,
resource mutexes, durable custody, idempotent historical import, old-schema
upgrades, workspace validation and real stdio bridges over HTTP.

Optional checks:

- `python tests/check_installed.py`: read-only checks through saved connections;
  registers test identities in the configured hub.
- `python tests/check_hooks_installed.py`: invokes saved hook commands with
  native-shaped payloads through PowerShell on Windows (a shell on Unix) for
  Codex and direct argv for Claude, confirming denied unclaimed edits/Git operations;
  does not exercise native harness dispatch or spend model usage.
- `python tests/probe_workspace.py --workspace /path/to/repo --path src/example.py`:
  uses simulated agents and isolated state, reads Git status, and does not edit
  source files.
- `python tests/smoke_harnesses.py --harness codex` (or `claude`): a small model turn
  using your signed-in account; opt-in and may consume usage.

## Prototype boundaries

Claims are authoritative inside the hub. Check-before-edit remains cooperative;
optional command hooks add guardrails, not OS-level write protection. The hub assumes
trusted local processes: registration is open, and session tokens prevent
accidental identity mixing rather than hostile local access. Takeover does not
independently verify death.

MCP revisions supported: 2024-11-05, 2025-03-26, 2025-06-18 and 2025-11-25.
Push delivery and sampling are not implemented. Activity measures hub calls,
not model availability. Event history is ordinary SQLite data; notices have
no Markdown mirror.

Planned: owner-authorized bypass, quota detection, configurable evidence validation
and optional push adapters.
