"""Holding a mouse button to talk.

Nobody on this build has Windows, a mouse to press, or any way to install a
low-level mouse hook, so what is held here is everything except the hook itself:
which values name a mouse button and which are refused, what the hook is told to
do with each event, what happens to the button's own job, and that the keyboard
chord behind it survives a hook that will not install.

`platform/windows/mouse.py` - the ~40 lines that call Windows - is not exercised
by anything in this file or anywhere else. That is stated in the pull request
rather than implied here.
"""

from __future__ import annotations

import unittest

from dictate import config as config_mod, hotkey_switch
from dictate.errors import ConfigError, MouseHookError
from dictate.platform import hotkey_spec
from dictate.platform.mouse_trigger import DOWN, UP, MouseTrigger
from dictate.platform.trigger_pair import TriggerPair


class WhatNamesAMouseButton(unittest.TestCase):
    def test_the_spellings_a_person_would_write(self):
        for written, expected in (
            ("mouse 4", "mouse4"),
            ("Mouse 4", "mouse4"),
            ("mouse4", "mouse4"),
            ("mouse-4", "mouse4"),
            ("mouse_4", "mouse4"),
            ("MOUSE 5", "mouse5"),
            ("xbutton1", "mouse4"),
            ("x button 2", "mouse5"),
            ("middle", "mouse3"),
            ("middle click", "mouse3"),
            ("middle mouse button", "mouse3"),
            ("mouse wheel", "mouse3"),
            ("scroll wheel click", "mouse3"),
            ("mmb", "mouse3"),
        ):
            with self.subTest(written=written):
                self.assertEqual(hotkey_spec.normalise(written), expected)

    def test_they_are_named_in_the_words_he_would_recognise(self):
        self.assertEqual(hotkey_spec.describe("mouse 4"), "Mouse 4")
        self.assertEqual(hotkey_spec.describe("mouse 5"), "Mouse 5")
        self.assertEqual(hotkey_spec.describe("middle click"),
                         "Middle mouse button")

    def test_a_keyboard_combination_is_not_a_mouse_button(self):
        self.assertIsNone(hotkey_spec.mouse_button("ctrl + alt + space"))
        self.assertFalse(hotkey_spec.is_mouse("ctrl + alt + space"))
        self.assertEqual(hotkey_spec.mouse_button("mouse 4"), "mouse4")

    def test_every_combination_that_worked_before_still_works(self):
        """The mouse is an addition. A config file written last week has to go
        on meaning exactly what it meant last week."""
        for written, expected in (
            ("ctrl + alt + space", "control + alt + space"),
            ("Ctrl-Shift-D", "control + shift + d"),
            ("win + alt + q", "alt + window + q"),
            ("space", "space"),
            ("caps_lock", "caps_lock"),
            ("ALT+F4", "alt + f4"),
        ):
            with self.subTest(written=written):
                self.assertEqual(hotkey_spec.normalise(written), expected)

    def test_the_left_and_right_buttons_are_refused_and_say_why(self):
        """Holding a button to talk means swallowing it, and a machine whose
        left button does nothing cannot be used - including to stop dictate."""
        for written in ("mouse 1", "left click", "mouse 2", "right button"):
            with self.subTest(written=written):
                with self.assertRaises(ConfigError) as caught:
                    hotkey_spec.normalise(written)
                report = caught.exception.report()
                self.assertIn("mouse 4", report)
                self.assertIn("cannot be used", report)

    def test_a_button_windows_does_not_report_names_the_ones_it_does(self):
        for written in ("mouse 9", "mouse", "xbutton3", "button 7"):
            with self.subTest(written=written):
                with self.assertRaises(ConfigError) as caught:
                    hotkey_spec.normalise(written)
                self.assertIn("mouse 4", caught.exception.report())
                self.assertIn("mouse 5", caught.exception.report())

    def test_a_mouse_button_is_held_on_its_own(self):
        """Ctrl + Mouse 4 is the three-button chord he asked to get away from,
        and it would make the mouse hook read the keyboard as well."""
        with self.assertRaises(ConfigError) as caught:
            hotkey_spec.normalise("ctrl + mouse 4")
        self.assertIn("on its own", caught.exception.report())
        self.assertIn('"mouse 4"', caught.exception.report())

    def test_the_config_refuses_a_bad_button_at_startup_not_mid_sentence(self):
        with self.assertRaises(ConfigError) as caught:
            config_mod.validate(config_mod.from_mapping(
                {"hotkey": {"combination": "mouse 7"}}))
        self.assertIn("mouse 4", caught.exception.report())

    def test_a_mouse_button_cannot_be_a_toggle(self):
        """Everything dictate does with the button is decided by how long it is
        held, which a toggle has no notion of."""
        with self.assertRaises(ConfigError) as caught:
            config_mod.validate(config_mod.from_mapping(
                {"hotkey": {"combination": "mouse 4", "mode": "toggle"}}))
        self.assertIn("mode", caught.exception.report())

    def test_the_fallback_may_not_itself_be_a_mouse_button(self):
        with self.assertRaises(ConfigError) as caught:
            config_mod.validate(config_mod.from_mapping(
                {"hotkey": {"combination": "mouse 4",
                            "keyboard_fallback": "mouse 5"}}))
        self.assertIn("keyboard", caught.exception.report())

    def test_a_keyboard_trigger_is_not_troubled_by_any_of_this(self):
        cfg = config_mod.validate(config_mod.from_mapping(
            {"hotkey": {"combination": "ctrl + alt + space", "mode": "toggle"}}))
        self.assertEqual(cfg.hotkey.mode, "toggle")


class TheSwallowRule(unittest.TestCase):
    """What the hook is told to do with each event.

    The whole design question is in this class: Mouse 4 is Back everywhere, so
    swallowing it always breaks Back all day and passing it through always
    navigates Back on every dictation.
    """

    def setUp(self):
        self.trigger = MouseTrigger("mouse4", min_hold_s=0.35)
        self.trigger.arm()

    def test_holding_it_is_a_dictation_and_the_app_never_hears_the_button(self):
        down = self.trigger.event(DOWN, "mouse4", now=10.0)
        self.assertEqual((down.swallow, down.press, down.replay),
                         (True, True, False))
        up = self.trigger.event(UP, "mouse4", now=11.5)
        self.assertEqual((up.swallow, up.release, up.replay), (True, True, False))

    def test_a_quick_click_is_given_back_to_the_app(self):
        """Shorter than an utterance, so it was a click: dictate swallows his
        button and sends the app one of its own."""
        self.trigger.event(DOWN, "mouse4", now=10.0)
        up = self.trigger.event(UP, "mouse4", now=10.08)
        self.assertTrue(up.swallow)
        self.assertTrue(up.replay)
        self.assertTrue(up.release)

    def test_the_boundary_is_the_length_of_an_utterance(self):
        self.trigger.event(DOWN, "mouse4", now=0.0)
        self.assertTrue(self.trigger.event(UP, "mouse4", now=0.349).replay)
        self.trigger.event(DOWN, "mouse4", now=1.0)
        self.assertFalse(self.trigger.event(UP, "mouse4", now=1.35).replay)

    def test_turning_click_through_off_keeps_the_button_entirely(self):
        trigger = MouseTrigger("mouse4", click_through=False, min_hold_s=0.35)
        trigger.arm()
        trigger.event(DOWN, "mouse4", now=0.0)
        up = trigger.event(UP, "mouse4", now=0.05)
        self.assertTrue(up.swallow)
        self.assertTrue(up.release)
        self.assertFalse(up.replay)

    def test_the_replayed_click_is_not_read_as_a_new_press(self):
        """It goes back through the same hook. Recognised by dictate's own tag
        rather than by "it was injected", so a mouse driver's own events still
        work as a trigger."""
        for kind in (DOWN, UP):
            action = self.trigger.event(kind, "mouse4", now=0.0, ours=True)
            self.assertEqual((action.swallow, action.press, action.release),
                             (False, False, False))

    def test_every_other_button_goes_past_untouched(self):
        for button in ("mouse3", "mouse5"):
            with self.subTest(button=button):
                action = self.trigger.event(DOWN, button, now=0.0)
                self.assertFalse(action.swallow)
                self.assertFalse(action.press)

    def test_nothing_is_swallowed_before_dictate_is_listening(self):
        trigger = MouseTrigger("mouse4")
        action = trigger.event(DOWN, "mouse4", now=0.0)
        self.assertFalse(action.swallow)
        self.assertFalse(action.press)

    def test_a_button_already_held_when_dictate_starts_keeps_its_own_up(self):
        """We never saw its down, so the app did. Swallowing the up would leave
        that app believing the button is still down for the rest of the day."""
        trigger = MouseTrigger("mouse4")
        trigger.arm()   # he is holding it right now
        up = trigger.event(UP, "mouse4", now=1.0)
        self.assertFalse(up.swallow)
        self.assertFalse(up.release)

    def test_a_button_still_held_when_dictate_stops_ends_the_utterance(self):
        self.trigger.event(DOWN, "mouse4", now=0.0)
        parting = self.trigger.disarm()
        self.assertTrue(parting.release)
        # Never a replay: he is holding it because he is talking, and a click
        # nobody asked for on the way out would be dictate navigating Back as
        # it shut down.
        self.assertFalse(parting.replay)
        self.assertFalse(parting.swallow)

    def test_stopping_when_nothing_is_held_says_nothing(self):
        self.assertFalse(self.trigger.disarm().release)

    def test_a_second_down_does_not_start_a_second_utterance(self):
        self.trigger.event(DOWN, "mouse4", now=0.0)
        again = self.trigger.event(DOWN, "mouse4", now=0.1)
        self.assertTrue(again.swallow)      # the app must not see half a pair
        self.assertFalse(again.press)
        # and the utterance is still the first one: it is long by now.
        self.assertFalse(self.trigger.event(UP, "mouse4", now=1.0).replay)

    def test_disarming_forgets_the_press_so_a_restart_is_clean(self):
        self.trigger.event(DOWN, "mouse4", now=0.0)
        self.trigger.disarm()
        self.trigger.arm()
        self.assertFalse(self.trigger.holding)
        self.assertFalse(self.trigger.event(UP, "mouse4", now=1.0).swallow)


class _FakeHalf:
    """One trigger, doing nothing. Not in `src/` - see tests/fakes.py."""

    def __init__(self, name: str, *, fail_on: str = "") -> None:
        self.name = name
        self.fail_on = fail_on
        self.registered = False
        self.started = False
        self.stopped = 0
        self.press = None
        self.release = None
        self.on_lost = None

    def register(self, on_press, on_release) -> None:
        if self.fail_on == "register":
            raise MouseHookError("Windows would not have it.", "Try the chord.")
        self.press, self.release = on_press, on_release
        self.registered = True

    def start(self) -> None:
        if self.fail_on == "start":
            raise MouseHookError("Windows would not have it.", "Try the chord.")
        self.started = True

    def stop(self) -> None:
        self.stopped += 1
        self.started = False

    @property
    def describe(self) -> str:
        return self.name


class TheKeyboardBehindIt(unittest.TestCase):
    """A mouse trigger never comes on its own: the chord is registered too, and
    is what makes a hook that fails a degraded dictate rather than a dead one."""

    def setUp(self):
        self.events = []
        self.mouse = _FakeHalf("Mouse 4 (hold to talk)")
        self.keyboard = _FakeHalf("Ctrl + Alt + Space (hold to talk)")
        self.notices = []

    def _pair(self):
        pair = TriggerPair(self.mouse, self.keyboard,
                           notify=lambda level, msg: self.notices.append((level, msg)))
        pair.register(lambda: self.events.append("press"),
                      lambda: self.events.append("release"))
        pair.start()
        return pair

    def test_both_halves_are_live_and_either_one_dictates(self):
        pair = self._pair()
        self.assertTrue(self.mouse.started and self.keyboard.started)
        self.mouse.press()
        self.mouse.release()
        self.keyboard.press()
        self.keyboard.release()
        self.assertEqual(self.events, ["press", "release", "press", "release"])
        self.assertIn("Mouse 4", pair.describe)
        self.assertIn("Ctrl + Alt + Space", pair.describe)

    def test_the_half_that_started_an_utterance_is_the_only_one_that_ends_it(self):
        """Two triggers, one pipeline: a chord tapped while the button is held
        would otherwise end the sentence he is in the middle of."""
        self._pair()
        self.mouse.press()
        self.keyboard.press()
        self.keyboard.release()
        self.assertEqual(self.events, ["press"])
        self.mouse.release()
        self.assertEqual(self.events, ["press", "release"])

    def test_a_hook_windows_will_not_install_leaves_the_chord_working(self):
        for failing in ("register", "start"):
            with self.subTest(failing=failing):
                self.mouse = _FakeHalf("Mouse 4", fail_on=failing)
                self.keyboard = _FakeHalf("Ctrl + Alt + Space")
                with self.assertRaises(MouseHookError) as caught:
                    self._pair()
                self.assertTrue(self.keyboard.registered)
                self.assertTrue(self.keyboard.started)
                self.assertIn("Ctrl + Alt + Space", caught.exception.report())

    def test_a_failed_trigger_does_not_claim_to_have_a_mouse_button(self):
        self.mouse = _FakeHalf("Mouse 4", fail_on="start")
        self.keyboard = _FakeHalf("Ctrl + Alt + Space")
        with self.assertRaises(MouseHookError):
            self._pair()
        pair = TriggerPair(self.mouse, self.keyboard)
        self.assertIn("NOT working", pair.describe)

    def test_the_chord_still_dictates_after_the_hook_was_refused(self):
        self.mouse = _FakeHalf("Mouse 4", fail_on="start")
        self.keyboard = _FakeHalf("Ctrl + Alt + Space")
        with self.assertRaises(MouseHookError):
            self._pair()
        self.keyboard.press()
        self.keyboard.release()
        self.assertEqual(self.events, ["press", "release"])

    def test_a_hook_lost_later_is_said_out_loud(self):
        """The failure this cannot detect from inside. Reported at the loudest
        level there is, because that is what turns the tray icon red - the only
        surface a copy started at logon has."""
        pair = self._pair()
        self.mouse.on_lost("The hook was removed.")
        self.assertEqual([level for level, _ in self.notices], ["error"])
        self.assertIn("Ctrl + Alt + Space", self.notices[0][1])
        self.assertIn("NOT working", pair.describe)

    def test_stopping_stops_both(self):
        pair = self._pair()
        pair.stop()
        self.assertEqual((self.mouse.stopped, self.keyboard.stopped), (1, 1))

    def test_one_that_was_stopped_can_be_put_back(self):
        """`app._restore_hotkey` puts the previous trigger back by registering
        it again. A half that believed it was still registered would come back
        bound to nothing, which is dictate with no way in."""
        pair = self._pair()
        pair.stop()
        self.keyboard.registered = False
        pair.register(lambda: self.events.append("press"),
                      lambda: self.events.append("release"))
        pair.start()
        self.assertTrue(self.keyboard.registered)
        self.assertTrue(self.keyboard.started)
        self.keyboard.press()
        self.assertEqual(self.events, ["press"])


class WhatHeIsToldBeforeChoosing(unittest.TestCase):
    """Every button costs him something. He should be able to read the cost on
    the menu he chooses it from, not discover it afterwards."""

    def test_all_three_buttons_are_offered_by_the_tray(self):
        offered = {hotkey_spec.normalise(c) for c, _ in hotkey_switch.CHOICES
                   if hotkey_spec.is_mouse(c)}
        self.assertEqual(offered, set(hotkey_spec.MOUSE_BUTTONS))

    def test_each_one_says_what_it_costs_on_the_menu_line(self):
        for combination, why in hotkey_switch.CHOICES:
            if hotkey_spec.is_mouse(combination):
                with self.subTest(combination=combination):
                    self.assertTrue(why)

    def test_the_middle_button_names_autoscroll_where_he_will_read_it(self):
        """The one thing that is specific to the wheel: push-to-talk means
        holding a button for seconds, and holding the wheel is autoscroll."""
        menu = dict(hotkey_switch.CHOICES)["middle mouse button"]
        self.assertIn("autoscroll", menu)
        cost = hotkey_switch.MOUSE_COST["mouse3"]
        self.assertIn("autoscroll", cost.held)
        self.assertIn("autoscroll", cost.cost)

    def test_every_button_has_its_cost_written_down_once(self):
        self.assertEqual(set(hotkey_switch.MOUSE_COST),
                         set(hotkey_spec.MOUSE_BUTTONS))
        for button, cost in hotkey_switch.MOUSE_COST.items():
            with self.subTest(button=button):
                self.assertTrue(cost.normally and cost.held and cost.cost)

    def test_the_note_says_the_hook_the_button_the_cost_and_the_way_back(self):
        lines = " ".join(hotkey_switch.trigger_note(
            "mouse 4", keyboard_fallback="ctrl + alt + space"))
        self.assertIn("low-level mouse hook", lines)
        self.assertIn("Back", lines)
        self.assertIn("Ctrl + Alt + Space", lines)

    def test_the_button_that_is_not_the_recommendation_says_which_is(self):
        """He asked about the wheel. The answer is the cost, and then which one
        this project would pick and why - said where he is choosing."""
        wheel = " ".join(hotkey_switch.trigger_note("middle mouse button"))
        self.assertIn("would pick Mouse 4", wheel)
        self.assertNotIn("would pick", " ".join(
            hotkey_switch.trigger_note("mouse 4")))

    def test_the_note_changes_when_the_button_is_dictates_alone(self):
        lines = " ".join(hotkey_switch.trigger_note(
            "mouse 4", keyboard_fallback="", click_through=False))
        self.assertIn("belongs to dictate entirely", lines)

    def test_a_keyboard_chord_needs_no_explaining(self):
        self.assertEqual(hotkey_switch.trigger_note("ctrl + alt + space"), [])
        self.assertEqual(hotkey_switch.trigger_note("++"), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
