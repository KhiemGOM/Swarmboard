# Patch queue lock

Use existing board MCP identity and confirm workspace. Queue operations are
available through patch_queue. Work only within the user's authorized scope.

## Where shared preparation helps

The queue gives collaborators an immutable base, preserves each accepted patch,
and publishes one combined candidate when everyone releases. Agents can prepare
independent contributions at the same time without one holding exclusive access
to every part of the file. Good fits include:

- Two fixes in different functions of a shared editor module.
- UI text/layout changes and a separate interaction fix in the same component,
  when their hunks and behavior do not depend on each other.
- Different existing keys or sections of one translation/catalogue file.
- Setup instructions and troubleshooting notes in separate README sections.
- Separate regression cases in distinct areas of an existing test file.

Agree on regions when useful. Two new entries appended at the same boundary can
still conflict, even if their names differ. Compatible hunks also need joint
validation: changes in different functions can have behavioral dependencies.

For a held direct file claim, request relaxation rather than merely waiting for
exclusive access if shared preparation would help. The holder decides when to
stop direct/background writes and convert the file. A broad folder claim can be
carved around just that shared file, keeping the rest protected. Never join
behind a direct writer or force conversion. Continue other authorized work if
the holder cannot switch yet.

Use an exclusive turn or staged batches when agents need to rewrite the same
function, depend on each other's new API, or perform a rename/multi-file change.
The queue suits independent single-file contributions; it is not a promise of
automatic conflict resolution or immediate publication while peers are working.

## Workflow

0. Check the direct lock before joining. If another agent holds a file/folder
   claim and your contribution fits independent shared preparation, call
   `board_coordination action=request_relax` with the exact absolute file `path`
   and a `reason` describing your contribution. This delivers the request to the
   holder's inbox. Do this instead of merely announcing that you are waiting.
   Work elsewhere until the holder converts the file, then explicitly join.
   If you hold the claim, stop direct/background writes and use `action=relax`
   through `board_coordination`. An already queued/unclaimed file can proceed
   directly to joining. A request is not conversion or permission to edit.
1. `action=join`, `path`, `reason`: join an open batch after resolving direct
   claims. Never join behind an exclusive writer, including your own process.
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
