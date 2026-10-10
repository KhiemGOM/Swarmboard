# Patch queue lock

Use existing board MCP identity and confirm workspace. Queue operations are
available through patch_queue. Work only within the user's authorized scope.

1. `action=join`, `path`, `reason`: join an open batch. An exclusive claim,
   including your own, conflicts; release it appropriately before joining.
2. `action=read`, `batch_id`, `content=base`: get the immutable base with its
   `base_hash`. Page using character `offset` and `limit` (at most 8000).
3. Prepare a single-file unified diff with `--- a/path` and `+++ b/path`.
   Do not edit the queued file itself. A private scratch copy is fine; do not
   confuse it with the shared file. Submit with `action=submit`, `batch_id`,
   `base_hash`, `patch`. The diff must match that base exactly. Each participant
   has one accepted patch; resubmitting replaces your own patch until release.
4. If rejected, inspect conflicting patch IDs and the candidate via `read`, then
   revise against the same base. Other accepted patches remain unchanged.
5. `action=release`, `batch_id` ends participation. `withdraw` also ends it and
   preserves already accepted patches. Neither allows later submissions to that
   batch. When all participants finish, the board automatically publishes the
   combined candidate if the shared file still matches the base. A conflict with
   an external write blocks publication and preserves the candidate for repair.
6. Everyone receives one final system inbox message. Inspect your changes in the
   final file and run targeted tests. Acceptance proves compatible line edits,
   not correct program behavior. Report failures with evidence to relevant peers
   via inbox when contact is authorized. No automatic tests or commits occur.
7. After committing, call `action=record_commit`, your `patch_ids`, `commit_hash`
   to resolve per-patch custody. Normal git:index rules still apply.

Existing UTF-8 regular files up to 1 MiB are supported; diffs up to 32000
characters. Binary files, renames, new/deleted files and multi-file transactions
are not supported. Identical-line or insertion-boundary overlaps are rejected
conservatively. Direct writers can bypass cooperative guards; the final base
check detects changed content, but is not an operating-system filesystem lock.

An absent participant keeps a batch open. Request explicit retirement when the
owner declares it unavailable; retirement preserves accepted patches and ends
that participant's wait. Mere silence/disconnection does not release membership.
Inheritance moves unfinished patch custody, queue membership and pending waits
to the successor. Do not create a duplicate identity to escape membership.

HELD file work older than two hours is highlighted. Reclaiming/releasing it does
not reset its original unresolved timestamp. Prefer small coherent commits.
