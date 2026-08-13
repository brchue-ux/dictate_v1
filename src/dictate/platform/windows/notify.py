"""One message box, for one job: a start that failed at logon.

When dictate starts itself at logon it runs under `pythonw.exe` and has no
console. Everything it does is written to `autostart.log`, but a log only helps
someone who knows to look at it - and the whole point of the logon start is that
he never thinks about dictate at all. So the one case where nobody is watching -
it could not start, and he is about to press the hotkey and get nothing - also
puts a dialog on screen.

This is used from nowhere else. It is deliberately the plainest Win32 call there
is, and the caller wraps it, because a failure to *report* a failure must not
become a second failure.
"""

from __future__ import annotations

import ctypes
import sys

if sys.platform != "win32":  # pragma: no cover - guarded by the caller
    raise ImportError("dictate.platform.windows.notify requires Windows")

MB_OK = 0x00000000
MB_ICONERROR = 0x00000010
#: Bring it in front of whatever is there. At logon that may be nothing at all.
MB_SETFOREGROUND = 0x00010000
MB_TOPMOST = 0x00040000


def show_error(title: str, text: str) -> None:
    """Show a modal error dialog. Returns when the user dismisses it."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.MessageBoxW(
        None, str(text), str(title), MB_OK | MB_ICONERROR | MB_SETFOREGROUND | MB_TOPMOST,
    )
