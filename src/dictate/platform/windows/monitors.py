"""Which monitor the captions belong on, and how big a pixel is there.

He works across several displays and does not want to look left and right while
he is talking, so the overlay follows the work rather than the primary display.
The rule, in order:

1. **The monitor holding the window captured at hotkey press.** That is where
   the finished text is going to be pasted, so it is where he is looking. It is
   also the handle the pipeline already captures at press for exactly this kind
   of reason - see `Pipeline._capture_target`.
2. **The monitor holding the mouse pointer**, if there was no usable target
   window (no window focused, or it closed while he was speaking).
3. **The primary monitor**, if even that fails.

Then the DPI. Displays can have different scaling, and a caption laid out in raw
pixels on a 100% display is two thirds the size on a 150% one. So the process
declares itself per-monitor DPI aware, every measurement in `[overlay]` is read
as a value at 100%, and `plan_slab` multiplies by this monitor's scale.

Declaring awareness matters twice over: without it Windows silently *virtualises*
coordinates for this process, so the monitor rectangles reported here and the
rectangle Tk is asked to occupy stop being in the same coordinate space, and the
overlay lands in the wrong place on a mixed-DPI desktop. It has to happen before
any window exists, which is why `set_dpi_awareness()` is called at the top of
`run_forever()`.

None of this touches focus. `MonitorFromWindow`, `GetMonitorInfoW`,
`GetCursorPos` and `GetDpiForMonitor` are all read-only queries, and
`SetProcessDpiAwarenessContext` is a process-wide setting that creates and
activates nothing.
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes
from dataclasses import dataclass

from .win32 import (
    DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE,
    DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2,
    DPI_AWARENESS_CONTEXT_SYSTEM_AWARE,
    MDT_EFFECTIVE_DPI,
    MONITOR_DEFAULTTONEAREST,
    MONITOR_DEFAULTTOPRIMARY,
    MONITORINFO,
    MONITORINFOF_PRIMARY,
    PROCESS_PER_MONITOR_DPI_AWARE,
    PROCESS_SYSTEM_DPI_AWARE,
    USER_DEFAULT_SCREEN_DPI,
    user32,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Monitor:
    """One display, in virtual-desktop coordinates."""

    #: The usable area - the whole monitor minus the taskbar and any app bars.
    work: tuple[int, int, int, int]
    #: The whole monitor, kept for the log line.
    bounds: tuple[int, int, int, int]
    dpi: int = USER_DEFAULT_SCREEN_DPI
    primary: bool = False
    #: How the monitor was chosen: "window", "cursor" or "primary".
    picked_by: str = "primary"

    @property
    def scale(self) -> float:
        return self.dpi / USER_DEFAULT_SCREEN_DPI

    def __str__(self) -> str:
        left, top, right, bottom = self.bounds
        return (f"{right - left}x{bottom - top} at ({left},{top}), "
                f"{round(self.scale * 100)}% scaling"
                f"{', primary' if self.primary else ''} [by {self.picked_by}]")


def set_dpi_awareness(mode: str) -> str:
    """Declare this process's DPI awareness. Returns what was actually obtained.

    Must be called before any window is created. Each route is tried in turn
    because the newest one only exists from Windows 10 1703 and this has to keep
    working on older builds rather than refusing to draw captions on them.
    """
    if mode == "off":
        return "off"

    if mode == "per-monitor":
        contexts = [("per-monitor-v2", DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2),
                    ("per-monitor", DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE),
                    ("system", DPI_AWARENESS_CONTEXT_SYSTEM_AWARE)]
    else:
        contexts = [("system", DPI_AWARENESS_CONTEXT_SYSTEM_AWARE)]

    setter = getattr(user32, "SetProcessDpiAwarenessContext", None)
    if setter is not None:
        setter.argtypes = (wintypes.HANDLE,)
        setter.restype = wintypes.BOOL
        for name, context in contexts:
            try:
                if setter(context):
                    return name
            except OSError:  # pragma: no cover - Windows only
                log.debug("SetProcessDpiAwarenessContext(%s) raised", name,
                          exc_info=True)

    # Windows 8.1 route.
    try:
        shcore = ctypes.WinDLL("shcore")
    except OSError:
        shcore = None
    if shcore is not None:
        wanted = (PROCESS_PER_MONITOR_DPI_AWARE if mode == "per-monitor"
                  else PROCESS_SYSTEM_DPI_AWARE)
        try:
            # S_OK, or E_ACCESSDENIED when something already set it - which is
            # still "we are aware", so it is not treated as a failure.
            result = shcore.SetProcessDpiAwareness(wanted)
            if result in (0, -2147024891):
                return "per-monitor" if wanted == PROCESS_PER_MONITOR_DPI_AWARE else "system"
        except OSError:  # pragma: no cover - Windows only
            log.debug("SetProcessDpiAwareness failed", exc_info=True)

    # Vista route. System-wide awareness only, but better than virtualised.
    try:
        if user32.SetProcessDPIAware():
            return "system"
    except OSError:  # pragma: no cover - Windows only
        log.debug("SetProcessDPIAware failed", exc_info=True)
    return "unaware"


def _dpi_for(handle) -> int:
    """This monitor's effective DPI, or 96 if Windows will not say."""
    try:
        shcore = ctypes.WinDLL("shcore")
        get_dpi = shcore.GetDpiForMonitor
    except (OSError, AttributeError):
        return USER_DEFAULT_SCREEN_DPI
    # Declared rather than left to ctypes' defaults: a monitor handle is
    # pointer-sized, and an undeclared argument is passed as a C int, which
    # silently truncates it to 32 bits on 64-bit Windows.
    get_dpi.argtypes = (wintypes.HANDLE, ctypes.c_int,
                        ctypes.POINTER(wintypes.UINT),
                        ctypes.POINTER(wintypes.UINT))
    get_dpi.restype = ctypes.c_long          # HRESULT
    dpi_x = wintypes.UINT()
    dpi_y = wintypes.UINT()
    try:
        if get_dpi(handle, MDT_EFFECTIVE_DPI,
                   ctypes.byref(dpi_x), ctypes.byref(dpi_y)) != 0:
            return USER_DEFAULT_SCREEN_DPI
    except (OSError, ctypes.ArgumentError):  # pragma: no cover - Windows only
        return USER_DEFAULT_SCREEN_DPI
    return int(dpi_x.value) or USER_DEFAULT_SCREEN_DPI


def _describe(handle, picked_by: str) -> Monitor | None:
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    if not user32.GetMonitorInfoW(handle, ctypes.byref(info)):
        return None
    work = (info.rcWork.left, info.rcWork.top, info.rcWork.right, info.rcWork.bottom)
    bounds = (info.rcMonitor.left, info.rcMonitor.top,
              info.rcMonitor.right, info.rcMonitor.bottom)
    if work[2] <= work[0] or work[3] <= work[1]:
        work = bounds
    return Monitor(work=work, bounds=bounds, dpi=_dpi_for(handle),
                   primary=bool(info.dwFlags & MONITORINFOF_PRIMARY),
                   picked_by=picked_by)


def monitor_for_window(hwnd: int | None) -> Monitor | None:
    """The monitor holding `hwnd`, or None if there is no usable window."""
    if not hwnd:
        return None
    try:
        if not user32.IsWindow(wintypes.HWND(hwnd)):
            return None
        handle = user32.MonitorFromWindow(wintypes.HWND(hwnd),
                                          MONITOR_DEFAULTTONEAREST)
    except (OSError, ctypes.ArgumentError):  # pragma: no cover - Windows only
        return None
    return _describe(handle, "window") if handle else None


def monitor_for_cursor() -> Monitor | None:
    point = wintypes.POINT()
    try:
        if not user32.GetCursorPos(ctypes.byref(point)):
            return None
        handle = user32.MonitorFromPoint(point, MONITOR_DEFAULTTONEAREST)
    except OSError:  # pragma: no cover - Windows only
        return None
    return _describe(handle, "cursor") if handle else None


def primary_monitor() -> Monitor | None:
    handle = user32.MonitorFromPoint(wintypes.POINT(0, 0), MONITOR_DEFAULTTOPRIMARY)
    return _describe(handle, "primary") if handle else None


def pick_monitor(hwnd: int | None) -> Monitor | None:
    """The display the captions belong on. The rule is in this module's docstring."""
    for find in (lambda: monitor_for_window(hwnd), monitor_for_cursor, primary_monitor):
        try:
            found = find()
        except Exception:  # pragma: no cover - Windows only
            log.debug("a monitor lookup raised; trying the next one", exc_info=True)
            continue
        if found is not None:
            return found
    return None
