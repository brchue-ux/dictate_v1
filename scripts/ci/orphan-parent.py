"""A parent that starts a supervised child and then waits to be killed.

This is the fixture for the one thing about the orphaned whisper-server that
cannot be checked anywhere but a real Windows machine: whether the job object
in `dictate/platform/windows/job.py` really does make Windows end the child when
the parent dies without running any of its own cleanup.

CI starts this, waits for it to report the child's process id, ends THIS process
with `Stop-Process -Force` - a TerminateProcess, so nothing below runs and no
shutdown handler of dictate's gets a say - and then requires the child to be
gone and its port to be free. That is as close as anyone can get to the product
owner's Ctrl+C and "Terminate batch job (Y/N)? Y" without his machine.

It uses dictate's own `ManagedProcess`, not a copy of it, so a change that
broke the containment would break this too.

    --no-guard   start the child with no containment at all, which is what the
                 build before this one did. CI runs that as the control: the
                 child MUST survive, or this test proves nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "src"))

from dictate.engines.process import ManagedProcess          # noqa: E402
from dictate.engines.whisper_server import WhisperServerClient  # noqa: E402


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--stub", required=True, help="path to tests/stub_server.py")
    parser.add_argument("--report", required=True,
                        help="where to write the two process ids")
    parser.add_argument("--no-guard", action="store_true",
                        help="the control: no containment, so the child should "
                             "survive being orphaned")
    args = parser.parse_args(argv)

    client = WhisperServerClient("127.0.0.1", args.port)
    proc = ManagedProcess(
        [sys.executable, args.stub, "--port", str(args.port)],
        name="stub-server", health_check=client.is_healthy,
        startup_timeout_s=60.0, poll_interval_s=0.1,
    )
    if args.no_guard:
        # Deliberately taking the containment away. This is the ONLY thing that
        # differs between the real run and the control, so if the control's
        # child dies too, the real run proves nothing about the job object.
        proc.guard = None
    guarded = proc.guard is not None

    proc.start()
    Path(args.report).write_text(json.dumps({
        "parent": os.getpid(),
        "child": proc.pid,
        "guarded": guarded,
        "guard": proc.guard.describe if guarded else "",
    }), encoding="utf-8")
    print(f"parent ready: child {proc.pid}, guarded={guarded}", flush=True)

    # Wait to be killed. Nothing after this line is expected to run - that is
    # the whole point of the test.
    while True:
        time.sleep(1.0)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
