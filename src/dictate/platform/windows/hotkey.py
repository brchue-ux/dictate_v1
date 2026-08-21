"""The global push-to-talk hotkey.

Windows' own `RegisterHotKey` only reports key-down, so it cannot express
"recording lasts as long as you hold this". The `global_hotkeys` package gives
separate press and release callbacks, which is what push-to-talk needs.
`PinW/whisper-key-local` (MIT) uses the same package for the same reason;
reading it is what pointed here.

**Correction (2026-08-21, PR #30): this is not a `WH_KEYBOARD_LL` hook.**
`global_hotkeys==0.1.7` (`hotkey_checker.py::HotkeyChecker.run`) is a plain
Python thread that calls `win32api.GetAsyncKeyState()` for every registered
combination every 20 ms. It only *behaves* like a hook because that poll is
fast enough and global enough that the difference is invisible in the
ordinary case.

PR #30 also proposed the elevated-window/UIPI privilege boundary as the
leading suspect for toggle-off being missed - unconfirmed there, correctly.
**He has since reproduced it staying in the dictate window, in the terminal,
and on another screen, with identical results, including when he never left
the terminal at all. That rules focus out entirely; the boundary is not the
cause.**

**Root cause, found by driving the real pinned `hotkey_checker.py` source
with a synthetic key-state feed (win32api stubbed) rather than reasoning
about it: `actuate_on_partial_release=False` requires every key of the chord
to read as simultaneously "not pressed" in the same 20 ms poll before the
library will arm itself for the next press.** PR #30's hand trace covered
only the idealised case - all three keys released within the same poll - and
correctly found no problem there. It never tested a release that is not
fully simultaneous. A human letting go of a three-key chord like
Ctrl+Alt+Space essentially never does; and if even one of them (most often a
modifier he is still resting a finger on) never reads as fully up before he
presses the whole chord again, `hotkey_checker.py` never sees the down-edge
of that second press at all: no `press_callback` call, no "toggle hotkey
seen" log line, nothing. This is not focus-dependent - it is a plain
comparison of key states - which is exactly why the UIPI theory's own
disconfirming test could not touch it. Toggle's own internal reset then
never runs until every key of the chord happens to read simultaneously up,
which can outlast the press he actually meant to register.

`actuate_on_partial_release=True` is exactly the fix "hold" mode already
relies on for the mirror-image problem: releasing the *last* key of
Ctrl+Alt+Space should end the recording even though Ctrl and Alt are still
down, because that is how people actually let go of a chord. Toggle mode now
sets it too - not to fire a release callback (it still passes `None` for
that, so toggle's own on/off alternation is unchanged), only to let the
library re-arm the instant any one key of the chord comes up, rather than
waiting for all of them to agree at once. Driving the same synthetic feed
with this flag set confirms the previously-stuck sequence now re-arms
correctly, and that an intentional continuous hold (all three keys reading
down at every poll) is unaffected - the flag only changes when the *reset*
after a release happens, never the press edge. See the PR that made this
change for the full methodology and what still needs his machine.
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

            # actuate_on_partial_release=True - see the module docstring's
            # root-cause note. release_callback stays None: this only changes
            # when the library re-arms itself for the next press, never
            # whether a release fires anything.
            binding = [self.combination, _guard(press), None, True]
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
