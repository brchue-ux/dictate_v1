"""`WindowsHotkeyListener` builds the binding it hands to the third-party
`global_hotkeys` package. The package itself is Windows-only glue - like
`platform/windows/mouse.py` and `platform/windows/tray.py`, nothing about the
real poll thread runs off Windows - but which flags dictate asks it for is
dictate's own decision, and that is what this file tests, with a minimal
stand-in for the package's module-level API.
"""

from __future__ import annotations

import sys
import types
import unittest

from dictate.platform.windows.hotkey import WindowsHotkeyListener


class FakeClock:
    """A controllable clock so a test can put two presses on either side of
    the bounce window without a real `time.sleep`."""

    def __init__(self, start: float = 0.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakeGlobalHotkeys(types.ModuleType):
    """Records exactly the binding list `register()` builds - the one thing
    this file needs to see, never called for real off Windows."""

    def __init__(self) -> None:
        super().__init__("global_hotkeys")
        self.registered: list[list] = []

    def register_hotkeys(self, bindings) -> None:
        self.registered.extend(bindings)

    def start_checking_hotkeys(self) -> None:
        pass

    def stop_checking_hotkeys(self) -> None:
        pass

    def clear_hotkeys(self) -> None:
        pass


class HotkeyListenerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeGlobalHotkeys()
        self._saved = sys.modules.get("global_hotkeys")
        sys.modules["global_hotkeys"] = self.fake
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        if self._saved is None:
            sys.modules.pop("global_hotkeys", None)
        else:
            sys.modules["global_hotkeys"] = self._saved

    def _binding(self):
        [binding] = self.fake.registered
        return binding


class TogglePartialRelease(HotkeyListenerTestCase):
    """The bug: `actuate_on_partial_release=False` requires every key of the
    chord to read as simultaneously "not pressed" in the same poll before
    the library re-arms for a second press - which a human releasing a
    multi-key chord like Ctrl+Alt+Space essentially never does, most often
    because a modifier is still resting under a finger. `True` is exactly
    what "hold" mode already relies on for the mirror-image case (ending a
    recording on the *last* key of the chord to lift); see the module
    docstring for how this was established against the real pinned library
    source rather than guessed."""

    def test_toggle_mode_asks_for_partial_release(self) -> None:
        listener = WindowsHotkeyListener("ctrl + alt + space", "toggle")
        listener.register(lambda: None, lambda: None)
        _combination, _press, release_cb, actuate_on_partial_release = self._binding()
        self.assertTrue(actuate_on_partial_release)
        # Unchanged: toggle's own on/off alternation is driven entirely by
        # the press callback. A release callback here would fire on every
        # partial release, not just the ones toggle means as "off".
        self.assertIsNone(release_cb)

    def test_hold_mode_still_asks_for_partial_release(self) -> None:
        listener = WindowsHotkeyListener("ctrl + alt + space", "hold")
        listener.register(lambda: None, lambda: None)
        _combination, _press, release_cb, actuate_on_partial_release = self._binding()
        self.assertTrue(actuate_on_partial_release)
        self.assertIsNotNone(release_cb)

    def test_toggle_press_still_alternates_start_and_stop(self) -> None:
        """The flag only changes when the library re-arms itself - toggle's
        own alternation, which is what actually starts and stops a
        recording, is unchanged - for presses far enough apart to be
        genuinely distinct (see ToggleBounceDebounce for the other case)."""
        seen: list[str] = []
        clock = FakeClock()
        listener = WindowsHotkeyListener("ctrl + alt + space", "toggle", clock=clock)
        listener.register(lambda: seen.append("start"), lambda: seen.append("stop"))
        _combination, press_cb, _release, _actuate = self._binding()
        press_cb()
        clock.advance(1.0)
        press_cb()
        clock.advance(1.0)
        press_cb()
        self.assertEqual(seen, ["start", "stop", "start"])


class ToggleBounceDebounce(HotkeyListenerTestCase):
    """The regression: his trigger holds the whole chord down synthetically
    for as long as the button is held, rather than a human tapping it. Driven
    against the real pinned library source (see the module docstring's
    "Regression" section), a single missed 20ms poll of just one key of an
    otherwise still-held chord is enough, under `actuate_on_partial_release=
    True`, to make the library reset and immediately re-detect the same
    still-down chord as a brand new press - one physical hold, two
    `press_callback` calls, with no release from him in between. dictate
    cannot fix the library's poll, so it debounces the symptom: two press
    edges closer together than `_BOUNCE_WINDOW_S` collapse into one."""

    def test_a_bounce_within_the_window_is_ignored(self) -> None:
        seen: list[str] = []
        clock = FakeClock()
        listener = WindowsHotkeyListener("ctrl + alt + space", "toggle", clock=clock)
        listener.register(lambda: seen.append("start"), lambda: seen.append("stop"))
        _combination, press_cb, _release, _actuate = self._binding()
        press_cb()
        clock.advance(0.02)  # one library poll tick - the measured bounce gap
        press_cb()
        self.assertEqual(seen, ["start"])

    def test_a_press_after_the_window_registers_normally(self) -> None:
        seen: list[str] = []
        clock = FakeClock()
        listener = WindowsHotkeyListener("ctrl + alt + space", "toggle", clock=clock)
        listener.register(lambda: seen.append("start"), lambda: seen.append("stop"))
        _combination, press_cb, _release, _actuate = self._binding()
        press_cb()
        clock.advance(0.2)  # well past the bounce window
        press_cb()
        self.assertEqual(seen, ["start", "stop"])

    def test_hold_mode_is_not_debounced(self) -> None:
        """Deliberately unguarded - see the module docstring's "Hold mode"
        section: the same risk exists there in principle, but it is not what
        was reported, and is left alone rather than changed on a guess."""
        seen: list[str] = []
        clock = FakeClock()
        listener = WindowsHotkeyListener("ctrl + alt + space", "hold", clock=clock)
        listener.register(lambda: seen.append("start"), lambda: seen.append("stop"))
        _combination, press_cb, release_cb, _actuate = self._binding()
        press_cb()
        press_cb()
        self.assertEqual(seen, ["start", "start"])
        release_cb()
        release_cb()
        self.assertEqual(seen, ["start", "start", "stop", "stop"])


if __name__ == "__main__":
    unittest.main()
