import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .core import BoardError, Hub


def serve(hub, port=4317):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/health" or self.headers.get("Origin"):
                self.send_error(404)
                return
            payload = json.dumps({"service": "agent-board", "version": "0.1.0", "workspace": str(hub.workspace)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            if self.path != "/call":
                self.send_error(404)
                return
            # Reject browser-origin calls; this prototype is a local agent interface.
            if self.headers.get("Origin"):
                self.send_error(403)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536:
                    raise BoardError("Invalid request size")
                request = json.loads(self.rfile.read(length))
                result = hub.call(request["method"], request.get("params", {}))
                status = 200
            except (BoardError, ValueError, KeyError, TypeError) as exc:
                result, status = {"error": str(exc)}, 400
            payload = json.dumps(result).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--database")
    parser.add_argument("--port", type=int, default=4317)
    args = parser.parse_args()
    workspace = Path(args.workspace).resolve()
    hub = Hub(args.database or workspace / ".board" / "hub.sqlite", workspace)
    server = serve(hub, args.port)
    print(f"Agent Board listening on http://127.0.0.1:{server.server_port}; workspace={workspace}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
