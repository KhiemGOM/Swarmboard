---
name: mutex
description: Review your own Swarmboard file and resource mutexes and release idle locks. Use when asked to check mutexes, stop hogging locks, free unused claims, or review locks before a handoff or scope change.
---

# Review your mutexes

Use the existing board MCP identity. Read board_status, noticeboard_read and
inbox_read; confirm its workspace matches the work being reviewed. If mismatched
or unavailable, report it and do not release locks through another identity.
Process inbox messages before acknowledging them.

Review claims belonging to your_agent_id against the current task and running
processes. A lease's remaining time is not a reason to hold an idle mutex.
Keep the smallest concrete claims needed for ongoing work. Keep a port while
its preview/server is in use; keep a database/build lock while its operation
is running; keep git:index only during staging/commit/index synchronization.
Do not terminate processes just to free locks unless the task authorizes that.

Release your idle resource claim IDs with a completion reason. Release idle
file claim IDs with the existing scoped commit_hash, or held_reason describing
unfinished work, retained custody and the next step. Do not invent commits,
commit merely to clear a mutex, silently discard changes, or clear custody.
If the lock's current use is uncertain, keep it and explain the uncertainty.
Do not release another agent's locks, infer death from age, or take over work
as part of this review. Do not automatically renew every claim.

If another agent is waiting, use inbox_send for the authorized direct update
or reply as soon as the relevant lock is released. Include the paths/resources,
claim IDs and remaining custody/dependencies. A noticeboard post does not deliver
that reply. If contact authorization is unclear, ask whether the user wants to
alert that agent; once authorized, continue that exchange without repeated asks.
Keep general durable facts on the noticeboard.

Report briefly what was released, what is still held and why, and any HELD
custody remaining. Continue the current task unless the user asked to stop.
