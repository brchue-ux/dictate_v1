"""Supervised child process.

The whisper.cpp server holds ~1.6 GB of model in VRAM and must stay up between
utterances - reloading it per utterance costs ~2 s and blows the latency budget
on its own. So the app owns that process's whole life: it starts it, waits for it
to report healthy, watches it, restarts it if it dies, and shuts it down cleanly
on exit.

**And it owns the end of that life even when it does not get to run.** Every
child started here is handed to a `platform.base.ChildGuard` the moment it
exists, which on Windows puts it in a job object that Windows itself empties
when this process ends - however it ends. Ctrl+C answered at the "Terminate
batch job" prompt used to leave whisper-server holding the transcription port
forever; nothing below that line runs in that case, and that is precisely why
the containment cannot be code that runs here. See `platform/windows/job.py`.

This module is otherwise deliberately generic and free of anything
whisper-specific, so it can be tested on Linux against a stand-in server:
`tests/test_engines.py` starts a real child process, kills it, and asserts it
comes back, and asserts that every spawn - the first, a restart, and a reload
after an idle release - is handed to the guard.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence

from ..errors import BackendUnavailableError, DictateError

log = logging.getLogger(__name__)

#: Lines of child output kept for diagnostics. whisper.cpp is chatty at startup
#: and the useful line (which backend it chose) is near the top, so keep plenty.
LOG_TAIL_LINES = 200


def _platform_guard():
    """The operating system's own way of ending our children with us, or `None`.

    On Windows this is a job object, and it is what makes the orphaned
    whisper-server impossible. On the Linux and macOS machines this project is
    developed on there is no equivalent in dictate, and this returns `None`
    rather than something that would look like containment and provide none -
    the product does not run here, and the tests that care inject a guard of
    their own. What the real one does is proved on CI's Windows runners, by
    killing a parent without letting it clean up.
    """
    if sys.platform != "win32":
        return None
    from ..platform import factory  # noqa: PLC0415

    try:
        return factory.make_child_guard()
    except DictateError:
        log.exception("no child guard on this machine; a whisper-server could "
                      "outlive dictate if it is ever killed")
        return None


class ManagedProcess:
    """A child process that is kept alive for as long as we want it alive."""

    def __init__(
        self,
        argv: Sequence[str],
        *,
        name: str,
        health_check: Callable[[], bool],
        startup_timeout_s: float = 120.0,
        max_restarts: int = 3,
        poll_interval_s: float = 0.4,
        restart_backoff_s: float = 1.0,
        #: A restart budget that has held healthy this long is considered spent
        #: on a transient problem and is given back.
        healthy_reset_s: float = 120.0,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        #: Ties each child's life to ours. `None` means "ask the platform", and
        #: the platform answers with nothing on the machines this is developed
        #: on - see `_platform_guard`.
        guard=None,
        notify: Callable[[str, str], None] | None = None,
    ) -> None:
        self.argv = list(argv)
        self.name = name
        self.health_check = health_check
        self.startup_timeout_s = startup_timeout_s
        self.max_restarts = max_restarts
        self.poll_interval_s = poll_interval_s
        self.restart_backoff_s = restart_backoff_s
        self.healthy_reset_s = healthy_reset_s
        self.cwd = cwd
        self.env = env
        self.guard = guard if guard is not None else _platform_guard()
        self.notify = notify

        self._proc: subprocess.Popen[str] | None = None
        self._lock = threading.RLock()
        self._want_running = False
        self._restarts = 0
        self._last_started_at = 0.0
        self._log_tail: deque[str] = deque(maxlen=LOG_TAIL_LINES)
        self._reader: threading.Thread | None = None
        self._monitor: threading.Thread | None = None
        self._stop_event = threading.Event()
        #: Set when the process has died more times than we are willing to fix.
        self.gave_up_reason: str | None = None

    # -- public API ------------------------------------------------------

    def start(self) -> None:
        """Start and wait until healthy. Idempotent. Raises on failure."""
        with self._lock:
            if self._want_running and self.is_running():
                return
            self._want_running = True
            self._stop_event.clear()
            self._restarts = 0
            self.gave_up_reason = None
            self._spawn_and_wait()
            if self._monitor is None or not self._monitor.is_alive():
                self._monitor = threading.Thread(
                    target=self._watch, name=f"{self.name}-monitor", daemon=True
                )
                self._monitor.start()

    def stop(self, timeout_s: float = 10.0) -> None:
        """Shut the child down cleanly. Safe to call more than once."""
        # Signal first, THEN take the lock. The monitor thread can be inside a
        # restart holding the lock for as long as startup_timeout_s; it watches
        # this event and bails out, which is what lets us get the lock at all.
        self._want_running = False
        self._stop_event.set()
        monitor = self._monitor
        if monitor and monitor.is_alive() and monitor is not threading.current_thread():
            monitor.join(timeout=timeout_s)
        self._monitor = None
        self._terminate(timeout_s)

    def is_running(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    def log_tail(self, lines: int = 25) -> str:
        tail = list(self._log_tail)[-lines:]
        return "\n".join(tail)

    @property
    def pid(self) -> int | None:
        proc = self._proc
        return proc.pid if proc is not None and proc.poll() is None else None

    # -- internals -------------------------------------------------------

    def _spawn(self) -> None:
        self._log_tail.clear()
        creationflags = 0
        if sys.platform == "win32":
            # Keep the console window from flashing up on every start.
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self._proc = subprocess.Popen(
                self.argv,
                cwd=self.cwd,
                env={**os.environ, **(self.env or {})} if self.env else None,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                errors="replace",
                creationflags=creationflags,
            )
        except FileNotFoundError as exc:
            raise BackendUnavailableError(
                f"Could not start {self.name}: the program was not found at "
                f"{self.argv[0]}",
                "Check the path in your config file. If you have not built "
                "whisper.cpp yet, run scripts/build-whisper-vulkan.ps1.",
            ) from exc
        except OSError as exc:
            raise BackendUnavailableError(
                f"Could not start {self.name}: {exc}",
                "Check the path in your config file and that the file is not "
                "blocked by Windows or by your antivirus.",
            ) from exc

        self._contain(self._proc)
        self._last_started_at = time.monotonic()
        self._reader = threading.Thread(
            target=self._drain, args=(self._proc,), name=f"{self.name}-log", daemon=True
        )
        self._reader.start()
        log.info("%s started (pid %s)", self.name, self._proc.pid)

    def _contain(self, proc: subprocess.Popen[str]) -> None:
        """Hand the new child to the guard, before anything else happens to it.

        A guard that refuses does NOT stop the start: the app works perfectly
        well with an unguarded child, it is only the crash case that gets worse.
        But it is said out loud, with the command that clears up afterwards,
        rather than being discovered as a port nothing appears to be using.
        """
        if self.guard is None:
            return
        try:
            self.guard.adopt(proc)
        except DictateError as exc:
            log.error("%s is not contained: %s", self.name, exc.message)
            if self.notify:
                self.notify("warning", exc.report())
        except Exception:
            log.exception("%s could not be handed to the child guard", self.name)

    def _drain(self, proc: subprocess.Popen[str]) -> None:
        stream = proc.stdout
        if stream is None:
            return
        try:
            for line in stream:
                line = line.rstrip("\r\n")
                if line:
                    self._log_tail.append(line)
                    log.debug("[%s] %s", self.name, line)
        except (ValueError, OSError):
            pass  # stream closed under us during shutdown
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def _spawn_and_wait(self) -> None:
        self._spawn()
        deadline = time.monotonic() + self.startup_timeout_s
        while time.monotonic() < deadline:
            if not self.is_running():
                code = self._proc.returncode if self._proc else "?"
                raise BackendUnavailableError(
                    f"{self.name} exited immediately (exit code {code}).\n"
                    f"Its last output was:\n{self.log_tail(15)}",
                    "The output above is from whisper.cpp itself and usually says "
                    "what it could not find - most often the model file.",
                )
            try:
                if self.health_check():
                    log.info("%s is healthy after %.1fs", self.name,
                             time.monotonic() - self._last_started_at)
                    return
            except Exception:  # a health check must never crash the supervisor
                log.debug("%s health check raised", self.name, exc_info=True)
            if self._stop_event.wait(self.poll_interval_s):
                return
        self._terminate(5.0)
        raise BackendUnavailableError(
            f"{self.name} did not become ready within {self.startup_timeout_s:.0f} "
            f"seconds.\nIts last output was:\n{self.log_tail(15)}",
            "On the very first run this can be slow because the graphics driver "
            "compiles its shaders. If it happens every time, raise "
            "startup_timeout_s in your config, or check the output above.",
        )

    def _watch(self) -> None:
        while not self._stop_event.wait(self.poll_interval_s):
            with self._lock:
                if not self._want_running:
                    return
                if self.is_running():
                    if (self._restarts
                            and time.monotonic() - self._last_started_at > self.healthy_reset_s):
                        log.info("%s has been stable; restart budget reset", self.name)
                        self._restarts = 0
                    continue
                code = self._proc.returncode if self._proc else "?"
                tail = self.log_tail(15)
                if self._restarts >= self.max_restarts:
                    self.gave_up_reason = (
                        f"{self.name} has crashed {self._restarts + 1} times "
                        f"(last exit code {code}). Its last output was:\n{tail}"
                    )
                    log.error("%s", self.gave_up_reason)
                    self._want_running = False
                    return
                self._restarts += 1
                log.warning(
                    "%s exited unexpectedly (code %s); restart %d of %d",
                    self.name, code, self._restarts, self.max_restarts,
                )
            # Back off outside the lock so a caller can still stop() us.
            if self._stop_event.wait(self.restart_backoff_s * self._restarts):
                return
            with self._lock:
                if not self._want_running:
                    return
                try:
                    self._spawn_and_wait()
                except BackendUnavailableError as exc:
                    log.error("%s failed to restart: %s", self.name, exc.message)
                    if self._restarts >= self.max_restarts:
                        self.gave_up_reason = exc.report()
                        self._want_running = False
                        return

    def _terminate(self, timeout_s: float) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        log.info("stopping %s (pid %s)", self.name, proc.pid)
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            log.warning("%s did not stop when asked; killing it", self.name)
            try:
                proc.kill()
                proc.wait(timeout=timeout_s)
            except (OSError, subprocess.TimeoutExpired):
                log.error("%s could not be killed", self.name)
        reader = self._reader
        if reader and reader.is_alive() and reader is not threading.current_thread():
            reader.join(timeout=2.0)
