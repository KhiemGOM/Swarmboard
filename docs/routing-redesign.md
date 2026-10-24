# Routing redesign (0.12.0)

## What broke

1. **Shared bridge.** `ClaudeProfiles/acc2/claude_desktop_config.json` still defined `board`.
   Claude Desktop launches that definition once (its cwd is `C:\`, it has no
   `CLAUDE_CODE_SESSION_ID`) and routes every local Code chat's `mcp__board__*` calls
   to it, so every chat shared one in-memory identity and workspace. The installer
   only cleaned the default profile path.
2. **Bridge-global state.** The bridge keeps a single `identity` and `args.workspace`
   in memory, chosen from its process cwd/env. `board_workspace` mutates that for
   every chat that shares the process.
3. **Two route files.** `.codex/board-operator.json` and `.board/operator.json` are both
   read; here they disagree (`.board/operator.json` points at SkillScoped:4317).
4. **Identity caches.** Hook state tokens, `native-reconnect/*.json` records and the
   PostToolUse response scraping all try to re-derive "who is this chat".

## Design

- **One route file:** `.board/operator.json` = `{workspace, database, port}`, found by
  walking up from the chat's own cwd. A file whose `workspace` is not its own
  directory is stale and ignored. Legacy `.codex/board-operator.json` is read only
  when no canonical file exists, and `/board` migrates and deletes it.
- **Identity = (board, harness, native chat).** The hub already maps
  `native_sessions(harness, session_key) -> agent`; `register` with a native key
  returns the same agent for the same chat. Nothing else caches identity.
- **Per-call route.** The PreToolUse hook writes
  `_route = {harness, session_id, cwd}` into every board tool call (Claude Code
  applies `updatedInput` to MCP tools when the key is declared in the schema;
  verified live). The bridge resolves board + identity per call from `_route`,
  falling back to its own env/cwd only when no route was injected (Codex,
  hook-less setups). A Claude bridge with neither (the Desktop-shared bridge) refuses.
- **No cwd guessing.** No route file above the chat folder means no board: tools
  answer `WORKSPACE_REQUIRED`, nothing is auto-created at `C:\`.
- **`/board` cleans up.** Typing `/board` normalizes routing for the chat's project:
  writes the canonical route (migrating legacy values, replacing stale files),
  deletes the legacy route file, the `native-reconnect` cache and hook-session
  files older than 14 days, and warns about any Desktop config that still defines
  `board`.
- **History retention.** The hub prunes history older than 14 days (events, acked
  messages, notices, finished/deleted jobs, released claims, committed custody,
  finalized patch batches, settled wake deliveries, scratch records, and idle
  retired/disconnected agents with no live work) at startup and hourly.
  Live work is never pruned.
- **`/board reset`.** Wipes the whole board for the project (all rows except
  settings). Hook-gated: the `board_reset` tool is denied unless the user typed
  `/board reset` in this chat within the last 10 minutes; one use per request.
  Bridges re-register automatically on the next call.
- **Installer.** Removes `board` from every Desktop config, including
  `~/ClaudeProfiles/*/claude_desktop_config.json`.

## Potential failures and how the tests cover them

| Risk | Test |
|---|---|
| Two chats through one bridge get one identity (today's bug) | two `_route`s → two agents, each stable |
| Same chat across two bridges gets two identities | same `_route` twice in two bridges → same agent |
| Model forges `_route` to act as another chat | hook always overwrites `_route` (equivalent to `board_identity`, which is already trusted) |
| Chat in `C:\` or unconfigured folder creates a board | no route → `WORKSPACE_REQUIRED`, no DB file created |
| Stale route file (workspace mismatch) hijacks routing | stale file ignored by resolution, replaced by `/board` |
| Legacy-only project loses its live board on upgrade | legacy route still resolves; `/board` migrates same db/port |
| Subfolder cwd | walks up to nearest valid route |
| Desktop-shared Claude bridge without hook | refuses with message naming the duplicate config |
| Hook-less Codex bridge | env + process cwd fallback still works |
| Reset/prune deletes the caller's agent | bridge re-registers once on `Unknown agent` and retries |
| `/board reset` triggered by model text or replayed | denied without a fresh user `/board reset`; consumed after one use |
| Prune removes live work | open jobs, active claims, unacked messages, HELD custody and active agents survive |
| Undeclared `_route` stripped by Claude Code | `_route` declared on every tool schema |
| Existing behaviour regressions (claims, inbox, titles, opt-in) | existing suites keep passing |

Retired tests: ones that pin the removed native-reconnect cache, PostToolUse
identity scraping, bridge-global `board_workspace` switching, and the
`.codex/board-operator.json`-first precedence.
