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
    die_after_health = 0
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
            return
        self._json(200, {"status": "ok"})
        if type(self).die_after_health:
            # Crash *after* saying it is healthy, so a test can exercise the
            # restart path without betting on how long the process took to
            # start. A timer cannot do that: process startup takes a few
            # hundred milliseconds on Windows and almost none on Linux, so a
            # timer short enough to be quick there fires before the server is
            # ever up here, and the supervisor correctly reports a different
            # failure. (That is what CI caught.)
            type(self).die_after_health -= 1
            if type(self).die_after_health == 0:
                try:
                    self.wfile.flush()
                except OSError:
                    pass
                print("stub-server: dying now", flush=True)
                os._exit(9)

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
                fail_inference: int = 0,
                die_after_health: int = 0) -> ThreadingHTTPServer:
    handler = type("BoundStub", (StubHandler,), {
        "ready_at": time.monotonic() + ready_after,
        "reply_text": reply_text,
        "fail_inference": fail_inference,
        "die_after_health": die_after_health,
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
    parser.add_argument("--die-after-health", type=int, default=0,
                        help="exit abruptly after answering this many health "
                             "checks. Prefer this to --die-after for testing "
                             "the restart path: it does not depend on how long "
                             "the process took to start, which differs by an "
                             "order of magnitude between Windows and Linux")
    parser.add_argument("--die-once-marker", default=None,
                        help="with --die-after or --die-after-health: only die "
                             "if this file does not exist yet, so the FIRST run "
                             "crashes and the restarted one stays up")
    parser.add_argument("--slow-after-marker", type=float, default=0.0,
                        help="with --die-once-marker: become ready this many "
                             "seconds from now, but only on the runs AFTER the "
                             "first. That is how a test gets the supervisor "
                             "stuck part way through a restart on purpose")
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

    should_die = bool(args.die_after) or bool(args.die_after_health)
    is_a_restart = bool(args.die_once_marker) and os.path.exists(args.die_once_marker)
    if should_die and args.die_once_marker:
        if is_a_restart:
            should_die = False
        else:
            with open(args.die_once_marker, "w", encoding="utf-8") as fh:
                fh.write("died once")

    ready_after = args.ready_after
    if is_a_restart and args.slow_after_marker:
        ready_after = args.slow_after_marker

    server = make_server(
        args.port, ready_after=ready_after, reply_text=args.reply,
        die_after_health=(args.die_after_health if should_die else 0),
    )
    print("stub-server: listening", flush=True)

    if should_die and args.die_after:
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
