import argparse
import json
import os
from pathlib import Path

from .client import call
from .core import BoardError


def main():
    parser = argparse.ArgumentParser(description="Agent Board CLI; separate --session files identify separate agents")
    parser.add_argument("--url", default="http://127.0.0.1:4317")
    parser.add_argument("--session", default=".board/session.json")
    parser.add_argument("operation", choices=["register"] + [
        "board_status", "heartbeat", "claim", "release", "takeover", "noticeboard_read", "noticeboard_post", "inbox_send", "inbox_read", "inbox_ack"])
    parser.add_argument("--params", default="{}", help="JSON arguments")
    args = parser.parse_args()
    try:
        params = json.loads(args.params)
        session_path = Path(args.session)
        if args.operation == "register":
            if session_path.exists():
                raise BoardError("Session file already exists. Choose a new --session path to register another agent.")
            result = call(args.url, "register", params)
            session_path.parent.mkdir(parents=True, exist_ok=True)
            session_path.write_text(json.dumps(result), encoding="utf-8")
            if os.name != "nt":
                session_path.chmod(0o600)
            result = {key: value for key, value in result.items() if key != "session_token"}
        else:
            identity = json.loads(session_path.read_text(encoding="utf-8"))
            result = call(args.url, args.operation, {**params, "agent_id": identity["agent_id"], "session_token": identity["session_token"]})
        print(json.dumps(result, indent=2))
    except (BoardError, OSError, ValueError) as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()
