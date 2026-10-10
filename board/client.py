import json
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from .core import BoardError


def call(url, method, params):
    request = Request(url.rstrip("/") + "/call", json.dumps({"method": method, "params": params}).encode(),
                      {"Content-Type": "application/json"})
    try:
        # Local coordination must never pass through an inherited corporate proxy.
        with build_opener(ProxyHandler({})).open(request, timeout=10) as response:
            return json.load(response)
    except HTTPError as exc:
        try:
            message = json.load(exc).get("error", str(exc))
        except ValueError:
            message = str(exc)
        raise BoardError(message) from exc
    except URLError as exc:
        raise BoardError(f"Hub unavailable ({exc.reason}). Start board.server before connecting an agent.") from exc
