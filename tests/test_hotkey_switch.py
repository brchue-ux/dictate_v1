"""Changing the hotkey: the decisions, and the one line it writes.

Nobody here has a notification area to right-click or a global hotkey to press.
What can be held here is everything except the two Windows calls: whether a
change should be attempted, what he is told when it is refused or when it cannot
be made to stick, and that the file it writes is his file with one line
different.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dictate import config as config_mod, config_edit, hotkey_switch
from dictate.errors import ConfigError
from dictate.platform import hotkey_spec

REPO = Path(__file__).resolve().parent.parent
EXAMPLE = REPO / "config" / "dictate.example.toml"


class WhatIsOffered(unittest.TestCase):
    def test_every_offered_combination_is_one_dictate_can_parse(self):
        """The list is the whole point: a combination that will not parse would
        be an item on the menu that cannot work."""
        for combination, why in hotkey_switch.CHOICES:
            with self.subTest(combination=combination):
                config_mod.validate(config_mod.from_mapping(
                    {"hotkey": {"combination": combination}}))
                self.assertTrue(why)

    def test_they_are_spelled_with_letters_and_the_space_bar(self):
        """Nothing here may depend on a key name the hotkey library might not
        know - an F-key or a media key. Anything else is his to type.

        The mouse buttons on the list are exempt and have their own rule: they
        do not go through that library at all (`platform/windows/mouse.py`), and
        `test_mouse_trigger.py` holds what they are allowed to be.
        """
        for combination, _ in hotkey_switch.CHOICES:
            if hotkey_spec.is_mouse(combination):
                continue
            key = combination.split("+")[-1].strip()
            with self.subTest(combination=combination):
                self.assertTrue(key == "space" or (len(key) == 1 and key.isalpha()))

    def test_the_default_hotkey_is_on_the_list(self):
        offered = [c for c, _ in hotkey_switch.CHOICES]
        self.assertIn(config_mod.HotkeyConfig().combination, offered)

    def test_the_command_is_the_typed_form_of_the_same_thing(self):
        self.assertEqual(hotkey_switch.command_for("ctrl + alt + d"),
                         'dictate hotkey "ctrl + alt + d"')

    def test_his_own_hotkey_is_always_among_them(self):
        listed = hotkey_switch.choices_for("win + alt + q")
        self.assertEqual([c for c, _, current in listed if current],
                         ["alt + window + q"])

    def test_a_spelling_of_the_same_hotkey_is_not_a_second_entry(self):
        """"ctrl + alt + space" and "control+alt+space" are one hotkey."""
        listed = hotkey_switch.choices_for("CONTROL+ALT+SPACE")
        self.assertEqual(len(listed), len(hotkey_switch.CHOICES))
        self.assertEqual([c for c, _, current in listed if current],
                         ["ctrl + alt + space"])

    def test_an_unreadable_hotkey_ticks_nothing_and_hides_nothing(self):
        listed = hotkey_switch.choices_for("ctrl + alt")
        self.assertEqual(len(listed), len(hotkey_switch.CHOICES))
        self.assertEqual([c for c, _, current in listed if current], [])


class WhenItIsAttempted(unittest.TestCase):
    def test_an_ordinary_change_is_normalised_and_allowed(self):
        decision = hotkey_switch.decide("ctrl + alt + space", "Ctrl-Shift-D",
                                        recording=False)
        self.assertTrue(decision.act)
        self.assertEqual(decision.combination, "control + shift + d")

    def test_the_hotkey_he_already_has_is_not_a_change(self):
        decision = hotkey_switch.decide("ctrl + alt + space",
                                        "control + alt + space", recording=False)
        self.assertFalse(decision.act)
        self.assertIn("already", decision.message)
        self.assertEqual(decision.level, "info")

    def test_not_in_the_middle_of_a_sentence(self):
        """The hotkey being taken away is the one he is holding down. Doing it
        now would end the utterance and lose what he is saying."""
        decision = hotkey_switch.decide("ctrl + alt + space", "ctrl + alt + d",
                                        recording=True)
        self.assertFalse(decision.act)
        self.assertEqual(decision.level, "warning")
        self.assertIn("Ctrl + Alt + D", decision.message)

    def test_a_combination_that_cannot_be_parsed_says_why(self):
        decision = hotkey_switch.decide("ctrl + alt + space", "ctrl + alt",
                                        recording=False)
        self.assertFalse(decision.act)
        self.assertEqual(decision.level, "error")
        self.assertIn("modifier", decision.message)

    def test_a_current_hotkey_that_is_nonsense_does_not_block_the_fix(self):
        """A config with an unusable hotkey in it is exactly when he most needs
        to be able to choose a working one from the menu."""
        decision = hotkey_switch.decide("++", "ctrl + alt + d", recording=False)
        self.assertTrue(decision.act)


class WhatHeIsTold(unittest.TestCase):
    def test_a_change_that_was_written_down_says_where(self):
        level, message = hotkey_switch.applied(
            "control + alt + d", config_path=r"C:\x\dictate.toml", persisted=True)
        self.assertEqual(level, "info")
        self.assertIn("Ctrl + Alt + D", message)
        self.assertIn("dictate.toml", message)

    def test_a_change_that_could_not_be_written_says_it_will_not_last(self):
        """The worst version of this is a hotkey that works this evening and is
        back to the old one tomorrow, with nothing having said so."""
        level, message = hotkey_switch.applied(
            "control + alt + d", config_path=r"C:\x\dictate.toml", persisted=False)
        self.assertEqual(level, "warning")
        self.assertIn("comes back", message)

    def test_with_no_config_file_it_names_the_command_that_makes_one(self):
        level, message = hotkey_switch.applied(
            "control + alt + d", config_path=None, persisted=False)
        self.assertEqual(level, "warning")
        self.assertIn("dictate init", message)

    def test_a_refusal_says_the_old_one_is_still_the_one(self):
        message = hotkey_switch.refused("control + alt + d", "ctrl + alt + space",
                                        "Windows says no")
        self.assertIn("Ctrl + Alt + D", message)
        self.assertIn("Ctrl + Alt + Space", message)
        self.assertIn("Nothing was changed", message)


class TheOneLineItWrites(unittest.TestCase):
    """`config_edit`: his file, one line different, everything else identical."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / "dictate.toml"

    def test_the_shipped_example_still_loads_after_it_is_edited(self):
        """The file he actually has is the one `dictate init` copied out of the
        repository, so that is the file this is tried on."""
        self.path.write_bytes(EXAMPLE.read_bytes())
        config_edit.write_string(self.path, "hotkey", "combination",
                                 "control + shift + d")
        cfg = config_mod.load(self.path)
        self.assertEqual(cfg.hotkey.combination, "control + shift + d")

    def test_and_nothing_else_in_it_moved(self):
        self.path.write_bytes(EXAMPLE.read_bytes())
        before = EXAMPLE.read_text(encoding="utf-8").splitlines()
        config_edit.write_string(self.path, "hotkey", "combination",
                                 "control + shift + d")
        after = self.path.read_text(encoding="utf-8").splitlines()
        differing = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
        self.assertEqual(len(differing), 1, differing)
        self.assertEqual(len(before), len(after))
        self.assertIn("combination", after[differing[0]])

    def test_his_comments_and_his_line_endings_survive(self):
        original = ('# mine\r\n[hotkey]\r\n# hold this one\r\n'
                    'combination = "ctrl + alt + space"   # <<< CHECK THIS\r\n'
                    'mode = "hold"\r\n')
        self.path.write_bytes(original.encode("utf-8"))
        config_edit.write_string(self.path, "hotkey", "combination", "ctrl + alt + d")
        text = self.path.read_bytes().decode("utf-8")
        self.assertIn('combination = "ctrl + alt + d"   # <<< CHECK THIS\r\n', text)
        self.assertIn("# hold this one", text)
        self.assertNotIn("\n\n", text.replace("\r\n", "\n\r"))  # no line ending changed

    def test_a_byte_order_mark_is_still_there_afterwards(self):
        """Notepad and Windows PowerShell both write one; `config.load` reads
        utf-8-sig for that reason. Dropping it would be a change he did not ask
        for to a file he did not ask us to rewrite."""
        self.path.write_bytes(b"\xef\xbb\xbf" + b'[hotkey]\ncombination = "ctrl + alt + space"\n')
        config_edit.write_string(self.path, "hotkey", "combination", "ctrl + alt + d")
        self.assertTrue(self.path.read_bytes().startswith(b"\xef\xbb\xbf"))
        self.assertEqual(config_mod.load(self.path).hotkey.combination,
                         "ctrl + alt + d")

    def test_a_missing_key_is_added_to_its_section(self):
        self.path.write_text('[hotkey]\nmode = "hold"\n', encoding="utf-8")
        config_edit.write_string(self.path, "hotkey", "combination", "ctrl + alt + d")
        cfg = config_mod.load(self.path)
        self.assertEqual(cfg.hotkey.combination, "ctrl + alt + d")
        self.assertEqual(cfg.hotkey.mode, "hold")

    def test_a_missing_section_is_added_at_the_end(self):
        """At the end, and as a table header, which is safe wherever the rest
        of the file has got to - the trap every rules file warns about is a
        plain setting written after a [[table]], and this never writes one."""
        self.path.write_text('[audio]\ndevice = ""\n', encoding="utf-8")
        config_edit.write_string(self.path, "hotkey", "combination", "ctrl + alt + d")
        cfg = config_mod.load(self.path)
        self.assertEqual(cfg.hotkey.combination, "ctrl + alt + d")
        self.assertEqual(cfg.audio.device, "")

    def test_an_array_of_tables_with_the_same_name_is_not_mistaken_for_it(self):
        text = '[[hotkey]]\ncombination = "wrong"\n'
        out = config_edit.set_string(text, "hotkey", "combination", "right")
        self.assertIn('combination = "wrong"', out)
        self.assertIn('[hotkey]\ncombination = "right"', out)

    def test_only_the_section_it_was_asked_about_is_touched(self):
        text = ('[audio]\ncombination = "not this one"\n\n'
                '[hotkey]\ncombination = "this one"\n')
        out = config_edit.set_string(text, "hotkey", "combination", "changed")
        self.assertIn('combination = "not this one"', out)
        self.assertIn('combination = "changed"', out)

    def test_a_quote_in_the_value_cannot_break_the_file(self):
        text = '[hotkey]\ncombination = "ctrl + alt + space"\n'
        out = config_edit.set_string(text, "hotkey", "combination", 'a"b\\c')
        self.path.write_text(out, encoding="utf-8")
        self.assertEqual(config_mod.load(self.path).hotkey.combination, 'a"b\\c')

    def test_a_file_that_cannot_be_read_says_what_to_type_instead(self):
        with self.assertRaises(ConfigError) as caught:
            config_edit.write_string(self.dir / "nope.toml", "hotkey",
                                     "combination", "ctrl + alt + d")
        self.assertIn("combination", caught.exception.report())

    def test_nothing_is_left_half_written(self):
        """`os.replace`, like the dictation history: the file is the old one or
        the new one and never half of either."""
        source = Path(config_edit.__file__).read_text(encoding="utf-8")
        self.assertIn("os.replace", source)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
