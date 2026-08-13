"""A second copy of dictate, for the single-instance tests.

This is a TEST FIXTURE, not part of the product - it lives in `tests/` and
nothing in `src/` imports it. It exists so the lock can be tested the way it is
actually used: two operating-system processes contending for it, rather than two
objects inside one interpreter, which would prove nothing about the case that
matters.

    hold <lock> <seconds>   take the lock, print HELD, keep it, then let go
    take <lock>             try to take it; print HELD or REFUSED plus the message
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dictate.errors import AlreadyRunningError  # noqa: E402
from dictate.instance import InstanceLock  # noqa: E402


def main(argv: list[str]) -> int:
    action, target = argv[0], Path(argv[1])
    lock = InstanceLock(target, started_by="hand")
    if action == "hold":
        seconds = float(argv[2]) if len(argv) > 2 else 30.0
        lock.acquire()
        print("HELD", flush=True)
        time.sleep(seconds)
        lock.release()
        return 0
    if action == "take":
        try:
            lock.acquire()
        except AlreadyRunningError as exc:
            print("REFUSED", flush=True)
            print(exc.report(), flush=True)
            return 3
        print("HELD", flush=True)
        lock.release()
        return 0
    raise SystemExit(f"unknown action {action!r}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
