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
        recording, is unchanged."""
        seen: list[str] = []
        listener = WindowsHotkeyListener("ctrl + alt + space", "toggle")
        listener.register(lambda: seen.append("start"), lambda: seen.append("stop"))
        _combination, press_cb, _release, _actuate = self._binding()
        press_cb()
        press_cb()
        press_cb()
        self.assertEqual(seen, ["start", "stop", "start"])


if __name__ == "__main__":
    unittest.main()
