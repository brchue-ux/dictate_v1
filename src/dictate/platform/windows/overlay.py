"""The caption overlay.

**The hard constraint, restated because it is the one that breaks the product:**
this window must never take focus. If it does, the window the user was typing
into stops being the foreground window, and the finished text is pasted into the
wrong place. So:

* The window is created with `WS_EX_NOACTIVATE`, which tells Windows never to
  activate it - not when it is shown, not when it is clicked.
* It is shown with `ShowWindow(SW_SHOWNOACTIVATE)` and positioned with
  `SetWindowPos(..., SWP_NOACTIVATE)`. Tk's own `deiconify()` is deliberately
  not used, because it goes through `SW_SHOW`.
* `WS_EX_TRANSPARENT` makes it click-through, so a click aimed at whatever is
  underneath goes there and never reaches this window at all.
* `WS_EX_TOOLWINDOW` keeps it off the taskbar and out of Alt-Tab.
* Nothing in this file calls `SetForegroundWindow`, `SetFocus`, `focus_force`,
  `grab_set` or `lift()`.

Tkinter is used for the drawing because it is in the Python standard library, so
there is no extra install for the product owner, and because the window it makes
is a normal HWND whose extended style we can then correct with the four flags
above. `PinW/whisper-key-local` (MIT) was read as prior art for the surrounding
app shape - it uses a system tray and a terminal UI rather than an overlay, so
there was no overlay implementation there to follow, and this window is built
from the Win32 primitives directly.

Tk's own rule applies: every Tk call has to happen on the thread that created the
widgets. `set_state()` is therefore thread-safe by queueing, and a poller running
on the UI thread applies the change.
"""

from __future__ import annotations

import logging
import queue
import threading

from ...config import OverlayConfig
from ..base import OverlayState
from ..geometry import overlay_geometry

log = logging.getLogger(__name__)

#: How long a finished/failed message stays on screen before the overlay hides.
DONE_LINGER_MS = 900
ERROR_LINGER_MS = 6000

_ERROR_COLOUR = "#ff8a8a"

_LABELS = {
    OverlayState.LISTENING: ("● listening", "accent"),
    OverlayState.THINKING: ("● transcribing…", "accent"),
    OverlayState.DONE: ("✓ pasted", "muted"),
    OverlayState.ERROR: ("✕ dictate", "error"),
}


class TkCaptionOverlay:
    """Implements `platform.base.CaptionOverlay`.

    Lifecycle: build on the main thread, call `run_forever()` on the main thread,
    call `set_state()` from anywhere, call `close()` to end `run_forever()`.
    """

    def __init__(self, cfg: OverlayConfig) -> None:
        self.cfg = cfg
        self._queue: queue.Queue[tuple[OverlayState, str]] = queue.Queue()
        self._root = None
        self._label = None
        self._status = None
        self._hwnd = 0
        self._visible = False
        self._hide_job = None
        self._closing = threading.Event()
        self._ready = threading.Event()

    # -- public, thread-safe ---------------------------------------------

    def set_state(self, state: OverlayState, text: str = "") -> None:
        self._queue.put((state, text))

    def close(self) -> None:
        self._closing.set()

    def wait_ready(self, timeout: float = 10.0) -> bool:
        return self._ready.wait(timeout)

    # -- UI thread -------------------------------------------------------

    def run_forever(self) -> None:
        """Build the window and run the Tk loop. Blocks. Main thread only."""
        import tkinter as tk  # noqa: PLC0415 - stdlib, but only wanted on Windows

        root = tk.Tk()
        self._root = root
        root.withdraw()
        root.overrideredirect(True)          # no title bar, no window chrome
        root.attributes("-topmost", True)
        root.attributes("-alpha", self.cfg.opacity)
        root.configure(bg=self.cfg.background)
        try:
            root.attributes("-toolwindow", True)
        except tk.TclError:
            pass  # not available on every Tk build; the ex-style below covers it

        frame = tk.Frame(root, bg=self.cfg.background, padx=18, pady=12)
        frame.pack(fill="both", expand=True)
        self._status = tk.Label(
            frame, text="", bg=self.cfg.background, fg=self.cfg.accent,
            font=(self.cfg.font_family, max(9, self.cfg.font_size - 7)), anchor="w",
        )
        self._status.pack(fill="x", anchor="w")
        self._label = tk.Label(
            frame, text="", bg=self.cfg.background, fg=self.cfg.foreground,
            font=(self.cfg.font_family, self.cfg.font_size), anchor="w",
            justify="left", wraplength=self.cfg.max_width_px - 40,
        )
        self._label.pack(fill="x", anchor="w")

        # Getting the window into "Tk thinks it is mapped, Windows has it
        # hidden" without a visible flash, so that from here on visibility is
        # ours alone (ShowWindow) and Tk never tries to re-map or activate it:
        #
        #   1. fully transparent, so nothing can appear on screen
        #   2. realise the HWND while still withdrawn
        #   3. apply WS_EX_NOACTIVATE and friends BEFORE it is ever mapped
        #   4. map it - now safe, because it is non-activating and invisible
        #   5. hide it at the Win32 level and restore the real opacity
        root.attributes("-alpha", 0.0)
        root.update_idletasks()
        self._apply_window_styles()
        root.deiconify()
        root.update_idletasks()
        self._hide_now()
        root.attributes("-alpha", self.cfg.opacity)

        self._ready.set()
        root.after(30, self._poll)
        try:
            root.mainloop()
        finally:
            self._ready.clear()

    def _apply_window_styles(self) -> None:
        """The four extended styles that make this an overlay rather than a window."""
        from .win32 import (  # noqa: PLC0415 - Windows only, by design
            GWL_EXSTYLE, SWP_FRAMECHANGED, SWP_NOACTIVATE, SWP_NOMOVE, SWP_NOSIZE,
            SWP_NOZORDER, WS_EX_LAYERED, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW,
            WS_EX_TOPMOST, WS_EX_TRANSPARENT, user32,
        )

        raw = self._root.winfo_id()
        # With overrideredirect the Tk widget may still sit inside a wrapper
        # window; the extended style has to go on the real top-level HWND.
        parent = user32.GetParent(raw)
        self._hwnd = int(parent) if parent else int(raw)

        # OR the flags in rather than assigning: Tk's own -alpha has already set
        # WS_EX_LAYERED and the layered attributes, and clobbering that would
        # make the window opaque.
        style = user32.GetWindowLongW(self._hwnd, GWL_EXSTYLE)
        style |= WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_LAYERED
        if self.cfg.click_through:
            style |= WS_EX_TRANSPARENT
        user32.SetWindowLongW(self._hwnd, GWL_EXSTYLE, style)
        # SetWindowLongW alone does not always take effect; the documented way to
        # commit a changed frame style is a SetWindowPos with SWP_FRAMECHANGED.
        user32.SetWindowPos(
            self._hwnd, None, 0, 0, 0, 0,
            SWP_FRAMECHANGED | SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE,
        )
        applied = user32.GetWindowLongW(self._hwnd, GWL_EXSTYLE)
        if not applied & WS_EX_NOACTIVATE:
            # Worth shouting about: without this flag the overlay can take focus,
            # and if it does the finished text is pasted into the wrong window.
            log.error(
                "the caption overlay could not be made non-activating "
                "(ex-style 0x%08X). Captions may steal focus and text may be "
                "pasted into the wrong window - set [captions] enabled = false "
                "if that happens.", applied,
            )
        log.info("caption overlay hwnd=%s ex-style=0x%08X non-activating=%s "
                 "click-through=%s", self._hwnd, applied,
                 bool(applied & WS_EX_NOACTIVATE),
                 bool(applied & WS_EX_TRANSPARENT))

    # -- state application (UI thread) -----------------------------------

    def _poll(self) -> None:
        if self._closing.is_set():
            self._root.quit()
            return
        latest: tuple[OverlayState, str] | None = None
        while True:
            try:
                latest = self._queue.get_nowait()
            except queue.Empty:
                break
        if latest is not None:
            try:
                self._apply(*latest)
            except Exception:
                log.exception("overlay update failed")
        self._root.after(30, self._poll)

    def _apply(self, state: OverlayState, text: str) -> None:
        if self._hide_job is not None:
            self._root.after_cancel(self._hide_job)
            self._hide_job = None

        if state is OverlayState.HIDDEN:
            self._hide()
            return

        label, tone = _LABELS[state]
        colour = {
            "accent": self.cfg.accent,
            "muted": self.cfg.foreground,
            "error": _ERROR_COLOUR,
        }[tone]
        self._status.configure(text=label, fg=colour)
        # The big line carries the caption while listening and the message when
        # something went wrong; it is empty for THINKING and DONE, which is what
        # makes the caption text visibly disappear the moment the key is released.
        self._label.configure(
            text=text,
            fg=_ERROR_COLOUR if state is OverlayState.ERROR else self.cfg.foreground,
        )

        self._show()
        if state is OverlayState.DONE:
            self._hide_job = self._root.after(DONE_LINGER_MS, self._hide)
        elif state is OverlayState.ERROR:
            self._hide_job = self._root.after(ERROR_LINGER_MS, self._hide)

    def _show(self) -> None:
        from .win32 import (  # noqa: PLC0415
            HWND_TOPMOST, SWP_NOACTIVATE, SWP_NOMOVE, SWP_NOSIZE, SW_SHOWNOACTIVATE,
            user32,
        )

        # Let Tk own size and position - it lays the labels out, so it is the one
        # that knows how big the window needs to be.
        self._root.update_idletasks()
        width = min(self.cfg.max_width_px, max(320, self._root.winfo_reqwidth()))
        height = max(48, self._root.winfo_reqheight())
        x, y = overlay_geometry(
            self.cfg.position,
            self._root.winfo_screenwidth(),
            self._root.winfo_screenheight(),
            width, height, self.cfg.margin_px,
        )
        self._root.geometry(f"{width}x{height}+{x}+{y}")

        if not self._visible:
            # Re-assert the extended style: Tk can reset it when it rebuilds a
            # window, and losing WS_EX_NOACTIVATE here is the failure that would
            # send the pasted text to the wrong place.
            self._apply_window_styles()
            # SW_SHOWNOACTIVATE, never Tk's deiconify(), which goes through
            # SW_SHOW and would activate the window. That is the whole point.
            user32.ShowWindow(self._hwnd, SW_SHOWNOACTIVATE)
            self._visible = True
        # Reassert topmost without moving, resizing or activating: another app
        # going full-screen can knock a topmost window down the z-order.
        user32.SetWindowPos(self._hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    def _hide(self) -> None:
        self._hide_job = None
        if self._visible:
            self._hide_now()

    def _hide_now(self) -> None:
        from .win32 import SW_HIDE, user32  # noqa: PLC0415

        user32.ShowWindow(self._hwnd, SW_HIDE)
        self._visible = False
