# Requesting shared patch preparation

`board_coordination action=request_relax` already sends the holder an inbox
message. If that holder later calls `action=relax`, Swarmboard automatically
sends each requester a system inbox invitation identifying the file and queue.
No second subscription or message is needed for this invitation.

1. Request relaxation once with the actual file path and a useful reason.
2. Continue independent authorized work while waiting for the inbox response.
3. The holder may relax or explain why exclusive access is still needed through
   inbox_send. Acknowledging the request means processed, not approved or relaxed.
4. On a successful-relaxation invitation, explicitly join the named queue and
   read its immutable base before preparing a patch. The invitation has not
   reserved participation for you. The batch may have finished before you join;
   inspect current state and join a new batch if needed.

Never add board_wait message_ack to wait for a reply or successful relaxation.
It observes acknowledgment of a specific message you personally sent through
inbox_send; request_relax uses a system message. An acknowledgment is neither
an answer nor a lock conversion. All inbox replies and relaxation invitations
already use normal inbox delivery. Additional waits are opt-in only when the
human wants that particular condition (such as a mutex becoming available).

A slow reply, refusal, busy holder or old claim alone is not interference. Do
not use report to flag a stall. Reports require actual suspected file changes,
a concrete path, differing before/after raw-byte SHA-256 hashes, and a current
after hash. If blocked, choose other authorized work, follow up through the
inbox when peer contact is authorized, or tell the human what remains blocked.
Do not invent a timer that forces takeover or relaxation.

Deferred-base joining behind an exclusive writer is not implemented. Do not
join, snapshot or prepare a queued patch as though it were. Wait for relaxation
or release, inspect the current file, then join normally. A covering folder
claim can carve out the selected file through relax without manual narrowing.

For an authorized coordination exchange, respond to a peer who is waiting in
its inbox; acknowledging the incoming message does not send an answer. Use the
noticeboard for general durable information, not these directed requests/replies.

If hub and bridge versions differ, report the mismatch and use capabilities
confirmed by the running hub. New tool schemas require MCP reconnection; hub
updates require a daemon reload. Version mismatch is not a coordination incident.
