"""The global push-to-talk hotkey.

Windows' own `RegisterHotKey` only reports key-down, so it cannot express
"recording lasts as long as you hold this". The `global_hotkeys` package gives
separate press and release callbacks, which is what push-to-talk needs.
`PinW/whisper-key-local` (MIT) uses the same package for the same reason;
reading it is what pointed here.

**Correction, found while diagnosing toggle-off being missed away from the
window he started in (2026-08-21): this is not a `WH_KEYBOARD_LL` hook.**
`global_hotkeys==0.1.7` (`hotkey_checker.py::HotkeyChecker.run`) is a plain
Python thread that calls `win32api.GetAsyncKeyState()` for every registered
combination every 20 ms. It only *behaves* like a hook because that poll is
fast enough and global enough that the difference is invisible in the
ordinary case. The distinction matters because `GetAsyncKeyState` is a
key-STATE query, not an event: unlike a real `WH_KEYBOARD_LL` hook it cannot
be "missed" by a slow callback, but it inherits whatever Windows will and will
not tell an unelevated process about the keyboard right now - the same
boundary this codebase already documents for the mouse hook and for
`SendInput` (`hotkey_switch.MOUSE_COST`, `inject.py`'s "administrator" error).
That boundary is the leading suspect for the toggle-off report; it is not
confirmed, because it depends on which window he was in, which nobody here can
observe. See the PR that added this correction for what is proved and what
still needs his machine.

`actuate_on_partial_release=True` matters for "hold": releasing the *last* key
of Ctrl+Alt+Space should end the recording even though Ctrl and Alt are still
down, because that is how people actually let go of a chord. Traced through
`hotkey_checker.py`'s own state machine, this setting does not change toggle
mode's observed behaviour for an ordinary full press-then-release - toggle's
`False` was not the cause of a missed stop, and is left as it was rather than
changed on an unproven hunch.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from ...errors import MissingDependencyError, PlatformUnsupportedError
from ..hotkey_spec import describe, normalise

log = logging.getLogger(__name__)


class WindowsHotkeyListener:
    """Implements `platform.base.HotkeyListener`."""

    def __init__(self, combination: str, mode: str = "hold") -> None:
        self.combination = normalise(combination)
        self.pretty = describe(combination)
        self.mode = mode
        self._started = False
        self._api = None
        self._toggle_on = False

    @property
    def describe(self) -> str:
        how = "hold to talk" if self.mode == "hold" else "press to start, press again to stop"
        return f"{self.pretty} ({how})"

    def _load(self):
        if self._api is not None:
            return self._api
        try:
            import global_hotkeys  # noqa: PLC0415 - optional, Windows only
        except ImportError as exc:
            raise MissingDependencyError(
                "The global-hotkeys package is not installed, so dictate cannot "
                "listen for the hotkey.",
                "Install it with: pip install global-hotkeys",
            ) from exc
        self._api = global_hotkeys
        return global_hotkeys

    def register(self, on_press: Callable[[], None], on_release: Callable[[], None]) -> None:
        api = self._load()

        if self.mode == "toggle":
            def press() -> None:
                self._toggle_on = not self._toggle_on
                # The one piece of evidence a poll-based "hook" can give: that
                # the combination WAS seen. If a report ever says a toggle
                # press did nothing, this line's absence in the log for that
                # moment is what tells the difference between "Windows never
                # told dictate" and a fault somewhere after this point.
                log.info("toggle hotkey seen: recording now %s",
                         "on" if self._toggle_on else "off")
                (on_press if self._toggle_on else on_release)()

            binding = [self.combination, _guard(press), None, False]
        else:
            def press() -> None:
                log.debug("hotkey press seen")
                on_press()

            def release() -> None:
                log.debug("hotkey release seen")
                on_release()

            binding = [self.combination, _guard(press), _guard(release), True]

        try:
            api.register_hotkeys([binding])
        except Exception as exc:
            raise PlatformUnsupportedError(
                f"Windows would not accept the hotkey {self.pretty}: {exc}",
                "Another program may already own that combination. Pick a "
                "different [hotkey] combination in your config.",
            ) from exc
        log.info("hotkey registered: %s", self.describe)

    def start(self) -> None:
        if self._started:
            return
        self._load().start_checking_hotkeys()
        self._started = True

    def stop(self) -> None:
        if not self._started or self._api is None:
            return
        try:
            self._api.stop_checking_hotkeys()
            self._api.clear_hotkeys()
        except Exception:
            log.debug("stopping the hotkey listener raised", exc_info=True)
        self._started = False


def _guard(fn: Callable[[], None]) -> Callable[[], None]:
    """A callback that raises inside a keyboard hook can wedge the hook thread,
    and with it every keystroke on the machine. Nothing gets out of here."""

    def wrapped(*_args, **_kwargs) -> None:
        try:
            fn()
        except Exception:
            log.exception("hotkey callback failed")

    return wrapped
