"""Dependency-free stdio MCP bridge. Each process registers a distinct agent session."""
import argparse
import json
import sys

from .client import call
from .core import BoardError
from .runtime import ensure_hub


def schema(properties=None, required=None):
    return {"type": "object", "properties": properties or {}, "required": required or [], "additionalProperties": False}


STRING = {"type": "string"}
IDS = {"type": "array", "items": {"type": "integer"}, "minItems": 1}
TOOLS = [
    {"name": "board_status", "description": "Read latest agents and mutex claims before work. Includes your agent ID and unread inbox reminder.", "inputSchema": schema()},
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
    {"name": "noticeboard_post", "description": "Post resource information, dependencies, or handoff notes to all agents.", "inputSchema": schema({"body": STRING}, ["body"])},
    {"name": "inbox_send", "description": "Persist a message in another registered agent's inbox.",
     "inputSchema": schema({"recipient_agent_id": STRING, "body": STRING}, ["recipient_agent_id", "body"])},
    {"name": "inbox_read", "description": "Read your unread messages. Reading does not acknowledge them. Process then call inbox_ack.", "inputSchema": schema()},
    {"name": "inbox_ack", "description": "Acknowledge your messages after processing them.", "inputSchema": schema({"message_ids": IDS}, ["message_ids"])},
]
for tool in TOOLS:
    tool["annotations"] = {
        "readOnlyHint": tool["name"] in {"board_status", "noticeboard_read", "inbox_read"},
        "destructiveHint": tool["name"] == "takeover",
        "openWorldHint": False,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:4317")
    parser.add_argument("--name", default="agent")
    parser.add_argument("--workspace", help="Expected hub workspace; prevents connecting to the wrong project")
    parser.add_argument("--database", help="Explicit persistent state location for automatic startup")
    parser.add_argument("--autostart", action="store_true")
    args = parser.parse_args()
    identity = None

    def send(message):
        sys.stdout.write(json.dumps(message) + "\n")
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
                          "serverInfo": {"name": "swarmboard", "version": "0.2.0"},
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
                    result = {"content": [{"type": "text", "text": json.dumps(output)}], "isError": False}
                except BoardError as exc:
                    output = {"error": str(exc), "your_agent_id": identity["agent_id"]}
                    try:
                        status = call(args.url, "board_status", {"agent_id": identity["agent_id"], "session_token": identity["session_token"]})
                        output["unread_inbox"] = status["unread_inbox"]
                        if "inbox_reminder" in status:
                            output["inbox_reminder"] = status["inbox_reminder"]
                        if "custody_reminder" in status:
                            output["custody_reminder"] = status["custody_reminder"]
                    except BoardError:
                        pass
                    result = {"content": [{"type": "text", "text": json.dumps(output)}], "isError": True}
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


if __name__ == "__main__":
    main()
