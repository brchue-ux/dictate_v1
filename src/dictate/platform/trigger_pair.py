"""A mouse trigger with the keyboard chord still live behind it.

**Why both at once.** A global mouse hook is a thing that can fail: Windows may
refuse to install it, and it can be dropped later without telling anybody -
which is a trigger that looks fine and does nothing, the exact failure this
project's rules forbid. dictate cannot detect the second case from inside
(nothing is delivered when a hook is not called, and "no mouse events" is also
what an idle machine looks like), so the answer is not detection: it is that
there is always another way in. When `[hotkey] combination` names a mouse
button, the chord in `[hotkey] keyboard_fallback` is registered as well and
keeps working forever, and both halves call the same press and release.

It is also the honest reading of "this adds an option, it does not replace the
chord": the keyboard he has been using for weeks does not stop working the
moment he tries the thumb button.

**Which half owns an utterance.** Whichever went down first. Without that, a
chord tapped while the mouse button is held would end the recording he is in the
middle of - two triggers, one pipeline, and `finish_utterance` does not care who
sent it. The half that started it is the only one that can end it.

**What a failing mouse hook does here.** `register`/`start` raise, but never
before the keyboard half is registered AND started, so whoever catches the error
already has a working trigger. `app.Application` catches it two different ways
on purpose: at startup it reports and carries on (there is nothing else to keep,
and refusing to run over a mouse button would be absurd), and when he picks a
mouse button from the tray it puts the previous trigger back and says Windows
refused - the same order `change_hotkey` has always kept.

Pure: the two halves are anything with the `platform.base.HotkeyListener` shape,
so this is tested with fakes in tests/test_mouse_trigger.py.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from ..errors import DictateError, MouseHookError

log = logging.getLogger(__name__)

MOUSE = "mouse"
KEYBOARD = "keyboard"


class TriggerPair:
    """Implements `platform.base.HotkeyListener` over two of them."""

    def __init__(self, mouse, keyboard, *, notify=None) -> None:
        self.mouse = mouse
        self.keyboard = keyboard
        self.notify = notify or (lambda level, message: None)
        self._lock = threading.Lock()
        self._holder: str | None = None
        self._on_press: Callable[[], None] | None = None
        self._on_release: Callable[[], None] | None = None
        self._keyboard_registered = False
        self._keyboard_started = False
        #: Whether the mouse half is actually in place. False after a failure,
        #: so `describe` and the tray stop claiming a trigger nobody has.
        self.mouse_live = False

    # -- the HotkeyListener shape ----------------------------------------

    def register(self, on_press: Callable[[], None],
                 on_release: Callable[[], None]) -> None:
        self._on_press, self._on_release = on_press, on_release
        self._ensure_keyboard(register=True, start=False)
        # Told where to complain if the hook is lost later. Set here rather
        # than passed to the constructor so that a half with no such notion -
        # a fake, a future implementation - simply does not get one.
        if hasattr(self.mouse, "on_lost"):
            self.mouse.on_lost = self._mouse_lost
        self._mouse_step("register", lambda: self.mouse.register(
            *self._wrap(MOUSE)))

    def start(self) -> None:
        self._ensure_keyboard(register=True, start=True)
        self._mouse_step("start", self.mouse.start)
        self.mouse_live = True

    def stop(self) -> None:
        for name, half in (("mouse", self.mouse), ("keyboard", self.keyboard)):
            try:
                half.stop()
            except Exception:
                log.debug("stopping the %s trigger raised", name, exc_info=True)
        self.mouse_live = False
        # Both halves are back to never-registered. `app._restore_hotkey` puts
        # a stopped trigger back by registering it again, and a keyboard half
        # that believed it was still registered would come back with no
        # binding at all - which is dictate with no way in.
        self._keyboard_registered = False
        self._keyboard_started = False
        with self._lock:
            self._holder = None

    @property
    def describe(self) -> str:
        mouse = self.mouse.describe
        if not self.mouse_live:
            return f"{self.keyboard.describe} - {mouse} is NOT working"
        return f"{mouse}; {self.keyboard.describe} still works too"

    # -- the two halves ---------------------------------------------------

    def _ensure_keyboard(self, *, register: bool, start: bool) -> None:
        """Get the chord live, once, whatever else happens.

        Called from `register`, from `start`, and again from the failure path,
        so the promise "there is always a way in" holds even when the mouse
        half fails on the first of the two calls.
        """
        if register and not self._keyboard_registered:
            self.keyboard.register(*self._wrap(KEYBOARD))
            self._keyboard_registered = True
        if start and self._keyboard_registered and not self._keyboard_started:
            self.keyboard.start()
            self._keyboard_started = True

    def _mouse_step(self, what: str, step: Callable[[], None]) -> None:
        """Run one step of the mouse half; on failure leave the chord working
        and raise something that says both halves of the truth."""
        try:
            step()
        except BaseException as exc:
            self.mouse_live = False
            log.warning("the mouse trigger could not %s: %s", what, exc)
            try:
                self._ensure_keyboard(register=True, start=True)
            except Exception:
                log.exception("the keyboard trigger could not be started either")
            reason = exc.message if isinstance(exc, DictateError) else str(exc)
            raise MouseHookError(
                f"{self.mouse.describe} is not available: {reason}",
                f"dictate is listening on {self.keyboard.describe} instead, "
                f"which still works. Security software that blocks a "
                f"low-level mouse hook is the usual reason; `dictate doctor` "
                f"reports what is in force.",
            ) from exc

    def _wrap(self, half: str) -> tuple[Callable[[], None], Callable[[], None]]:
        """The callbacks one half gets: the app's own, with the ownership rule
        in front of them."""

        def press() -> None:
            with self._lock:
                if self._holder is not None:
                    return
                self._holder = half
            if self._on_press is not None:
                self._on_press()

        def release() -> None:
            with self._lock:
                if self._holder != half:
                    return
                self._holder = None
            if self._on_release is not None:
                self._on_release()

        return press, release

    def _mouse_lost(self, reason: str) -> None:
        """The hook went away while dictate was running.

        Loud, because the symptom otherwise is a button that silently stopped
        working; `notify("error")` is what turns the tray icon red, which is
        the only surface a logon-started copy has.
        """
        self.mouse_live = False
        self.notify("error", f"{self.mouse.describe} has stopped working: "
                             f"{reason} Hold {self.keyboard.describe} instead, "
                             f"or restart dictate from the icon by the clock.")
