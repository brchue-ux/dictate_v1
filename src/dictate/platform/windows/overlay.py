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
  `grab_set` or `lift()`. The restyle below added no window operation that can
  activate: `SetWindowRgn` is not used, `SetProcessDpiAwarenessContext` is a
  process-wide setting that creates nothing, and every monitor call in
  `monitors.py` is a read-only query.

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

---

**The look, and the three rules that hold it up.**

The overlay is committed to reading as one heavy object that gets lit, rather
than as a notification that pops. Three values of one dark material - a near
black `edge` around a lighter `face`, sitting on a thicker `plinth` - plus
exactly one block of colour, the `bar`, which is the only saturated thing on
screen and is the whole state indicator at a glance. It is deliberately not
translucent: a partly transparent panel goes muddy over a white document, which
is the condition this thing is looked at in most.

1. **It does not move while it is being read.** Size and position are computed
   once per appearance, in `_show()`, and nothing in the caption path touches
   geometry again. Words arriving must never resize, reposition or re-stack the
   window - that restlessness in the corner of the eye is what "the box is ugly"
   was mostly about. Two lines of caption are reserved whether or not there are
   two lines to show.
2. **Nothing snaps.** Appearing and disappearing are a cubic ease on the
   window's own opacity, out slower than in, resuming from wherever the alpha
   actually is if it is interrupted. See `platform/fade.py`.
3. **The caption thread is never made to wait.** `set_state()` still only
   enqueues. The fade runs on its own `after` chain on the UI thread, holds no
   lock, and does no work per frame beyond one attribute set.
"""

from __future__ import annotations

import logging
import queue
import threading

from ...config import OverlayConfig
from ..base import OverlayState, TargetWindow
from ..fade import Fade
from ..fonts import choose_font
from ..geometry import SlabLayout, plan_slab

log = logging.getLogger(__name__)

#: How long a finished/failed message stays on screen before the overlay hides.
#: DONE holds at full opacity: the slab does not follow the words out.
DONE_LINGER_MS = 900
ERROR_LINGER_MS = 6000

#: The state words. Short, lower case and calm on purpose - they sit beside the
#: caption at a smaller size, and the colour bar is what actually carries the
#: state, so these do not have to shout. The longest one sets the gutter width,
#: which is why keeping them short buys measure for the words.
_LABELS = {
    OverlayState.LISTENING: ("listening", "live"),
    OverlayState.THINKING: ("thinking", "idle"),
    OverlayState.DONE: ("pasted", "idle"),
    OverlayState.ERROR: ("error", "bad"),
}
_LONGEST_LABEL = max((text for text, _ in _LABELS.values()), key=len)


class TkCaptionOverlay:
    """Implements `platform.base.CaptionOverlay`.

    Lifecycle: build on the main thread, call `run_forever()` on the main thread,
    call `set_state()` from anywhere, call `close()` to end `run_forever()`.
    """

    def __init__(self, cfg: OverlayConfig, *, notify=None) -> None:
        self.cfg = cfg
        self._queue: queue.Queue[tuple[OverlayState, str, TargetWindow | None]] = queue.Queue()
        self._notify = notify
        self._root = None
        self._frame = None
        self._bar = None
        self._status = None
        self._label = None
        self._caption_font = None
        self._status_font = None
        self._hwnd = 0
        self._visible = False
        self._layout: SlabLayout | None = None
        self._state = OverlayState.HIDDEN
        self._target: TargetWindow | None = None
        self._hide_job = None
        self._fade_job = None
        self._fade = Fade(step_ms=max(1, cfg.fade_step_ms))
        self._closing = threading.Event()
        self._ready = threading.Event()
        self._font_choice = None
        self._dpi_mode = "not set"

    # -- public, thread-safe ---------------------------------------------

    def set_state(self, state: OverlayState, text: str = "",
                  target: TargetWindow | None = None) -> None:
        """Update what is on screen. Safe to call from any thread.

        `target` is the window captured at hotkey press. It decides which
        monitor the captions appear on, and is only read when the overlay is
        about to appear.
        """
        self._queue.put((state, text, target))

    def close(self) -> None:
        self._closing.set()

    def wait_ready(self, timeout: float = 10.0) -> bool:
        return self._ready.wait(timeout)

    @property
    def describe(self) -> str:
        font = self._font_choice
        family = font.family if font else self.cfg.font_family
        return (f"{family} {self.cfg.font_size}pt, {self.cfg.position}, "
                f"DPI awareness {self._dpi_mode}")

    # -- UI thread -------------------------------------------------------

    def run_forever(self) -> None:
        """Build the window and run the Tk loop. Blocks. Main thread only."""
        import tkinter as tk  # noqa: PLC0415 - stdlib, but only wanted on Windows

        # Before any window exists, or Windows virtualises this process's
        # coordinates and the monitor rectangles stop matching what Tk is asked
        # for. See monitors.py.
        self._dpi_mode = self._set_dpi_awareness()

        root = tk.Tk()
        self._root = root
        root.withdraw()
        root.overrideredirect(True)          # no title bar, no window chrome
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.0)
        root.configure(bg=self.cfg.edge)
        try:
            root.attributes("-toolwindow", True)
        except tk.TclError:
            pass  # not available on every Tk build; the ex-style below covers it

        self._build_widgets(tk)

        # Getting the window into "Tk thinks it is mapped, Windows has it
        # hidden" without a visible flash, so that from here on visibility is
        # ours alone (ShowWindow) and Tk never tries to re-map or activate it:
        #
        #   1. fully transparent, so nothing can appear on screen
        #   2. realise the HWND while still withdrawn
        #   3. apply WS_EX_NOACTIVATE and friends BEFORE it is ever mapped
        #   4. map it - now safe, because it is non-activating and invisible
        #   5. hide it at the Win32 level
        #
        # The opacity is NOT restored at the end any more: the window now lives
        # at alpha 0 while hidden and is faded up when it appears, so there is
        # never a frame at full opacity that nobody asked for.
        root.update_idletasks()
        self._apply_window_styles()
        root.deiconify()
        root.update_idletasks()
        self._hide_now()
        self._fade.jump_to(0.0)

        self._ready.set()
        root.after(30, self._poll)
        try:
            root.mainloop()
        finally:
            self._ready.clear()

    def _set_dpi_awareness(self) -> str:
        from .monitors import set_dpi_awareness  # noqa: PLC0415 - Windows only

        try:
            mode = set_dpi_awareness(self.cfg.dpi_awareness)
        except Exception:
            log.exception("could not set DPI awareness; carrying on unaware")
            return "unaware"
        if self.cfg.dpi_awareness == "per-monitor" and mode not in (
                "per-monitor-v2", "per-monitor"):
            log.warning(
                "this Windows build would not give dictate per-monitor DPI "
                "awareness (got %r). Captions on a second monitor with "
                "different scaling may be the wrong size or land in the wrong "
                "place; set [overlay] dpi_awareness = \"off\" if they do.", mode)
        log.info("caption overlay DPI awareness: %s", mode)
        return mode

    def _build_widgets(self, tk) -> None:
        """The slab: an edge frame, a face inside it, a colour bar down the left.

        Nothing here has a border, a relief or a highlight ring - Tk's defaults
        for those are one and two pixel lines, and a hairline is the one thing
        this look refuses. Every visible division is a block of real thickness,
        sized in `_show()`.
        """
        import tkinter.font as tkfont  # noqa: PLC0415

        self._font_choice = choose_font(
            self.cfg.font_family,
            lambda family: tkfont.Font(root=self._root, family=family,
                                       size=-16).actual("family"),
        )
        message = self._font_choice.report()
        if message:
            log.warning("%s", message)
        else:
            message = f"caption font: {self._font_choice.family}"
            log.info("%s", message)
        # Said on startup either way. "It silently used a different font" is the
        # failure this whole path exists to make impossible, and the only way to
        # be sure it did not happen is to print which font it got.
        if self._notify is not None:
            try:
                self._notify("warning" if not self._font_choice.ok else "info", message)
            except Exception:
                log.debug("could not report the caption font", exc_info=True)

        family = self._font_choice.family
        self._caption_font = tkfont.Font(root=self._root, family=family, size=-16)
        self._status_font = tkfont.Font(root=self._root, family=family, size=-12,
                                        weight="bold")

        common = dict(bd=0, highlightthickness=0, relief="flat")
        self._frame = tk.Frame(self._root, bg=self.cfg.background, **common)
        self._bar = tk.Frame(self._frame, bg=self.cfg.accent, **common)
        self._status = tk.Label(self._frame, text="", bg=self.cfg.background,
                                fg=self.cfg.accent, font=self._status_font,
                                anchor="nw", justify="left", **common)
        self._label = tk.Label(self._frame, text="", bg=self.cfg.background,
                               fg=self.cfg.foreground, font=self._caption_font,
                               anchor="nw", justify="left", **common)

    # -- window styles ---------------------------------------------------

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
            self._cancel(("_hide_job", "_fade_job"))
            self._root.quit()
            return
        latest = None
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

    def _apply(self, state: OverlayState, text: str,
               target: TargetWindow | None) -> None:
        if self._hide_job is not None:
            self._root.after_cancel(self._hide_job)
            self._hide_job = None

        if state is OverlayState.HIDDEN:
            self._state = state
            self._hide()
            return

        if not self._visible:
            # The one place geometry is decided. Everything after this point in
            # the utterance only ever changes text and colour.
            self._target = target
            self._show()
        else:
            if target is not None:
                self._target = target
            # Still on screen, but possibly on its way out - the DONE linger has
            # expired and the fade started, and then he pressed the hotkey again.
            # Without this the new utterance's captions would be drawn onto a
            # panel that carries on fading to nothing.
            if self._fade.target < self.cfg.opacity:
                self._fade_to(self.cfg.opacity, self.cfg.fade_in_ms)

        label, tone = _LABELS[state]
        colour = {
            "live": self.cfg.accent,
            "idle": self.cfg.muted,
            "bad": self.cfg.error,
        }[tone]
        self._bar.configure(bg=colour)
        self._status.configure(text=label, fg=colour)
        # The big line carries the caption while listening and the message when
        # something went wrong; it is empty for THINKING and DONE, which is what
        # makes the caption text visibly disappear the moment the key is
        # released. The slab itself does not go with it - that is the point.
        self._label.configure(text=text)
        self._state = state

        if state is OverlayState.DONE:
            self._hide_job = self._root.after(DONE_LINGER_MS, self._hide)
        elif state is OverlayState.ERROR:
            self._hide_job = self._root.after(ERROR_LINGER_MS, self._hide)

    # -- geometry, once per appearance -----------------------------------

    def _pick_monitor(self):
        """The display this appearance belongs on. The rule is in monitors.py."""
        from .monitors import Monitor, pick_monitor, primary_monitor  # noqa: PLC0415

        if self.cfg.follow_focus:
            handle = self._target.handle if self._target is not None else None
            monitor = pick_monitor(handle)
        else:
            monitor = primary_monitor()
        if monitor is None:
            # Nothing about the desktop is knowable; fall back to what Tk sees so
            # the overlay still appears rather than refusing to.
            width = self._root.winfo_screenwidth()
            height = self._root.winfo_screenheight()
            monitor = Monitor(work=(0, 0, width, height),
                              bounds=(0, 0, width, height), picked_by="tk")
        return monitor

    def _measure(self) -> SlabLayout:
        """This appearance's numbers, on this appearance's monitor.

        The order matters and is why this is not two functions: the scale comes
        from the monitor, the font pixel sizes come from the scale, and the
        gutter width and line height are then *measured* from fonts that are
        already at those sizes. Estimating either would clip the state word or
        misjudge the slab's height on a display that is not at 100%.
        """
        monitor = self._pick_monitor()
        scale = monitor.scale if self.cfg.dpi_awareness != "off" else 1.0
        caption_px = max(6, round(self.cfg.font_size * 96 / 72 * scale))
        status_px = max(5, round(max(7, self.cfg.font_size - 5) * 96 / 72 * scale))
        self._caption_font.configure(size=-caption_px)
        self._status_font.configure(size=-status_px)

        layout = plan_slab(
            position=self.cfg.position,
            work=monitor.work,
            scale=scale,
            max_width_px=self.cfg.max_width_px,
            margin_px=self.cfg.margin_px,
            edge_px=self.cfg.edge_px,
            padding_px=self.cfg.padding_px,
            font_size=self.cfg.font_size,
            status_size=max(7, self.cfg.font_size - 5),
            lines=self.cfg.lines,
            status_width_px=self._status_font.measure(_LONGEST_LABEL),
            line_height_px=self._caption_font.metrics("linespace"),
        )
        log.debug("caption overlay on %s -> %dx%d at (%d,%d)", monitor,
                  layout.width, layout.height, layout.x, layout.y)
        return layout

    def _show(self) -> None:
        from .win32 import (  # noqa: PLC0415
            HWND_TOPMOST, SWP_NOACTIVATE, SWP_NOMOVE, SWP_NOSIZE,
            SW_SHOWNOACTIVATE, user32,
        )

        layout = self._measure()
        self._layout = layout

        # Lay the slab out. `place` rather than `pack`, so every element is at an
        # exact pixel and nothing is negotiated with a geometry manager while
        # captions are arriving.
        self._root.configure(bg=self.cfg.edge)
        self._frame.place(x=layout.edge, y=layout.edge,
                          width=layout.width - 2 * layout.edge,
                          height=layout.face_height)
        self._bar.place(x=0, y=0, width=layout.bar, height=layout.face_height)
        self._status.place(x=layout.bar + layout.pad_x, y=layout.pad_y,
                           width=layout.gutter - layout.bar - 2 * layout.pad_x,
                           height=layout.line_height)
        self._label.place(x=layout.gutter, y=layout.pad_y, width=layout.text_width,
                          height=layout.lines * layout.line_height)
        self._label.configure(wraplength=layout.text_width)
        self._root.geometry(f"{layout.width}x{layout.height}+{layout.x}+{layout.y}")
        # Settle the layout now rather than on the next trip round the event
        # loop, which would be after ShowWindow. With the fade on, the window is
        # at zero opacity here and nobody could see the difference; with
        # `fade = false` it is the difference between appearing laid out and
        # appearing for one frame with everything in the wrong place.
        self._root.update_idletasks()

        # Re-assert the extended style: Tk can reset it when it rebuilds a
        # window, and losing WS_EX_NOACTIVATE here is the failure that would
        # send the pasted text to the wrong place.
        self._apply_window_styles()
        # SW_SHOWNOACTIVATE, never Tk's deiconify(), which goes through SW_SHOW
        # and would activate the window. That is the whole point.
        user32.ShowWindow(self._hwnd, SW_SHOWNOACTIVATE)
        self._visible = True
        # Reassert topmost without moving, resizing or activating: another app
        # going full-screen can knock a topmost window down the z-order. This is
        # the only SetWindowPos in the visible life of an utterance - it happens
        # here, at the appearance, and never again while words are arriving.
        user32.SetWindowPos(self._hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        self._fade_to(self.cfg.opacity, self.cfg.fade_in_ms)

    def _hide(self) -> None:
        self._hide_job = None
        if not self._visible:
            return
        if self._closing.is_set():
            self._hide_now()
            return
        self._fade_to(0.0, self.cfg.fade_out_ms, then=self._hide_now)

    def _hide_now(self) -> None:
        from .win32 import SW_HIDE, user32  # noqa: PLC0415

        self._cancel(("_fade_job",))
        self._fade.jump_to(0.0)
        self._set_alpha(0.0)
        user32.ShowWindow(self._hwnd, SW_HIDE)
        self._visible = False

    # -- the fade (UI thread) --------------------------------------------

    def _fade_to(self, target: float, duration_ms: int, then=None) -> None:
        """Drive the window's own opacity to `target`. Never blocks.

        Interrupting a fade in flight is normal - a second hotkey press while
        the overlay is on its way out - and `Fade` resumes from the alpha that is
        actually on screen rather than restarting.
        """
        self._cancel(("_fade_job",))
        if not self.cfg.fade or duration_ms <= 0:
            self._fade.jump_to(target)
            self._set_alpha(target)
            if then is not None:
                then()
            return
        self._fade.to(target, duration_ms)
        self._step_fade(then)

    def _step_fade(self, then) -> None:
        self._fade_job = None
        if self._closing.is_set():
            return
        alpha = self._fade.advance()
        self._set_alpha(alpha)
        if self._fade.done:
            if then is not None:
                then()
            return
        self._fade_job = self._root.after(self.cfg.fade_step_ms,
                                          lambda: self._step_fade(then))

    def _set_alpha(self, alpha: float) -> None:
        try:
            self._root.attributes("-alpha", round(alpha, 3))
        except Exception:
            log.debug("could not set the overlay opacity", exc_info=True)

    def _cancel(self, jobs) -> None:
        for name in jobs:
            job = getattr(self, name, None)
            if job is not None:
                try:
                    self._root.after_cancel(job)
                except Exception:
                    log.debug("could not cancel %s", name, exc_info=True)
                setattr(self, name, None)
