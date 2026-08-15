"""`python -m dictate`.

The logon task reaches the package through ``pythonw.exe -m dictate``.  That
interpreter has no console, so the ordinary Python traceback has nowhere to go.
The ``--autostart`` bootstrap below is deliberately earlier and smaller than
the CLI: it puts a file behind stdout and stderr before importing the CLI at
all.  A failure in argument parsing or in one of the CLI's imports therefore
cannot disappear before :func:`dictate.autostart.run_at_logon` opens its own
log block.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Callable

# These three strings are shared in meaning with ``autostart.py`` but live here
# as literals on purpose.  Importing that module before the file is open would
# put all of its imports back inside the silent gap this entry point closes.
_LOG_NAME = "autostart.log"
_BLOCK_MARK = "=== dictate autostart"
_PID_MARK = "dictate starting at logon (pid "


def _state_dir() -> Path:
    """Resolve the state folder without importing any other dictate module."""
    override = os.environ.get("DICTATE_STATE_DIR")
    if override:
        return Path(override).expanduser()
    for variable in ("LOCALAPPDATA", "APPDATA"):
        base = os.environ.get(variable)
        if base:
            return Path(base) / "dictate"
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / "dictate"
    return Path.home() / ".local" / "state" / "dictate"


def _write(handle, message: str = "") -> None:
    """Best-effort bootstrap logging; reporting a fault cannot become one."""
    try:
        stamp = time.strftime("%H:%M:%S")
        for line in str(message).splitlines() or [""]:
            handle.write(f"{stamp}  {line}\n")
        handle.flush()
    except (OSError, ValueError):
        pass


def _exit_code(value: object) -> int:
    if value is None:
        return 0
    return value if isinstance(value, int) else 1


def _windowless_main(
    argv: list[str] | None = None,
    *,
    dispatch: Callable[[list[str] | None], int] | None = None,
) -> int:
    """Run the CLI with a durable stream in place before the CLI is imported.

    ``dispatch`` is the test seam.  Production deliberately imports ``cli``
    only after the file is open and both standard streams point at it.
    """
    arguments = list(sys.argv[1:] if argv is None else argv)
    path = _state_dir() / _LOG_NAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a", encoding="utf-8", errors="replace", buffering=1)
    except OSError:
        # There is no console to report this through.  Returning failure is the
        # only honest outcome when the one durable destination cannot be opened.
        return 1

    saved = sys.stdout, sys.stderr
    sys.stdout = handle
    sys.stderr = handle
    try:
        _write(handle)
        _write(handle, f"{_BLOCK_MARK} entry {time.strftime('%Y-%m-%d %H:%M:%S')} ===")
        _write(handle, f"{_PID_MARK}{os.getpid()})")
        _write(handle, "console-less package entry reached before the CLI import")
        _write(handle, f"interpreter: {sys.executable}")
        _write(handle, f"arguments: {arguments!r}")
        try:
            _write(handle, f"working directory: {Path.cwd()}")
        except OSError as exc:
            _write(handle, f"working directory: could not be read ({exc})")
        _write(handle, f"state log: {path}")

        try:
            if dispatch is None:
                from .cli import main as dispatch  # noqa: PLC0415 - after logging
            return dispatch(arguments)
        except SystemExit as exc:
            code = _exit_code(exc.code)
            _write(handle, f"outcome: package entry stopped before returning (code {code}).")
            return code
        except BaseException as exc:  # noqa: BLE001 - pythonw has nowhere else
            import traceback  # noqa: PLC0415 - even this is after the file exists

            _write(handle, "outcome: package entry failed before the logon start could report.")
            _write(handle, f"{type(exc).__name__}: {exc}")
            _write(handle, traceback.format_exc())
            return 1
    finally:
        sys.stdout, sys.stderr = saved
        try:
            handle.close()
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if "--autostart" in arguments:
        return _windowless_main(list(arguments))
    from .cli import main as cli_main  # noqa: PLC0415 - ordinary console path

    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
