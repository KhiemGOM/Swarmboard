# Review and release your locks

Use the existing board identity. Read focused `board_status` for `self`, notices
and inbox, and confirm the workspace. Review direct file/resource claims and
patch-queue memberships against the current task and running processes.

Release idle resource claim IDs with `release` and a reason. Release idle file
claim IDs with a scoped commit hash or a held reason explaining unfinished work.
Keep claims required by active operations. Do not discard work or manufacture
commit evidence to free a lock.

For each idle patch-queue membership, inspect the batch with `patch_queue`
`action=read` with its batch ID, then use `action=release`. Release preserves
submitted patches; the last participant's release can publish the candidate.
Keep membership while still preparing a patch. Record applied patch custody
with `action=record_commit` after the corresponding commit.

Do not release another agent's locks, renew everything or infer death from age.
Report what was released, what remains needed and any unfinished custody.
Use the inbox for an authorized direct handoff if another agent is waiting.
