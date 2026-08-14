"""The mouse push-to-talk trigger, in Win32.

**Read this before touching anything in here.** Windows offers exactly one way
to see the middle and thumb mouse buttons globally: `SetWindowsHookEx` with
`WH_MOUSE_LL`. Neither `RegisterHotKey` nor the `global_hotkeys` package behind
the keyboard chord can see a mouse button at all - both are keyboard-only, and
`hotkey.py`'s header says why the package is there in the first place. So this
file puts a callback of dictate's in the path of **every mouse event on his
machine**, and everything about its shape follows from that:

* **the callback decides and returns, and does nothing else.** Windows gives a
  low-level hook `LowLevelHooksTimeout` milliseconds (300 by default) to answer;
  a hook that is slower has its answer ignored and, on older builds, is removed
  outright. So the callback does one dict lookup, one struct read, one call into
  a pure state machine that takes an uncontended lock, and a `SimpleQueue.put`.
  Everything with any weight in it - starting a recording, finishing an
  utterance, sending the replayed click - happens on a worker thread. Nothing
  here logs, allocates a message, or touches the pipeline.
* **the swallow is the return value.** Returning 1 stops the event reaching
  every other application; returning `CallNextHookEx` passes it on. There is no
  third answer and no way to decide later, which is what
  `platform/mouse_trigger.py` is built around.
* **what is decided lives in the pure half.** This file holds no rules. Nobody
  on this build has Windows or a mouse to press, so the parsing, the arming, the
  swallow rule and the press/release state machine are all in
  `platform/mouse_trigger.py` and `platform/hotkey_spec.py`, tested anywhere,
  and this is the smallest possible amount of code that cannot be.

**Two things about a global mouse hook that are worth saying out loud.** It is
the same Windows facility a keylogger's mouse half uses, so security software
takes an interest in it and may refuse or remove it - that is not a hypothetical
and it is why `platform/trigger_pair.py` keeps the keyboard chord live behind
this. And Windows does not let an unelevated process touch the input of an
elevated one, so with an administrator window focused the mouse trigger is
expected to do nothing at all - the keyboard chord has the same limitation, and
"expected" is the right word for both: nobody here has watched either.
"""

from __future__ import annotations

import ctypes
import logging
import queue
import threading
import time
from collections.abc import Callable
from ctypes import wintypes

from ...errors import MouseHookError
from ..hotkey_spec import MOUSE_BUTTONS, describe
from ..mouse_trigger import DOWN, UP, Action, MouseTrigger
from . import win32

log = logging.getLogger(__name__)

#: Stamped into `dwExtraInfo` on the click dictate replays, and recognised on
#: the way back in. Deliberately not "was it injected?": a mouse driver or a
#: remapping utility injects events too, and a button dictate is told about
#: through one of those should still work.
DICTATE_TAG = 0xD1C7A7E

#: Windows message -> what `mouse_trigger` calls it. A lookup rather than a
#: chain of comparisons because this runs for every mouse move on the machine.
_KIND: dict[int, str] = {
    win32.WM_MBUTTONDOWN: DOWN,
    win32.WM_MBUTTONUP: UP,
    win32.WM_XBUTTONDOWN: DOWN,
    win32.WM_XBUTTONUP: UP,
}

#: The thumb buttons, as the high word of `mouseData` reports them.
_XBUTTON = {win32.XBUTTON1: "mouse4", win32.XBUTTON2: "mouse5"}

#: What `SendInput` needs to reproduce a click of each button.
_REPLAY = {
    "mouse3": (win32.MOUSEEVENTF_MIDDLEDOWN, win32.MOUSEEVENTF_MIDDLEUP, 0),
    "mouse4": (win32.MOUSEEVENTF_XDOWN, win32.MOUSEEVENTF_XUP, win32.XBUTTON1),
    "mouse5": (win32.MOUSEEVENTF_XDOWN, win32.MOUSEEVENTF_XUP, win32.XBUTTON2),
}

#: Pushed onto the worker's queue by `stop()`.
_FINISHED = object()


class WindowsMouseTriggerListener:
    """Implements `platform.base.HotkeyListener` for one mouse button."""

    def __init__(self, button: str, *, click_through: bool = True,
                 min_hold_s: float = 0.35, notify=None) -> None:
        if button not in MOUSE_BUTTONS:
            raise MouseHookError(
                f"{button!r} is not a mouse button dictate can hold.",
                "This is a programming error: platform/hotkey_spec.py decides "
                "what is a mouse button, and it let this through.",
            )
        self.button = button
        self.pretty = describe(button)
        self.click_through = click_through
        self.trigger = MouseTrigger(button, click_through=click_through,
                                    min_hold_s=min_hold_s)
        self.notify = notify or (lambda level, message: None)
        #: Set by `trigger_pair.TriggerPair` so a hook lost while dictate is
        #: running is reported rather than becoming a button that stopped
        #: working for no stated reason.
        self.on_lost: Callable[[str], None] | None = None
        self._on_press: Callable[[], None] = lambda: None
        self._on_release: Callable[[], None] = lambda: None
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._hook_thread: threading.Thread | None = None
        self._worker: threading.Thread | None = None
        self._hook_thread_id = 0
        self._hook = None
        self._ready = threading.Event()
        self._failure: BaseException | None = None
        self._stopping = False
        self._replay_complained = False
        # Kept on the instance: Windows holds the pointer, and a callback that
        # is garbage collected while installed is a crash in somebody else's
        # process, not an exception in ours.
        self._proc = win32.HOOKPROC(self._on_mouse_event)

    @property
    def describe(self) -> str:
        if self.click_through:
            return (f"{self.pretty} (hold to talk; a quick click still does "
                    f"what the button normally does)")
        return (f"{self.pretty} (hold to talk; the button does nothing else "
                f"while dictate is running)")

    # -- the HotkeyListener shape ----------------------------------------

    def register(self, on_press: Callable[[], None],
                 on_release: Callable[[], None]) -> None:
        self._on_press, self._on_release = on_press, on_release

    def start(self) -> None:
        """Install the hook, and raise if Windows would not have it.

        Returns once the hook is in place or has failed - never later, because
        the caller's next line tells the product owner which trigger he has.
        """
        if self._hook_thread is not None:
            return
        # Clean slate: a listener that failed once and was stopped can be
        # started again (that is what `app._restore_hotkey` does), and the old
        # failure must not be re-reported as this attempt's.
        self._stopping = False
        self._failure = None
        self._ready.clear()
        self._worker = threading.Thread(target=self._pump_actions,
                                        name="dictate-mouse-trigger", daemon=True)
        self._worker.start()
        self.trigger.arm()
        self._hook_thread = threading.Thread(target=self._run_hook,
                                             name="dictate-mouse-hook", daemon=True)
        self._hook_thread.start()
        if not self._ready.wait(timeout=10.0) or self._failure is not None:
            self.stop()
            reason = (f"{self._failure}" if self._failure is not None
                      else "it did not answer within ten seconds")
            raise MouseHookError(
                f"Windows would not give dictate a low-level mouse hook: {reason}",
                "Security software is the usual reason a mouse hook is "
                "refused. Without one dictate cannot see a mouse button at "
                "all - Windows tells applications about the keyboard's "
                "hotkeys, and about the mouse only this way.",
            )
        log.info("mouse trigger installed: %s", self.describe)

    def stop(self) -> None:
        self._stopping = True
        outstanding = self.trigger.disarm()
        thread_id, self._hook_thread_id = self._hook_thread_id, 0
        if thread_id:
            # Ends the message loop, which unhooks on its way out. Posting is
            # the only way in: the hook belongs to that thread.
            win32.user32.PostThreadMessageW(thread_id, win32.WM_QUIT, 0, 0)
        thread, self._hook_thread = self._hook_thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        # The utterance he was in the middle of is finished before the worker
        # is told to go: he was still holding the button when dictate stopped,
        # and the recording is his.
        if outstanding.release:
            self._queue.put(outstanding)
        worker, self._worker = self._worker, None
        if worker is not None:
            self._queue.put(_FINISHED)
            if worker is not threading.current_thread():
                worker.join(timeout=5.0)
        self._ready.clear()

    # -- the hook --------------------------------------------------------

    def _run_hook(self) -> None:
        """Own the hook and pump its thread's messages.

        A low-level hook is called on the thread that installed it, and only
        while that thread is pumping messages - so this thread exists to do
        nothing but that.
        """
        try:
            self._install()
        except BaseException as exc:  # noqa: BLE001 - reported to start()
            self._failure = exc
            self._ready.set()
            return
        self._ready.set()
        lost = ""
        try:
            message = wintypes.MSG()
            while win32.user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                pass  # nothing is dispatched here; the hook is called directly
        except Exception as exc:  # noqa: BLE001 - the hook is gone either way
            log.exception("the mouse hook's message loop stopped")
            lost = str(exc)
        finally:
            self._uninstall()
        if not self._stopping:
            self._lost(lost or "its message loop ended without being asked to.")

    def _install(self) -> None:
        module = win32.kernel32.GetModuleHandleW(None)
        hook = win32.user32.SetWindowsHookExW(win32.WH_MOUSE_LL, self._proc,
                                             module, 0)
        if not hook:
            raise OSError(win32.last_error() or "SetWindowsHookExW failed")
        self._hook = hook
        self._hook_thread_id = win32.kernel32.GetCurrentThreadId()

    def _uninstall(self) -> None:
        hook, self._hook = self._hook, None
        if hook:
            try:
                win32.user32.UnhookWindowsHookEx(hook)
            except Exception:
                log.debug("UnhookWindowsHookEx raised", exc_info=True)

    def _on_mouse_event(self, code: int, wparam: int, lparam: int) -> int:
        """Every mouse event on the machine arrives here. Do almost nothing.

        The order of the first three lines is the performance of the mouse
        system: a mouse move is a dict miss and a `CallNextHookEx`, and never
        reaches the state machine or the queue.
        """
        try:
            if code < 0:
                return win32.user32.CallNextHookEx(None, code, wparam, lparam)
            kind = _KIND.get(wparam)
            if kind is None:
                return win32.user32.CallNextHookEx(None, code, wparam, lparam)
            # A view over Windows' own struct, not a copy: `from_address`
            # rather than a cast because it is one call instead of two, and
            # this is the path every mouse button on the machine takes.
            data = win32.MSLLHOOKSTRUCT.from_address(lparam)
            if wparam in (win32.WM_XBUTTONDOWN, win32.WM_XBUTTONUP):
                button = _XBUTTON.get((data.mouseData >> 16) & 0xFFFF, "")
            else:
                button = "mouse3"
            action = self.trigger.event(
                kind, button, now=time.monotonic(),
                ours=data.dwExtraInfo == DICTATE_TAG)
            if action.press or action.release or action.replay:
                self._queue.put(action)
            if action.swallow:
                return 1
        except Exception:  # noqa: BLE001 - never wedge the machine's mouse
            # No logging call on this path on purpose: it runs inside the hook,
            # and a log handler that blocks is the mouse freezing.
            pass
        return win32.user32.CallNextHookEx(None, code, wparam, lparam)

    # -- the worker ------------------------------------------------------

    def _pump_actions(self) -> None:
        """Everything the hook decided, done somewhere it is allowed to take
        time. One thread, so a press can never overtake the release before it."""
        while True:
            action = self._queue.get()
            if action is _FINISHED:
                return
            try:
                self._perform(action)
            except Exception:
                log.exception("a mouse trigger action failed")

    def _perform(self, action: Action) -> None:
        # The replay first: it is his Back click, and the millisecond it waits
        # is a millisecond he is looking at the page.
        if action.replay:
            self._replay()
        if action.press:
            self._safely(self._on_press, "press")
        if action.release:
            self._safely(self._on_release, "release")

    def _safely(self, fn: Callable[[], None], what: str) -> None:
        try:
            fn()
        except Exception:
            log.exception("the mouse trigger's %s callback failed", what)

    def _replay(self) -> None:
        """Give the app the click it was expecting.

        A down and an up in one `SendInput` call, at wherever the cursor is now
        - which is where he clicked, a few milliseconds ago. Tagged, so the hook
        above lets it past instead of reading it as a new press.
        """
        down_flag, up_flag, data = _REPLAY[self.button]
        events = (win32.INPUT * 2)()
        for event, flag in ((events[0], down_flag), (events[1], up_flag)):
            event.type = win32.INPUT_MOUSE
            event.mi.dx = 0
            event.mi.dy = 0
            event.mi.mouseData = data
            event.mi.dwFlags = flag
            event.mi.time = 0
            event.mi.dwExtraInfo = DICTATE_TAG
        sent = win32.user32.SendInput(2, events, ctypes.sizeof(win32.INPUT))
        if sent == 2:
            return
        # His click went nowhere, which is the one way this design can cost him
        # something silently. Said once - it would otherwise be said on every
        # click for as long as whatever is blocking SendInput is focused.
        log.warning("the replayed %s click was refused by Windows (%s)",
                    self.pretty, win32.last_error())
        if not self._replay_complained:
            self._replay_complained = True
            self.notify("warning",
                        f"A quick click of {self.pretty} could not be passed "
                        f"on to the window you clicked in: Windows refused it. "
                        f"That usually means an application running as "
                        f"administrator, which dictate - which is not - may "
                        f"not send input to.")

    def _lost(self, reason: str) -> None:
        log.error("the mouse hook is gone: %s", reason)
        self.trigger.disarm()
        handler = self.on_lost
        if handler is not None:
            try:
                handler(reason)
            except Exception:
                log.exception("reporting the lost mouse hook failed")
        else:
            self.notify("error", f"{self.pretty} has stopped working: {reason}")
