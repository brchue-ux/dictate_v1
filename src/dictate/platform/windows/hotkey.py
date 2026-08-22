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

**Open question, found immediately after: the fix above assumes physical
fingers, and he does not use them for this trigger.** The button is a
Logitech G502 mapped through G HUB to synthesize Ctrl+Alt+Space -
reportedly a couple of milliseconds of key-down, not a human hold. That is
short enough to fall entirely between two 20 ms polls, on its own, with
nothing to do with release timing or window focus - a different, structural
blind spot that `actuate_on_partial_release` cannot touch, because that flag
only changes what happens *after* a poll has already caught the chord down,
and here a poll may never catch it at all. Checked and ruled out as a
contributing cause: `sherpa-onnx`'s streaming calls (`accept_waveform`,
`is_ready`, `decode_stream`, `get_result`) all declare
`py::call_guard<py::gil_scoped_release>()` at the pinned `>=1.13.5` floor
(read from the real source, not assumed), so decoding captions cannot hold
the GIL and starve this thread; `overlay.set_state` is a non-blocking queue
put, never a cross-thread wait. Still not established: why the first
(start) press is reported reliable and the second (stop) press is not -
that asymmetry is the open part. `pynput`'s Windows backend
(`SetWindowsHookEx(WH_KEYBOARD_LL, ...)`, actively maintained) is the
concrete shape a real hook replacement would take if one is needed - a
*sampled* key-state query structurally cannot see a keystroke shorter than
its sampling interval; an *event-driven* hook does not have that failure
mode at all, at any duration. See the PR for the cheap checks that would
settle this before that scale of change is undertaken.

**Regression, found immediately after shipping the above (2026-08-21): his
trigger does not use physical fingers at all, and `actuate_on_partial_release
=True` double-fires a held synthetic chord.** His toggle combination is not
pressed by hand - the G502/G HUB macro genuinely holds Ctrl, Alt and Space
down for as long as the button is down, all three released together when he
lets go. That is a *longer, continuous* hold than any human tap, which
matters: driving the real pinned `hotkey_checker.py` with a synthetic
key-state feed (methodology unchanged from the PR above; this file's own
`AGENTS.md` entry has the counts) shows that neither an atomic hold+release
nor a non-simultaneous ("staggered") release causes a second `press_callback`
- only a **single missed poll of ANY ONE key of the chord while the other two
are still read as down** does. With `actuate_on_partial_release=False` (the
pre-PR-#31 setting) that same single-poll dip is harmless: the library
requires every key to read simultaneously up before it resets, so a
transient one-key miss changes nothing. With `True`, that one dip is
indistinguishable from a real release - the library resets its internal
press state immediately, and the very next poll (which sees the chord still
down, because it never actually went anywhere) is read as a brand new press.
One physical hold, one missed 20ms poll, two `press_callback` calls - his
toggle flips on then immediately back off, mid-hold, with no release from
him in between. A long synthesized hold spends far more polls continuously
down than a human's quick tap-and-release, which is what gives a single poll
far more chances to miss - not a difference in the release itself, which the
same test shows is not the risk.

**Fix: dictate debounces toggle's own press edge, in `register()`'s toggle
closure, rather than trusting the library's edge is real.** `_BOUNCE_WINDOW_S`
below is chosen from that same test: the spurious second press lands one poll
(~20ms) after the first, and no legitimate second press - human or macro -
plausibly lands that close together on purpose. This targets only the
*symptom* dictate can observe (two press callbacks too close together), not
the poll miss itself, which happens inside the library and pywin32 and is not
observable from here. `actuate_on_partial_release` stays `True` for toggle:
turning it back off would resurrect the never-re-arms fault fixed above for
every keyboard user, which is strictly worse - that fault drops a real press
silently forever, where a debounced bounce merely delays the next real one by
under `_BOUNCE_WINDOW_S`. `tests/test_hotkey_listener.py::ToggleBounceDebounce`
holds this against an injected clock, since the real interval is real wall
time this file cannot control off Windows.

**Hold mode carries the identical structural risk and is deliberately left
alone.** It has asked for `actuate_on_partial_release=True` since before
PR #30 (see above) for its own, unrelated reason (ending on the last key of
the chord to lift), and the same single-poll-miss mechanism applies equally
to *its* press/release pair - a synthesized long hold could, in principle,
see a spurious stop-then-restart mid-recording. Nobody has reported that, and
he does not use hold mode for the macro trigger this was diagnosed against,
so it is not fixed here; if it is ever reported, this section and the
debounce shape above are the starting point.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from ...errors import MissingDependencyError, PlatformUnsupportedError
from ..hotkey_spec import describe, normalise

log = logging.getLogger(__name__)

#: See the module docstring's "Regression" section. Measured against the real
#: pinned library source, the library's own spurious second press lands one
#: 20ms poll after the first; this is comfortably above that and comfortably
#: below any interval a real second press - human or macro - would use.
_BOUNCE_WINDOW_S = 0.15


class WindowsHotkeyListener:
    """Implements `platform.base.HotkeyListener`."""

    def __init__(self, combination: str, mode: str = "hold",
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.combination = normalise(combination)
        self.pretty = describe(combination)
        self.mode = mode
        self._started = False
        self._api = None
        self._toggle_on = False
        self._clock = clock
        self._last_toggle_press: float | None = None

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
                now = self._clock()
                last = self._last_toggle_press
                if last is not None and (now - last) < _BOUNCE_WINDOW_S:
                    # See the module docstring's "Regression" section: the
                    # library itself cannot tell a real second press from its
                    # own single-poll bounce on a held synthetic chord, so
                    # dictate does not trust that this edge is real.
                    log.debug("toggle hotkey press ignored: %.3fs since the "
                              "last one, inside the bounce window", now - last)
                    return
                self._last_toggle_press = now
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
