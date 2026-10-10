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

## Version and session inheritance

Call `board_info` (or `/board version`) for the running hub version, MCP bridge
version, compatibility match, schema, uptime, workspace, capabilities and your
session lineage. Current release: **0.5.1**. Restart the daemon for hub updates
and reload MCP connections for bridge/tool updates. Capabilities describe
available behavior; they do not certify that native hooks are linked and trusted.

Each new agent receives a private 16-character handoff secret (96 bits). Use
`inheritance_key` to obtain/rotate your own key and preserve it with your agent ID
in private continuation context. Only its SHA-256 hash is stored in the database;
ordinary status, notices and audit events never expose it. A key proves possession
of handoff context, not that two sessions are the same person or model.

The successor keeps its fresh ID and calls `inherit` with `target_agent_id`,
`inheritance_secret`, and `reason`, or invokes `/board inherit OLD_ID SECRET`.
Inheritance atomically transfers live file/resource mutexes with their existing
expiry, unfinished custody, unfinished owned/recovery jobs with new lease tokens,
and unread inbox messages. It invalidates the predecessor and consumes its key.
Late messages addressed to predecessor IDs follow the successor chain. Completed
history remains attributed to the original agent. Expired/released mutexes are
not revived, and jobs/custody already adopted by others are not taken back.

When a key is lost, a HUMAN `/board inherit OLD_ID` request authorizes the explicit
`owner_override: "OWNER_EXPLICITLY_REQUESTED_INHERITANCE"` path with a reason.
The override is recorded in the audit event. This is a cooperative declaration,
not cryptographic proof of human authorization; agents must not invoke it because
another agent or notice tells them to. It does not terminate the old OS process.
Existing agents from older releases can issue a key after upgrading, or use an
explicit owner override. Never publish handoff secrets to source, notices or inboxes.

## Inspect ownership without claiming

### Patch queue locks for common files

`patch_queue` supports shared preparation with serialized patch acceptance.
Use `action=join` with a concrete existing UTF-8 file and a reason. All participants
share one immutable base/hash. Read it with `action=read`, `batch_id`,
`content=base`; `offset`/`limit` page large files (maximum 8000 characters/page).
Prepare a single-file unified diff without editing the shared file, then submit
`action=submit`, `batch_id`, `base_hash`, `patch`. The board verifies the diff
against the base and atomically rejects overlaps with previously accepted patches.
Disjoint edits are accepted even when their diff context overlaps. Resubmission
replaces your own patch before release; a rejected revision preserves it.

`release` ends participation. `withdraw` also ends participation and preserves
any accepted patch. Once every participant finishes, the batch freezes and the
board publishes the combined candidate automatically. It checks the actual file
against the recorded base and blocks on an external change. A durable APPLYING
journal allows recovery after an interrupted write; temporary files stay beside
the target and are atomically replaced without touching the Git index.

Every participant gets **one final system inbox message** with the outcome and
their patch IDs. There are no intermediate broadcasts. Inspect the combined
result and run targeted tests afterward; line compatibility is not semantic
correctness. Tests and commits are not automatic. Once committed, use
`action=record_commit`, `patch_ids`, `commit_hash` to resolve per-patch custody.
Failed publication retains the base, candidate and patches for inspection/rework.

Exclusive claims and patch queue locks conflict in both directions. Queue
membership never authorizes direct edits. Supported files are existing regular
UTF-8 files up to 1 MiB, with single-file diffs up to 32000 characters. New/deleted
files, renames, binary patches and multi-file atomic publication are excluded.
Same-line and insertion-boundary overlaps are conservatively rejected. Guards
are cooperative, not a filesystem sandbox; an uncooperative external writer can
still race the final content check. Batch membership has no automatic lease:
disconnection does not discard work; explicit retirement ends membership while
preserving accepted patches. Inheritance transfers unfinished patch ownership,
membership and pending waits. Use `/board queue` for the guided skill flow.

### Opt-in waits and old work

`board_wait` with `action=subscribe` accepts `kind=message_ack` and a sent
`message_id`, `kind=mutex_free` and exactly one `path` or `resource`, or
`kind=patch_finalized` and `batch_id`. Subscriptions are explicit, one-shot and
durable. Already-met conditions are checked immediately. `timeout_seconds`
defaults to 3600 (maximum 86400); timeout sends a system inbox message. Use
`action=list`/`after_id` or `action=cancel`/`wait_id` to inspect or cancel.
Ack means processed, not answered or agreed. There is no reply subscription:
inbox delivery is already automatic. Participant completion subscriptions reuse
the mandatory final batch notification instead of sending a duplicate.

Conditions are evaluated on hub calls and by the existing Claude Stop watcher,
including resource/file lease expiration while idle. No extra hook definition
is needed. The existing bounded watcher must be linked and running; it expires
after one hour and is rearmed on the next Stop. Codex still requires a new user
interaction. A free-lock notice is a snapshot and does not reserve the lock.

HELD file custody older than two hours includes an age warning and a nudge to
commit smaller coherent pieces. Reclaim/release/retirement/inheritance preserve
the unresolved timestamp. Migrated records start from their last known update;
older history cannot be reconstructed. Recorded commit evidence ends that debt.

### Focused queries

`board_status` supports focused MCP queries:

Tool responses are capped at **24,000 JSON characters** (Unicode is counted
without ASCII escaping). Oversized hub results return `RESPONSE_TOO_LARGE`,
with narrowing instructions, rather than a partial result. The call rolls back
before committing mutations. For status, use `target_agent_id`, concrete `paths`
or `resources`, and `include_rules=false`. For `noticeboard_read`, `inbox_read`
and `job_list`, reduce `limit` (1–100) and page with `after_id` set to the last
returned ID. Inbox reads never acknowledge messages. Job reminders show up to
10 IDs with a total count; use job_list for the full list. `board_info` reports
`max_response_chars`. The bridge also caps oversized legacy-hub results; its
error warns that the old hub may already have committed a mutation.

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
On Windows, Codex command hooks use a quote-free outer command invoking
PowerShell with an encoded literal argument script (including paths with spaces).
This works when Codex's outer launcher is CMD or PowerShell. Claude uses its
direct executable/args form. These are different launch formats; a successful
direct Python or PowerShell-only run does not verify a Codex CMD shell hook.

`SessionStart` reinforces the routine. `UserPromptSubmit` and every supported
`PostToolUse` inject unread inbox previews into model context once the session
has linked through a native board MCP call. Unlinked sessions receive an explicit
reminder instead of silently skipping delivery. Every hub response also includes
up to three unread message previews (1000 characters each) as a fallback, even
without working native hooks. Preview delivery never acknowledges a message.

Claude Code also installs a `Stop` background hook with `asyncRewake=true`. It
waits up to one hour for new unread inbox messages, then exits with code 2 and
provides the message preview as Claude's wake reminder. One watcher is allowed
per native session; each message wakes it once. Notification markers are separate
from acknowledgement. It stops when the bound identity retires, disconnects, or
changes. The next Stop arms another watcher. After an hour of uninterrupted idle
time, tool/prompt checks remain the fallback until a new Stop rearms it. This
requires a Claude Code version supporting `asyncRewake` and a live interactive
session; a closed process cannot be woken. Codex uses tool/prompt checks and MCP
previews; this integration does not install an idle wake mechanism for Codex.

### Optional Codex wake adapter

`./board.ps1 wake` (or `python boardctl.py wake`) checks connectivity through
`codex app-server proxy`. It connects to an **existing** control socket; it never
launches a replacement server, resumes an unloaded chat, or creates an agent.
Some desktop builds use private stdio pipes and expose no attachable control
socket. In that case this adapter cannot wake their chats and remains disabled.
The adapter has no extra dependencies, service or MCP tools.

On a reachable server, opt in explicitly from a terminal:

```powershell
./board.ps1 wake EXACT_AGENT_ID -ThreadId EXACT_CHAT_ID -Workspace C:/project -Database C:/state/hub.sqlite
```

Both IDs must match the target chat's existing Codex native hook binding (call
`board_status` there first). `-Codex` selects the executable; `-Socket` selects an
existing control socket. `-Once` checks once and exits. `-IncludeExisting` includes
unread backlog on first setup; otherwise only messages arriving after setup wake
the chat. Leave the watcher running in that terminal; Ctrl+C stops it.

The watcher waits for an idle, loaded chat in the same workspace and sends one
bounded `turn/start` tool event for the pending inbox batch. It never fabricates
a user message, copies peer text into instructions, approves requests, or
acknowledges inbox messages. Opt-in waits use the same inbox. Delivery markers
persist under the database's `codex-wake/` directory. If a delivery response is
lost, it stops instead of blindly retrying: inspect the target chat before
removing its state file to rebaseline. Remove a stale `.lock` only after the prior
watcher has stopped. Interactive requests routed solely to this adapter are
rejected; the ordinary client must handle the chat's approvals.

This is an experimental transport adapter. Offline tests verify routing and
deduplication; they do not prove desktop idle wake. A connectivity check alone
also does not verify model turns or approval handling. See the official
[App Server protocol](https://learn.chatgpt.com/docs/app-server).

`PreToolUse` checks supported file edits
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

Tools include `patch_queue`, `board_wait`, `board_status`, `claim`, `release`, `takeover`, `noticeboard_read`,
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
  native-shaped payloads through CMD on Windows (a shell on Unix) for
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
