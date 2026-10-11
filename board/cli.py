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
        "board_file", "board_info", "board_agent_name", "patch_queue", "board_wait", "inheritance_key", "inherit", "board_status", "heartbeat", "disconnect", "retire", "claim", "mutex_claim", "release", "custody_resolve", "takeover", "noticeboard_read", "noticeboard_post", "inbox_send", "inbox_read", "inbox_ack",
        "job_post", "job_list", "job_claim", "job_update", "job_yield", "job_finish"])
    parser.add_argument("--params", default="{}", help="JSON arguments")
    parser.add_argument("--title", help="Quick job_post title")
    parser.add_argument("--description", help="Quick job_post description")
    parser.add_argument("--acceptance", help="Quick job_post acceptance criteria")
    args = parser.parse_args()
    try:
        params = json.loads(args.params)
        if not isinstance(params, dict):
            raise BoardError('params must be a JSON object')
        for key in ('title', 'description', 'acceptance'):
            if getattr(args, key) is not None:
                if args.operation != 'job_post':
                    raise BoardError(f'--{key} is only for job_post')
                params[key] = getattr(args, key)
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
