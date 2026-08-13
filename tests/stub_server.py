"""A stand-in for `whisper-server`, speaking the same HTTP contract.

This is a TEST FIXTURE, not part of the product - it lives in `tests/` and
nothing in `src/` imports it. It exists so the process supervisor and the HTTP
client can be tested for real (a real child process, a real socket, real
multipart parsing) on a machine with no GPU and no whisper.cpp build.

It implements the two endpoints dictate uses:
    GET  /health     -> {"status":"ok"} 200, or {"status":"loading model"} 503
    POST /inference  -> {"text": "..."}

Options let a test make it slow to become ready, or make it die, which is how
the restart behaviour is exercised.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # set by make_server
    ready_at = 0.0
    reply_text = "Hello from the stub."
    fail_inference = 0
    seen: list = []

    def log_message(self, *_args):  # keep the test output clean
        pass

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path != "/health":
            self._json(404, {"error": "not found"})
            return
        if time.monotonic() < type(self).ready_at:
            self._json(503, {"status": "loading model"})
        else:
            self._json(200, {"status": "ok"})

    def do_POST(self):
        if self.path != "/inference":
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        content_type = self.headers.get("Content-Type", "")
        type(self).seen.append({"content_type": content_type, "body": body})
        if type(self).fail_inference:
            self._json(type(self).fail_inference, {"error": "stub failure"})
            return
        if b"filename=" not in body or b"RIFF" not in body:
            self._json(400, {"error": "no wav file in the request"})
            return
        self._json(200, {"text": type(self).reply_text})


def make_server(port: int = 0, *, ready_after: float = 0.0,
                reply_text: str = "Hello from the stub.",
                fail_inference: int = 0) -> ThreadingHTTPServer:
    handler = type("BoundStub", (StubHandler,), {
        "ready_at": time.monotonic() + ready_after,
        "reply_text": reply_text,
        "fail_inference": fail_inference,
        "seen": [],
    })
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


def serve_in_thread(**kwargs) -> tuple[ThreadingHTTPServer, int]:
    server = make_server(**kwargs)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--ready-after", type=float, default=0.0)
    parser.add_argument("--die-after", type=float, default=0.0,
                        help="exit abruptly this many seconds after starting")
    parser.add_argument("--die-once-marker", default=None,
                        help="with --die-after: only die if this file does not "
                             "exist yet, so the FIRST run crashes and the "
                             "restarted one stays up")
    parser.add_argument("--exit-immediately", action="store_true")
    parser.add_argument("--reply", default="Hello from the stub.")
    # whisper-server's real flags, accepted and ignored, so a test can pass the
    # same argv the backend builds.
    for flag in ("--model", "--host", "--threads", "--language",
                 "--beam-size", "--best-of"):
        parser.add_argument(flag, default=None)
    parser.add_argument("--no-timestamps", action="store_true")
    parser.add_argument("--no-gpu", action="store_true")
    args = parser.parse_args()

    if args.exit_immediately:
        print("stub: refusing to start (simulated bad model file)", file=sys.stderr)
        return 7

    server = make_server(args.port, ready_after=args.ready_after, reply_text=args.reply)
    print("stub-server: listening", flush=True)

    should_die = bool(args.die_after)
    if should_die and args.die_once_marker:
        if os.path.exists(args.die_once_marker):
            should_die = False
        else:
            with open(args.die_once_marker, "w", encoding="utf-8") as fh:
                fh.write("died once")

    if should_die:
        def die():
            time.sleep(args.die_after)
            print("stub-server: dying now", flush=True)
            os._exit(9)

        threading.Thread(target=die, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
