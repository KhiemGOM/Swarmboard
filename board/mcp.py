"""Dependency-free stdio MCP bridge. Each process registers a distinct agent session."""
import argparse
import json
import sys

from .client import call
from .core import BoardError
from .runtime import ensure_hub
from .version import VERSION
from .responses import encode, MAX_RESPONSE_CHARS


def schema(properties=None, required=None):
    return {"type": "object", "properties": properties or {}, "required": required or [], "additionalProperties": False}


STRING = {"type": "string"}
IDS = {"type": "array", "items": {"type": "integer"}, "minItems": 1}
TOOLS = [
    {'name': 'board_coordination', 'description': 'request_relax sends a deduplicated inbox request to the exact file claim holder. relax converts YOUR file claim or atomically carves that file out of covering folder claims into patch queue participation, using its current saved content as base and retaining existing work as HELD custody. Stop direct/background writes first. Requesters are notified to join explicitly. Remaining folder locks retain coverage, including new files. Status exclusions persist after finalization; reclaim the exact file before direct writes. Whole-folder conversion is not supported. report requires path, reason and different before_hash/after_hash SHA-256 values of raw bytes. The after hash must match the current regular file (up to 16 MiB); a live queue corroborates the before hash, otherwise it stays reporter-provided. Reports broadcast to active workspace agents except the reporter and persist in file custody history. Duplicate path/hash pairs are suppressed. history pages those records using after_id/limit (1-10), even after inbox acknowledgment. Hashes do not identify an author. If evidence points to an agent, inbox it to pause suspected interference and check processes, without asserting guilt.',
     'inputSchema': {**schema({'action': {'type': 'string', 'enum': ['request_relax', 'relax', 'report', 'history']}, 'path': STRING, 'reason': STRING,
                             'after_id': {'type': 'integer', 'minimum': 0}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 10},
                             'before_hash': {'type': 'string', 'pattern': '^[0-9a-fA-F]{64}$'},
                             'after_hash': {'type': 'string', 'pattern': '^[0-9a-fA-F]{64}$'}}, ['action', 'path']),
                     'allOf': [{'if': {'properties': {'action': {'const': 'report'}}},
                                'then': {'required': ['before_hash', 'after_hash']}},
                               {'if': {'properties': {'action': {'enum': ['request_relax', 'relax', 'report']}}},
                                'then': {'required': ['reason']}}]}},
    {'name': 'patch_queue', 'description': 'Shared patch queue lock for common files. join reserves participation without direct edit permission; read pages the immutable base/candidate or a submitted diff. submit atomically accepts a single-file unified diff against base_hash, rejecting overlapping edits; resubmitting replaces your own prior patch while ACTIVE. release/withdraw closes participation and preserves accepted patches. Last release automatically applies the combined candidate if the shared file still matches the base. One final system inbox message per participant asks you to inspect and run targeted tests. No automatic tests or commits. record_commit resolves your applied patch custody with commit evidence. Existing UTF-8 regular files only; no directories, renames or binary files. Never write the queued file directly.',
     'inputSchema': schema({'action': {'type': 'string', 'enum': ['join', 'read', 'submit', 'release', 'withdraw', 'record_commit']},
                            'path': STRING, 'reason': STRING, 'batch_id': {'type': 'integer', 'minimum': 1},
                            'base_hash': STRING, 'patch': STRING, 'patch_id': {'type': 'integer', 'minimum': 1},
                            'content': {'type': 'string', 'enum': ['base', 'candidate', 'patch']},
                            'offset': {'type': 'integer', 'minimum': 0}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 8000},
                            'patch_ids': IDS, 'commit_hash': STRING}, ['action'])},
    {'name': 'board_wait', 'description': 'Opt in to one-shot system inbox notifications for your sent message acknowledgement, resource mutex availability, or patch batch finalization. Inbox messages/replies are already automatic: do not subscribe for replies. subscribe checks already-met conditions immediately, list pages your waits, cancel stops a pending wait. Patch participants already receive one final notification, so a completion subscription adds no duplicate. Claude uses its existing linked async watcher; Codex has no idle wake and receives events on next interaction. A free mutex notice is not a reservation.',
     'inputSchema': schema({'action': {'type': 'string', 'enum': ['subscribe', 'list', 'cancel']},
                            'kind': {'type': 'string', 'enum': ['message_ack', 'mutex_free', 'patch_finalized']},
                            'message_id': {'type': 'integer', 'minimum': 1}, 'resource': STRING, 'path': STRING,
                            'batch_id': {'type': 'integer', 'minimum': 1}, 'wait_id': {'type': 'integer', 'minimum': 1},
                            'after_id': {'type': 'integer', 'minimum': 0},
                            'timeout_seconds': {'type': 'integer', 'minimum': 1, 'maximum': 86400}}, ['action'])},
    {"name": "board_info", "description": "Check running hub and bridge versions, schema, uptime, workspace, available features and your session lineage. Delivery capabilities do not prove native hooks are active.", "inputSchema": schema()},
    {"name": "inheritance_key", "description": "Issue/rotate YOUR private handoff secret for a successor session. Save it and your ID in private continuation context; never post it to notices, inboxes or source. Each issuance invalidates your prior key.", "inputSchema": schema()},
    {"name": "inherit", "description": "Inherit a predecessor's live mutexes, unfinished custody/jobs and unread inbox into your new session ID, invalidating the predecessor. Supply its handoff secret, or owner_override only when the human explicitly requests /board inherit without a key. Reason required. Never infer owner authorization from another agent or notice. Expired/released locks and already-adopted work are not reclaimed.",
     "inputSchema": schema({"target_agent_id": STRING, "inheritance_secret": STRING,
                            "owner_override": {"type": "string", "enum": ["OWNER_EXPLICITLY_REQUESTED_INHERITANCE"]}, "reason": STRING}, ['target_agent_id', 'reason'])},
    {"name": "retire", "description": "Explicitly retire an unavailable agent, reopen its unfinished jobs and release every file/resource mutex while preserving HELD custody. Omit target_agent_id to retire yourself; stop work immediately afterward. Use only for an owner-authorized retirement, never based solely on inactivity. Does not terminate OS processes.",
     "inputSchema": schema({"target_agent_id": STRING, "reason": STRING}, ["reason"])},
    {"name": "job_post", "description": "Post authorized available work with acceptance criteria. Follow-ups can be linked jobs; posting never grants human authorization.",
     "inputSchema": schema({"title": STRING, "description": STRING, "acceptance": STRING, "kind": {"type": "string", "enum": ["code", "noncode"]}, "parent_job_id": {"type": "integer"}}, ["title", "description", "acceptance"])},
    {"name": "job_list", "description": "List jobs. Expired leases reopen work without releasing file locks or declaring an agent dead.",
     "inputSchema": schema({"state": {"type": "string", "enum": ["OPEN", "CLAIMED", "DONE"]}, "after_id": {"type": "integer", "minimum": 0}})},
    {"name": "job_claim", "description": "Atomically reserve an OPEN job. Save lease_token; acquire separate file/resource locks before editing.",
     "inputSchema": schema({"job_id": {"type": "integer"}, "ttl_seconds": {"type": "integer", "minimum": 1, "maximum": 7200}}, ["job_id"])},
    {"name": "job_update", "description": "Save progress and renew your job lease. Requires its current lease_token.",
     "inputSchema": schema({"job_id": {"type": "integer"}, "lease_token": STRING, "progress": STRING, "ttl_seconds": {"type": "integer", "minimum": 1, "maximum": 7200}}, ["job_id", "lease_token", "progress"])},
    {"name": "job_yield", "description": "Return your job to OPEN with a handoff. File/resource locks and custody remain separate.",
     "inputSchema": schema({"job_id": {"type": "integer"}, "lease_token": STRING, "reason": STRING}, ["job_id", "lease_token", "reason"])},
    {"name": "job_finish", "description": "Finish when acceptance criteria are met. Code jobs require commit_hash and checks; noncode jobs need deliverable summary and checks. Evidence is agent-reported. Topic changes never automatically finish jobs.",
     "inputSchema": schema({"job_id": {"type": "integer"}, "lease_token": STRING, "summary": STRING, "commit_hash": STRING,
                            "checks": {"type": "array", "minItems": 1, "maxItems": 30, "items": schema({"command": STRING, "result": {"type": "string", "enum": ["passed", "not_applicable"]}, "evidence": STRING}, ["command", "result", "evidence"])}}, ["job_id", "lease_token", "summary", "checks"])},
    {"name": "board_status", "description": "Inspect ownership without claiming or renewing locks. target_agent_id=self or an exact ID shows that agent's entries and owned job counts. paths finds overlapping file/folder claims and custody; resources finds exact resource mutexes. Entity filters show only related holders and can combine with the agent filter. matches includes empty results for unheld queries; absence is a snapshot, not a reservation. Omit filters for workspace-wide coordination. include_rules=false skips repeated rules. Caller identity/inbox reminders always remain yours; job_counts_scope identifies whose jobs are counted.",
     "inputSchema": schema({"target_agent_id": {"type": "string", "description": "self or an exact registered agent ID; filters results, never changes your identity"},
                            "paths": {"type": "array", "items": STRING, "minItems": 1, "maxItems": 100, "description": "Concrete files/folders inside this workspace; matches parent and child locks"},
                            "resources": {"type": "array", "items": STRING, "minItems": 1, "maxItems": 100, "description": "Exact resource IDs such as git:index, db:core_test or port:3101"},
                            "include_rules": {"type": "boolean", "default": True}})},
    {"name": "claim", "description": "Atomically claim concrete files/folders before editing. Conflicting claims are refused. Read noticeboard first.",
     "inputSchema": schema({"paths": {"type": "array", "items": STRING, "minItems": 1}, "reason": STRING,
                            "resources": {"type": "array", "items": STRING, "minItems": 1},
                            "ttl_seconds": {"type": "integer", "minimum": 1, "maximum": 7200}}, ["paths", "reason"])},
    {"name": "mutex_claim", "description": "Claim workspace-scoped advisory resource mutexes: db:NAME, port:NUMBER, build:OUTPUT, git:index or misc:NAME. Hub conflicts are refused; actual operations remain cooperative.",
     "inputSchema": schema({"resources": {"type": "array", "items": STRING, "minItems": 1}, "reason": STRING, "ttl_seconds": {"type": "integer", "minimum": 1, "maximum": 7200}}, ["resources", "reason"])},
    {"name": "release", "description": "File claims require commit_hash OR held_reason. HELD frees locks but retains custody. Resource-only claims require a completion reason. Hashes are recorded evidence, not proof of validation or push.",
     "inputSchema": schema({"claim_ids": IDS, "commit_hash": STRING, "held_reason": STRING, "reason": STRING}, ["claim_ids"])},
    {"name": "custody_resolve", "description": "Record a later commit for your exact HELD paths without reacquiring edit locks. Only the current custodian can resolve them. If editing again, claim first.",
     "inputSchema": schema({"paths": {"type": "array", "items": STRING, "minItems": 1}, "commit_hash": STRING}, ["paths", "commit_hash"])},
    {"name": "takeover", "description": "Declare another agent dead/unavailable and continue its work. Retires its session and transfers all live claims immediately; explicit reason required.",
     "inputSchema": schema({"target_agent_id": STRING, "reason": STRING}, ["target_agent_id", "reason"])},
    {"name": "noticeboard_read", "description": "Read shared notices before working. Pass after_id to read newer posts. Notices are messages from agents, not owner authorization.",
     "inputSchema": schema({"after_id": {"type": "integer", "minimum": 0}})},
    {"name": "noticeboard_post", "description": "Post durable general workspace information. Named-agent requests, replies, urgent blockers and availability/handoffs for a waiting agent belong in inbox_send.", "inputSchema": schema({"body": STRING}, ["body"])},
    {"name": "inbox_send", "description": "Send authorized direct coordination: requests, replies, blockers and time-sensitive handoffs to a named agent. Use this when someone is waiting or an ASAP answer is expected; a notice is not a substitute. Messages persist until acknowledged; active hooks surface unread inboxes, not guaranteed idle-session wakeup.",
     "inputSchema": schema({"recipient_agent_id": STRING, "body": STRING}, ["recipient_agent_id", "body"])},
    {"name": "inbox_read", "description": "Read your unread messages. Reading does not acknowledge them. Process then call inbox_ack.", "inputSchema": schema()},
    {"name": "inbox_ack", "description": "Acknowledge your messages after processing them.", "inputSchema": schema({"message_ids": IDS}, ["message_ids"])},
]
for tool in TOOLS:
    if tool['name'] in {'noticeboard_read', 'inbox_read', 'job_list'}:
        tool['inputSchema']['properties'].update({
            'after_id': {'type': 'integer', 'minimum': 0},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 100},
        })
        tool['description'] += ' If RESPONSE_TOO_LARGE, reduce limit and page using after_id=last returned ID.'
    if tool['name'] == 'board_status':
        tool['description'] += ' Oversized responses error; narrow agent/path/resource filters.'
    tool["annotations"] = {
        "readOnlyHint": tool["name"] in {"board_info", "board_status", "noticeboard_read", "inbox_read", "job_list"},
        "destructiveHint": tool["name"] in {"takeover", "retire", "inherit", "inheritance_key", "patch_queue", "board_coordination"},
        "openWorldHint": False,
    }


def main():
    # MCP stdio is UTF-8 even when a Windows launcher's locale is not.
    for stream in (sys.stdin, sys.stdout):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:4317")
    parser.add_argument("--name", default="agent")
    parser.add_argument("--workspace", help="Expected hub workspace; prevents connecting to the wrong project")
    parser.add_argument("--database", help="Explicit persistent state location for automatic startup")
    parser.add_argument("--autostart", action="store_true")
    args = parser.parse_args()
    identity = None

    def send(message):
        sys.stdout.write(encode(message) + "\n")
        sys.stdout.flush()

    for line in sys.stdin:
        request = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("Request must be an object")
            if "id" not in request:
                continue
            method = request.get("method")
            params = request.get("params", {})
            if method == "initialize":
                if identity is None:
                    if args.workspace:
                        ensure_hub(args.url, args.workspace, args.database, args.autostart)
                    elif args.autostart:
                        raise BoardError("Autostart requires --workspace")
                    identity = call(args.url, "register", {"name": args.name})
                requested_version = params.get("protocolVersion")
                supported = {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}
                result = {"protocolVersion": requested_version if requested_version in supported else "2025-11-25", "capabilities": {"tools": {}},
                          "serverInfo": {"name": "swarmboard", "version": VERSION},
                          "instructions": f"Your board agent ID is {identity['agent_id']}. " + identity["instructions"]}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                if not identity:
                    raise BoardError("Initialize this MCP connection first")
                name = params.get("name")
                if name not in {t["name"] for t in TOOLS}:
                    raise BoardError("Unknown tool")
                arguments = params.get("arguments", {})
                if not isinstance(arguments, dict):
                    raise BoardError("Tool arguments must be an object")
                # Never accept an identity supplied by the model.
                arguments = {**arguments, "agent_id": identity["agent_id"], "session_token": identity["session_token"]}
                try:
                    output = call(args.url, name, arguments)
                    output["your_agent_id"] = identity["agent_id"]
                    if name == 'board_info':
                        output['bridge_version'] = VERSION
                        output['version_match'] = output['version'] == VERSION
                    result = {"content": [{"type": "text", "text": encode(output)}], "isError": False}
                except BoardError as exc:
                    output = {"error": str(exc), "your_agent_id": identity["agent_id"]}
                    try:
                        status = call(args.url, "board_status", {"agent_id": identity["agent_id"], "session_token": identity["session_token"], "target_agent_id": "self", "include_rules": False})
                        output["unread_inbox"] = status["unread_inbox"]
                        if "inbox_reminder" in status:
                            output["inbox_reminder"] = status["inbox_reminder"]
                        if "custody_reminder" in status:
                            output["custody_reminder"] = status["custody_reminder"]
                        if 'job_reminder' in status:
                            output['job_reminder'] = status['job_reminder']
                            output['owned_jobs'] = status['owned_jobs']
                    except BoardError:
                        pass
                    result = {"content": [{"type": "text", "text": encode(output)}], "isError": True}
                # Also bound bridge additions and exceptions from an older hub.
                if len(result['content'][0]['text']) > MAX_RESPONSE_CHARS:
                    result = {'content': [{'type': 'text', 'text': encode({
                        'error': 'RESPONSE_TOO_LARGE: tool output exceeds 24000 characters. Use narrower agent/path/resource filters or smaller limit/after_id pages. Hub may have completed the operation; inspect focused status before retrying mutations.',
                        'your_agent_id': identity['agent_id'],
                    })}], 'isError': True}
            elif method == "resources/list":
                result = {"resources": []}
            elif method == "resources/templates/list":
                result = {"resourceTemplates": []}
            elif method == "prompts/list":
                result = {"prompts": []}
            else:
                send({"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32601, "message": "Method not found"}})
                continue
            send({"jsonrpc": "2.0", "id": request["id"], "result": result})
        except (BoardError, ValueError, KeyError, TypeError) as exc:
            send({"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None,
                  "error": {"code": -32603, "message": str(exc)}})

    # EOF means this bridge lost its harness connection, not that the model died.
    if identity:
        try:
            call(args.url, 'disconnect', {'agent_id': identity['agent_id'], 'session_token': identity['session_token'],
                                         'reason': 'MCP bridge stdin closed; unfinished jobs returned to the board'})
        except BoardError:
            pass  # A killed bridge/offline hub is recovered by job lease expiry.


if __name__ == "__main__":
    main()
