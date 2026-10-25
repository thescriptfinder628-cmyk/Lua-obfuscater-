#!/usr/bin/env python3
"""A zero-dependency web playground for the obfuscator.

    python tools/playground.py            # http://localhost:8000
    python tools/playground.py --port 5000 --host 127.0.0.1

Serves ``playground/index.html`` and a tiny JSON API:

    POST /api/obfuscate  {"code": "...", "level": "medium", "seed": 7, "verify": true}
      -> {"code": "...", "stats": {...}, "warnings": [...], "verified": true}
      -> {"error": "message", "line": 3}   with HTTP 400

Only the standard library is used, so it runs anywhere Python does.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from luaobf.lexer import LuaSyntaxError  # noqa: E402
from luaobf.obfuscator import PRESETS, obfuscate_source, preset  # noqa: E402
from luaobf.verify import VerificationUnavailable, verify  # noqa: E402

PAGE = os.path.join(ROOT, "playground", "index.html")
MAX_BODY = 2 * 1024 * 1024  # 2 MB of Lua is plenty


def obfuscate_request(payload: dict) -> tuple[int, dict]:
    code = payload.get("code", "")
    if not isinstance(code, str):
        return 400, {"error": "`code` must be a string"}
    if len(code) > MAX_BODY:
        return 413, {"error": "input too large"}

    level = payload.get("level", "medium")
    if level not in PRESETS:
        return 400, {"error": f"unknown level {level!r}"}

    seed = payload.get("seed")
    try:
        seed = int(seed) if seed is not None else None
    except (TypeError, ValueError):
        return 400, {"error": "`seed` must be an integer"}

    options = preset(level, seed=seed)
    try:
        result = obfuscate_source(code, options, filename="<playground>")
    except LuaSyntaxError as exc:
        return 400, {"error": str(exc), "line": exc.line, "col": exc.col}
    except Exception as exc:  # noqa: BLE001 - report, don't crash the server
        traceback.print_exc()
        return 500, {"error": f"internal error: {type(exc).__name__}: {exc}"}

    response = {
        "code": result.code,
        "stats": {k: v for k, v in result.stats.items() if k != "names"},
        "warnings": result.warnings,
        "verified": False,
    }

    if payload.get("verify"):
        try:
            ok, message = verify(code, result.code, timeout=5.0)
        except VerificationUnavailable as exc:
            response["warnings"].append(f"could not verify: {exc}")
        else:
            if not ok:
                return 422, {"error": message}
            response["verified"] = True
    return 200, response


class Handler(BaseHTTPRequestHandler):
    server_version = "luaobf-playground"

    def log_message(self, fmt, *args):  # keep the console readable
        sys.stderr.write("  %s\n" % (fmt % args))

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # no X-Frame-Options / CSP frame-ancestors: the page must work inside
        # the preview iframe
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            try:
                with open(PAGE, "rb") as handle:
                    self._send(200, handle.read(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, b"playground/index.html is missing", "text/plain")
            return
        if path == "/api/levels":
            body = json.dumps(sorted(PRESETS)).encode()
            self._send(200, body, "application/json")
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != "/api/obfuscate":
            self._send(404, b"not found", "text/plain")
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY * 4:
            self._send(413, b'{"error": "body too large"}', "application/json")
            return
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
            if not isinstance(payload, dict):
                raise ValueError
        except ValueError:
            self._send(400, b'{"error": "invalid JSON body"}', "application/json")
            return
        status, data = obfuscate_request(payload)
        self._send(status, json.dumps(data).encode(), "application/json")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lua obfuscator web playground")
    parser.add_argument("--host", default="0.0.0.0", help="bind address (default 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="port (default 8000)")
    args = parser.parse_args(argv)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Lua obfuscator playground on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
