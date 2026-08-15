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
        process_id = _process_id(hwnd)
        return TargetWindow(
            handle=int(hwnd),
            title=_window_title(hwnd),
            process=_process_name(process_id),
            process_id=process_id,
        )

    def exists(self, target: TargetWindow) -> bool:
        """`IsWindow` on the captured handle. Read-only; activates nothing.

        Asked when the paste is about to be held or while a deferred delivery
        waits. A handle can be re-used by a later window, so two known process
        ids must still agree. An unreadable id remains unknown, not "closed".
        """
        try:
            hwnd = wintypes.HWND(target.handle)
            if not user32.IsWindow(hwnd):
                return False
            current_process_id = _process_id(hwnd)
            return (not target.process_id or not current_process_id
                    or current_process_id == target.process_id)
        except Exception:
            log.debug("could not ask whether %s still exists", target, exc_info=True)
            return True

    def focus(self, target: TargetWindow) -> bool:
        """Try to bring `target` to the foreground on the ordinary paste path.

        This is best-effort only. Attaching input queues lets the threads share
        input state; it does not grant permission to bypass Windows'
        foreground-lock rules, so SetForegroundWindow may still refuse. The
        deferred focus-change route deliberately never calls this method.
        """
        hwnd = wintypes.HWND(target.handle)
        if not self.exists(target):
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


def _process_id(hwnd) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _process_name(process_id: int) -> str:
    if not process_id:
        return ""
    handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, process_id)
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
