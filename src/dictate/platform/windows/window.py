"""Which window had focus, and putting focus back on it.

Constraint 2 of the build brief, restated: the window handle is read at hotkey
*press*. Between press and paste the overlay appears, a notification may pop, and
the user may alt-tab. Reading the foreground window at paste time would send the
text wherever focus happened to land.
"""

from __future__ import annotations

import ctypes
import logging
import time
from ctypes import wintypes

from ..base import TargetWindow
from .win32 import kernel32, user32, PROCESS_QUERY_LIMITED_INFORMATION

log = logging.getLogger(__name__)


class WindowsWindowTracker:
    """Implements `platform.base.WindowTracker`."""

    def foreground(self) -> TargetWindow | None:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        return TargetWindow(
            handle=int(hwnd),
            title=_window_title(hwnd),
            process=_process_name(hwnd),
        )

    def exists(self, target: TargetWindow) -> bool:
        """`IsWindow` on the captured handle. Read-only; activates nothing.

        Asked when the paste is about to be held, so that "it has closed" and
        "you moved" are told apart rather than guessed at. A handle can be
        re-used by a later window, which would make this say yes about a
        different window - that costs a sentence's accuracy and never an action,
        because nothing is pasted either way (`delivery.decide`).
        """
        try:
            return bool(user32.IsWindow(wintypes.HWND(target.handle)))
        except Exception:
            log.debug("could not ask whether %s still exists", target, exc_info=True)
            return True

    def focus(self, target: TargetWindow) -> bool:
        """Bring `target` to the foreground if it is not already there.

        SetForegroundWindow refuses when the calling process does not own the
        foreground window - a deliberate Windows anti-focus-stealing rule. The
        documented way round it, and the one every automation tool uses, is to
        attach our input queue to the foreground thread's for the duration of
        the call. Ugly, but it is the supported mechanism.
        """
        hwnd = wintypes.HWND(target.handle)
        if not user32.IsWindow(hwnd):
            log.warning("target window %s no longer exists", target)
            return False
        current = user32.GetForegroundWindow()
        if current and int(current) == target.handle:
            return True
        if user32.IsIconic(hwnd):
            log.warning("target window %s is minimised; not forcing it open", target)
            return False

        our_thread = kernel32.GetCurrentThreadId()
        their_thread = user32.GetWindowThreadProcessId(current, None) if current else 0
        attached = False
        if their_thread and their_thread != our_thread:
            attached = bool(user32.AttachThreadInput(our_thread, their_thread, True))
        try:
            user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(our_thread, their_thread, False)

        # SetForegroundWindow is asynchronous; give the switch a moment to land.
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            now = user32.GetForegroundWindow()
            if now and int(now) == target.handle:
                return True
            time.sleep(0.02)
        log.warning("could not restore focus to %s", target)
        return False


def _window_title(hwnd) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _process_name(hwnd) -> str:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1]
    finally:
        kernel32.CloseHandle(handle)
    return ""
