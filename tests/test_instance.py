"""One copy at a time, and a way to stop it.

Two copies of dictate would fight over the global hotkey and over
whisper-server's port, and the failure that produces reads as "dictate is
broken" rather than "there are two of them". So the guard is tested the way it
is used: with a second real process contending for the lock, on whichever
operating system these tests are running on - which includes real Windows, in
CI.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from dictate import instance
from dictate.errors import AlreadyRunningError

CHILD = Path(__file__).resolve().parent / "lock_child.py"


def spawn(action: str, path: Path, *extra: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(CHILD), action, str(path), *extra],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )


def wait_for_line(proc: subprocess.Popen, timeout: float = 20.0) -> str:
    """The child's first line, or "" if it never said anything."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if line:
            return line.strip()
        if proc.poll() is not None:
            return ""
    return ""


class TempState(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._previous = os.environ.get("DICTATE_STATE_DIR")
        os.environ["DICTATE_STATE_DIR"] = str(self.dir)
        self.addCleanup(self._restore)

    def _restore(self):
        if self._previous is None:
            os.environ.pop("DICTATE_STATE_DIR", None)
        else:
            os.environ["DICTATE_STATE_DIR"] = self._previous
        self._tmp.cleanup()


class StateDirectory(TempState):
    def test_the_override_wins(self):
        self.assertEqual(instance.state_dir(), self.dir)
        self.assertEqual(instance.lock_path(), self.dir / instance.LOCK_NAME)

    def test_windows_keeps_it_under_localappdata(self):
        from unittest import mock

        with mock.patch.dict(os.environ,
                             {"LOCALAPPDATA": r"C:\Users\owner\AppData\Local"},
                             clear=True):
            found = instance.state_dir()
        self.assertEqual(found.name, "dictate")
        self.assertIn("Local", str(found))


class TheLock(TempState):
    def test_a_second_process_is_refused_and_told_who_has_it(self):
        holder = spawn("hold", instance.lock_path(), "30")
        self.addCleanup(holder.kill)
        self.assertEqual(wait_for_line(holder), "HELD")

        second = spawn("take", instance.lock_path())
        out, _ = second.communicate(timeout=30)
        self.assertEqual(second.returncode, 3, out)
        self.assertIn("REFUSED", out)
        self.assertIn("already running", out)
        # It names the copy that got there first, rather than saying only that
        # something went wrong.
        self.assertIn(str(holder.pid), out)
        self.assertIn("dictate stop", out)

    def test_the_lock_is_gone_when_the_process_that_held_it_dies(self):
        """No stale locks, ever: the operating system releases it, so nothing
        has to guess whether a recorded pid is still alive."""
        holder = spawn("hold", instance.lock_path(), "30")
        self.assertEqual(wait_for_line(holder), "HELD")
        holder.kill()
        holder.wait(timeout=20)
        # The lock file is still on disk, with a dead pid written in it.
        self.assertTrue(instance.lock_path().exists())

        second = spawn("take", instance.lock_path())
        out, _ = second.communicate(timeout=30)
        self.assertEqual(second.returncode, 0, out)
        self.assertIn("HELD", out)

    def test_running_instance_reports_the_holder_and_then_nothing(self):
        self.assertIsNone(instance.running_instance())

        holder = spawn("hold", instance.lock_path(), "30")
        self.addCleanup(holder.kill)
        self.assertEqual(wait_for_line(holder), "HELD")

        found = instance.running_instance()
        self.assertIsNotNone(found)
        self.assertEqual(found.pid, holder.pid)
        self.assertEqual(found.started_by, "hand")
        self.assertIn("started", found.describe())

        holder.kill()
        holder.wait(timeout=20)
        self.assertIsNone(instance.running_instance())

    def test_a_lock_file_left_by_a_dead_process_is_not_a_running_copy(self):
        instance.lock_path().parent.mkdir(parents=True, exist_ok=True)
        instance.lock_path().write_bytes(b"pid=999999\nstarted_by=hand\n")
        self.assertIsNone(instance.running_instance())

    def test_it_can_be_taken_again_after_release(self):
        lock = instance.InstanceLock()
        lock.acquire()
        self.assertIsNotNone(instance.running_instance())
        lock.release()
        self.assertIsNone(instance.running_instance())
        with instance.InstanceLock():
            self.assertIsNotNone(instance.running_instance())

    def test_the_error_carries_the_holder_for_the_message(self):
        with instance.InstanceLock(started_by="logon"):
            second = instance.InstanceLock()
            with self.assertRaises(AlreadyRunningError) as ctx:
                second.acquire()
        self.assertEqual(ctx.exception.holder.pid, os.getpid())
        self.assertIn("logged in", ctx.exception.message)
        self.assertTrue(ctx.exception.remedy)


class TheRecord(unittest.TestCase):
    def test_junk_is_read_as_no_details_rather_than_an_error(self):
        holder = instance.parse_record("this is not a record at all")
        self.assertIsNone(holder.pid)
        self.assertEqual(holder.describe(), "no details recorded")

    def test_a_missing_file_reads_as_nothing(self):
        self.assertIsNone(instance.read_record(Path("/nonexistent/dictate.lock")))

    def test_it_says_how_the_copy_was_started(self):
        self.assertIn("automatically",
                      instance.parse_record("started_by=logon").describe())
        self.assertIn("by hand", instance.parse_record("started_by=hand").describe())


class StopRequests(TempState):
    def test_a_request_is_seen_and_can_be_cleared(self):
        started = time.time()
        self.assertFalse(instance.stop_requested(since=started))
        instance.request_stop()
        self.assertTrue(instance.stop_requested(since=started))
        instance.clear_stop_request()
        self.assertFalse(instance.stop_requested(since=started))

    def test_a_request_meant_for_an_earlier_copy_is_ignored(self):
        """Otherwise dictate would exit the moment it started, for a reason
        nobody could see."""
        instance.request_stop(now=time.time() - 3600)
        self.assertFalse(instance.stop_requested(since=time.time()))

    def test_clearing_a_request_that_is_not_there_is_not_an_error(self):
        instance.clear_stop_request()

    def test_waiting_gives_up_and_says_so(self):
        holder = spawn("hold", instance.lock_path(), "30")
        self.addCleanup(holder.kill)
        self.assertEqual(wait_for_line(holder), "HELD")
        self.assertFalse(instance.wait_until_stopped(0.5, poll_s=0.1))

        holder.kill()
        holder.wait(timeout=20)
        self.assertTrue(instance.wait_until_stopped(5.0, poll_s=0.1))


class TheAppStopsWhenAsked(TempState):
    """The other half of `dictate stop`: the running app noticing.

    `Application` cannot be built off Windows - the platform factory refuses,
    deliberately - so the watch loop is driven on its own, with the four things
    it touches supplied. That is enough to hold the part that matters: a request
    reaches `overlay.close()`, which is what ends the Tk loop and takes the
    whisper-server child down cleanly with it.
    """

    def _watcher(self):
        import threading

        from dictate.app import Application

        from . import fakes

        app = object.__new__(Application)
        app._stopping = threading.Event()
        app._started_at = time.time()
        app.console = lambda _msg="": None
        app.overlay = fakes.FakeOverlay()
        thread = threading.Thread(target=app._stop_request_loop, daemon=True)
        thread.start()
        self.addCleanup(app._stopping.set)
        return app, thread

    def test_a_request_brings_the_app_down_cleanly(self):
        app, _thread = self._watcher()
        self.assertFalse(app.overlay.closed.is_set())
        instance.request_stop()
        self.assertTrue(app.overlay.closed.wait(10.0))
        # And it clears up after itself, so the next start is not stopped by it.
        self.assertFalse(instance.stop_request_path().exists())

    def test_a_request_from_before_it_started_is_left_alone(self):
        instance.request_stop(now=time.time() - 3600)
        app, _thread = self._watcher()
        self.assertFalse(app.overlay.closed.wait(2.0))


class StopFromTheCommandLine(TempState):
    """`dictate stop` against a real process holding the lock. It cannot bring
    down a whole app here - there is no Windows - but it must find it, ask, and
    report honestly when it is not obeyed."""

    def test_it_says_so_when_nothing_is_running(self):
        code, out = _run_cli(["stop"])
        self.assertEqual(code, 0)
        self.assertIn("not running", out)

    def test_it_reports_a_copy_that_will_not_go(self):
        holder = spawn("hold", instance.lock_path(), "30")
        self.addCleanup(holder.kill)
        self.assertEqual(wait_for_line(holder), "HELD")

        code, out = _run_cli(["stop", "--timeout", "1"])
        self.assertEqual(code, 1)
        self.assertIn("asking dictate to stop", out)
        self.assertIn("did not stop", out)
        self.assertIn("whisper-server.exe", out)  # it has to go too
        self.assertNotIn("Traceback", out)


def _run_cli(argv: list[str]) -> tuple[int, str]:
    import contextlib
    import io

    from dictate import cli

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue() + err.getvalue()


if __name__ == "__main__":
    unittest.main()
