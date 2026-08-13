"""The running copy of dictate: where its state lives, the lock that keeps there
being only one of it, and the way to ask it to stop.

**Why a lock at all.** dictate owns two things that cannot be shared: the global
hotkey and whisper-server's TCP port. A second copy takes neither cleanly - the
hotkey goes to whichever hook registered last, and the second whisper-server
either fails to bind or quietly serves a second 1.6 GB model into VRAM. Both
failures look like "dictate is broken" rather than "there are two of them", which
is exactly the kind of baffling that this project is not allowed to produce. So
the second copy is refused, by name, before it takes anything.

**How.** An exclusive byte-range lock on a file in the state directory, held for
as long as the process lives. The operating system releases it when the process
ends - including when it is killed - so there is no such thing as a stale lock
here, and nothing has to guess whether a recorded pid is still alive. The
identity of the holder (pid, when it started, whether it was started at logon or
by hand) is written into the *front* of the same file, outside the locked region,
so a second copy can read who has it while being refused.

This module is plain standard library and works on Windows and on Linux, which is
what lets the guard be tested for real on the machines this project is developed
on as well as on the machine it runs on. It is not a stand-in for a Windows
feature: the same file lock is what runs on Windows.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .errors import AlreadyRunningError, DictateError

#: The byte that is locked. It is deliberately far past the end of the file, for
#: one reason: Windows byte-range locks are mandatory, so locking byte 0 would
#: also stop a second copy READING the record that says who the holder is. A
#: region beyond the end of the file may be locked on both platforms, and leaves
#: the record itself readable.
LOCK_OFFSET = 1_000_000

#: The record at the front of the lock file is written to a fixed width so that
#: it can be overwritten in place. Nothing truncates the file while it is locked.
RECORD_BYTES = 512

LOCK_NAME = "dictate.lock"
STOP_REQUEST_NAME = "stop-request"

#: Windows opens file descriptors in TEXT mode by default, which would rewrite
#: the newlines in the fixed-width record on the way in and out and make it stop
#: being fixed-width. There is no such flag anywhere else.
_BINARY = getattr(os, "O_BINARY", 0)


def state_dir() -> Path:
    """Where the running copy keeps its lock, its logs and its stop request.

    Per user, not per machine, and deliberately not the config directory: this is
    state dictate writes about itself, and none of it is anything to edit.
    """
    override = os.environ.get("DICTATE_STATE_DIR")
    if override:
        return Path(override).expanduser()
    for var in ("LOCALAPPDATA", "APPDATA"):
        base = os.environ.get(var)
        if base:
            return Path(base) / "dictate"
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / "dictate"
    return Path.home() / ".local" / "state" / "dictate"


def lock_path() -> Path:
    return state_dir() / LOCK_NAME


def stop_request_path() -> Path:
    return state_dir() / STOP_REQUEST_NAME


def _ensure_dir(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DictateError(
            f"dictate could not create the folder it keeps its state in: {path.parent} ({exc})",
            "Check that folder is writable, or set DICTATE_STATE_DIR to one that is.",
        ) from exc


# ---------------------------------------------------------------------------
# The record at the front of the lock file
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Holder:
    """Who is holding the lock. Every field is optional on purpose: the lock
    itself is the truth about whether a copy is running, and this record is only
    how that copy describes itself. A `Holder` with nothing in it still means
    "yes, something is running"."""

    pid: int | None = None
    started_at: str = ""
    started_epoch: float = 0.0
    #: "logon" (started automatically) or "hand" (someone typed `dictate run`).
    started_by: str = ""

    def describe(self) -> str:
        parts = []
        if self.pid:
            parts.append(f"process {self.pid}")
        if self.started_at:
            parts.append(f"started {self.started_at}")
        if self.started_by == "logon":
            parts.append("started automatically when you logged in")
        elif self.started_by == "hand":
            parts.append("started by hand")
        return ", ".join(parts) if parts else "no details recorded"


def _format_record(pid: int, started_by: str, epoch: float) -> bytes:
    text = (
        f"pid={pid}\n"
        f"started={time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(epoch))}\n"
        f"started_epoch={epoch:.0f}\n"
        f"started_by={started_by}\n"
    )
    return text.encode("utf-8", "replace").ljust(RECORD_BYTES, b" ")


def parse_record(raw: str) -> Holder:
    """Turn the front of a lock file back into a `Holder`. Anything unreadable
    becomes an empty field rather than an error - a lock we cannot describe is
    still a lock."""
    fields: dict[str, str] = {}
    for line in raw.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            fields[key.strip()] = value.strip()
    pid: int | None = None
    try:
        pid = int(fields.get("pid", ""))
    except ValueError:
        pid = None
    try:
        epoch = float(fields.get("started_epoch", ""))
    except ValueError:
        epoch = 0.0
    return Holder(
        pid=pid,
        started_at=fields.get("started", ""),
        started_epoch=epoch,
        started_by=fields.get("started_by", ""),
    )


def read_record(path: Path | None = None) -> Holder | None:
    """Read the record without touching the lock. `None` if there is no file.

    This says who *last* held the lock, not whether anyone holds it now - use
    `running_instance()` for that.
    """
    path = path or lock_path()
    try:
        with open(path, "rb") as handle:
            raw = handle.read(RECORD_BYTES)
    except OSError:
        return None
    return parse_record(raw.decode("utf-8", "replace"))


# ---------------------------------------------------------------------------
# The lock
# ---------------------------------------------------------------------------


def _lock_region(fd: int, *, blocking: bool = False) -> None:
    """Take the exclusive lock, or raise OSError. Platform-specific in exactly
    this one function."""
    if sys.platform == "win32":
        import msvcrt  # noqa: PLC0415 - Windows only, by design

        os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
        mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
        msvcrt.locking(fd, mode, 1)
    else:
        import fcntl  # noqa: PLC0415 - everywhere but Windows

        flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        fcntl.flock(fd, flags)


def _unlock_region(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt  # noqa: PLC0415

        os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl  # noqa: PLC0415

        fcntl.flock(fd, fcntl.LOCK_UN)


class InstanceLock:
    """Held for the whole life of a running dictate. One per user.

    Use it as a context manager; `acquire()` raises `AlreadyRunningError` naming
    the copy that already has it.
    """

    def __init__(self, path: Path | None = None, *, started_by: str = "hand") -> None:
        self.path = path or lock_path()
        self.started_by = started_by
        self._fd: int | None = None

    def acquire(self) -> None:
        _ensure_dir(self.path)
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | _BINARY, 0o644)
        except OSError as exc:
            raise DictateError(
                f"dictate could not open its lock file {self.path}: {exc}",
                "Check that folder is writable, or set DICTATE_STATE_DIR to one that is.",
            ) from exc
        try:
            _lock_region(fd)
        except OSError:
            os.close(fd)
            holder = read_record(self.path) or Holder()
            raise AlreadyRunningError(
                f"dictate is already running ({holder.describe()}).",
                "Only one copy can run at a time - two would fight over the "
                "hotkey and over the transcription port.\n"
                "  To see it:    dictate autostart status\n"
                "  To stop it:   dictate stop",
                holder=holder,
            ) from None
        self._fd = fd
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, _format_record(os.getpid(), self.started_by, time.time()))
        except OSError:
            # The lock is what matters and we have it. A record we could not
            # write costs a detail in a message, not correctness.
            pass

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _unlock_region(fd)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


def running_instance(path: Path | None = None) -> Holder | None:
    """The copy of dictate that is running now, or `None` if there is not one.

    Answered by trying to take the lock rather than by reading a pid, so a lock
    file left behind by a process that died is never mistaken for a running copy.
    """
    path = path or lock_path()
    if not path.exists():
        return None
    try:
        fd = os.open(path, os.O_RDWR | _BINARY)
    except OSError:
        return None
    try:
        _lock_region(fd)
    except OSError:
        return read_record(path) or Holder()
    else:
        _unlock_region(fd)
        return None
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Asking the running copy to stop
#
# Not a kill: dictate owns a whisper-server child holding ~1.6 GB of VRAM and a
# TCP port, and killing the parent orphans it. So `dictate stop` leaves a request
# and the running copy shuts itself down the same way Ctrl+C does.
# ---------------------------------------------------------------------------


def request_stop(path: Path | None = None, *, now: float | None = None) -> Path:
    """Ask the running copy to shut down cleanly. Returns the request path."""
    path = path or stop_request_path()
    _ensure_dir(path)
    stamp = time.time() if now is None else now
    path.write_text(
        f"epoch={stamp:.3f}\n"
        f"written={time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stamp))}\n"
        "This file asks a running dictate to stop. It deletes itself.\n",
        encoding="utf-8",
    )
    return path


def clear_stop_request(path: Path | None = None) -> None:
    path = path or stop_request_path()
    try:
        path.unlink()
    except OSError:
        pass


def stop_requested(path: Path | None = None, *, since: float = 0.0) -> bool:
    """Is there a stop request meant for a copy that started at `since`?

    A request written before this copy started was meant for a previous one, and
    honouring it would make dictate exit the moment it started - which is exactly
    the sort of inexplicable behaviour this project tries not to ship.
    """
    path = path or stop_request_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return False
    when = 0.0
    for line in raw.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "epoch":
            try:
                when = float(value.strip())
            except ValueError:
                when = 0.0
    # No readable timestamp: honour it. It was put there deliberately, and the
    # file is deleted on startup, so it cannot be older than this copy.
    if when == 0.0:
        return True
    return when >= since - 1.0


def wait_until_stopped(timeout_s: float = 20.0, *, path: Path | None = None,
                       poll_s: float = 0.25) -> bool:
    """Wait for the running copy to let go of the lock. True if it did."""
    deadline = time.monotonic() + timeout_s
    while True:
        if running_instance(path) is None:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_s)
