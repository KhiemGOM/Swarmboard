# Swarmboard

**Run several coding agents in one repo without them overwriting each other.**

Claude Code in one terminal, Codex in another, three more chats in Claude Desktop,
all in the same working directory. Without coordination they edit the same file at
once, commit each other's half-finished work, fight over the dev server port and
quietly redo jobs another agent already finished.

Swarmboard is a small local hub that gives those agents what a human team takes for
granted: who is editing what, who is doing which job, and a way to message each
other. It runs on your machine, stores everything in one SQLite file, needs nothing
beyond Python 3.10+, and talks to agents over MCP.

```text
you:      /board  fix the login redirect
claude:   claim src/auth/login.ts → accepted
codex:    claim src/auth/login.ts → refused: held by "Title Fight" (fixing login redirect)
codex:    request_relax src/auth/login.ts "I only need to add one translation key"
claude:   relax src/auth/login.ts → both now submit patches against one base;
          the board merges them and tells each agent the result
```

## What you get

- **File and folder claims.** An agent claims the paths it is about to edit, and the
  hub grants them atomically. Claude Code and Codex hooks block edits to files the
  agent hasn't claimed, so "check before you edit" isn't left to the model's memory.
- **A patch queue for hot files.** Translation tables, shared CSS, the changelog:
  several agents prepare diffs against one frozen base, and the board accepts
  compatible patches and publishes the combined file in one atomic write.
- **A job board.** Work is posted, claimed with a lease and finished with a commit
  hash and test evidence. If an agent crashes or runs out of quota, its lease
  expires and the job reopens for someone else, with its progress saved.
- **Commit custody.** Releasing a file means either "here's the commit" or "this is
  HELD, still uncommitted". Unfinished work never silently disappears from view.
- **Inboxes that wake agents up.** Direct messages between agents, delivered into
  their context by hooks. Idle Claude chats are woken by an async Stop hook, and idle
  Codex desktop chats by a queued message.
- **Resource locks.** `git:index`, `port:3000`, `db:test`, `build:dist`: anything
  agents shouldn't use at the same time.
- **Handoffs and takeovers.** Retire a stuck agent from the terminal (no model quota
  needed), pass its locks, jobs and inbox to a successor, or recover your own
  identity after a reconnect.
- **Opt-in per chat.** Nothing happens until you type `/board` in a chat. Every other
  chat and repository behaves exactly as before.
- **Per-chat identity.** Each chat gets its own agent ID and a display name it picks
  itself, even when Claude Desktop shares one MCP process across all its chats. A
  linked Claude chat's title follows the job it's working on.

Files outside the workspace are never coordinated: agents just edit them.

## Quickstart

You need Python 3.10+ and Claude Code and/or Codex.

**1. Get the code.**

```sh
git clone https://github.com/KhiemGOM/Swarmboard swarmboard
```

**2. Register the MCP server.** Use absolute paths. The hub starts itself on the
first call.

```sh
claude mcp add --scope user --transport stdio board -- /abs/path/to/python /abs/path/to/swarmboard/run_board.py mcp --name claude-code --autostart
codex mcp add board -- /abs/path/to/python /abs/path/to/swarmboard/run_board.py mcp --name codex --autostart
```

`python install_local.py --workspace /abs/path/to/project` does this for both CLIs
at once, backs up your configs and keeps your other servers.

**3. Install the hooks.** They enforce claims, deliver the inbox and wake idle chats.

```sh
python install_hooks.py
```

Restart the harness afterwards. Codex asks you to trust new hooks through `/hooks`.

**4. Install the skill.** Copy `examples/board-skill/` to `~/.claude/skills/board/`
(and `~/.codex/skills/board/` for Codex) so `/board` and `$board` work as commands.

**5. Opt in.** In any chat inside your project, type:

```text
/board
```

The first call creates `.board/` in the project root, registers the chat as an agent
and shows it the rules. Do the same in every chat that should join. Then work
normally: agents claim, post jobs, message each other and release with commit
evidence on their own.

## Everyday commands

Type these in a chat (Codex uses `$board` instead of `/board`):

| Command | What it does |
| --- | --- |
| `/board` | Join, or refresh status, notices and inbox |
| `/board release` | Review and free idle locks |
| `/board queue` | Prepare a shared-file patch with other agents |
| `/board request-relax FILE` | Ask a claim holder to open a file to patches |
| `/board rename [TITLE]` | Retitle this chat now |
| `/board retire [ID]` | End an agent's session and free its locks |
| `/board version` | Hub and bridge versions and capabilities |
| `/board reset` | Wipe this project's board (files are untouched) |

From a terminal, without spending model quota:

```sh
./board.ps1 status          # or: python boardctl.py status
./board.ps1 retire AGENT_ID -Reason "Out of quota"
./board.ps1 cleanup         # reconcile files deleted outside the board
```

## How it works

```text
 Claude Code ─┐                         ┌─ .board/hub.sqlite
 Codex ───────┼─ stdio MCP bridge ── HTTP ── hub daemon (127.0.0.1)
 Desktop chat ┘        ▲                └─ one hub per workspace
                       │
          PreToolUse / PostToolUse / Stop hooks
          (route each call to its chat, guard edits, deliver inbox)
```

One daemon per workspace owns the SQLite state. Every agent connects through a thin
stdio MCP bridge. Hooks stamp each board call with the chat it came from, so one
bridge can safely serve many chats. Nothing leaves your machine.

## What it isn't

- **Not a sandbox.** Hooks guard the edit tools Claude Code and Codex expose, but a
  shell script can still write anywhere. Coordination is cooperative.
- **Not an orchestrator.** Swarmboard doesn't plan or split work, and it doesn't
  spawn agents. You (or your agents) decide the work; the board keeps them from
  colliding.
- **Not multi-machine.** It's built for agents sharing one checkout on one computer.

## Learn more

- [Full reference](docs/REFERENCE.md): every tool, hook, flag and edge case
- [Per-chat routing design](docs/routing-redesign.md)
- [The `/board` skill](examples/board-skill/SKILL.md) agents follow
- Plain agent instructions: [examples/agent-instructions.md](examples/agent-instructions.md)

Run the tests with `python -m unittest discover -s tests`.
