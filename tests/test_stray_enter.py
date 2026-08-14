"""The stray Enter: where it came from, and why nothing can send one now.

The product owner dictates into a terminal, and reported that dictate was
"pasting things in and then pressing enter" - which in a terminal runs whatever
is on the command line. He did not notice at the time, which is what makes it
worse than every defect before it.

**Where it came from.** whisper.cpp's server writes one line per segment:
`output_str` in `examples/server/server.cpp` is
`result << speaker << text << "\\n"` for every segment, and that whole string is
the `text` field of the JSON reply. So an utterance Whisper split into two
segments - two sentences, which is most of them - came back as
`"Sentence one.\\nSentence two.\\n"`. The cleanup pass preserves interior
newlines on purpose (it collapses `[ \\t]`, never `\\s`), and
`injection_plan.plan_text` turns a newline into a Return keypress because that
is the only way to put a line break into another application. One dictation, one
Enter, in the middle of his command line.

`WhereItCameFrom` is that chain, from the server's own reply to the key events,
and it is the reproduction. The rest of this file is the two walls now standing
in it.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from dictate.cleanup import rules as cleanup_rules_mod
from dictate.cleanup.engine import clean
from dictate.engines.whisper_server import WhisperServerClient
from dictate.platform import line_breaks, modifier_guard
from dictate.platform.injection_plan import VK_RETURN, VK_TAB, plan_text
from dictate.punctuation import rules as punctuation_rules_mod
from dictate.punctuation.engine import apply as punctuate

REPO = Path(__file__).resolve().parent.parent

#: What whisper-server actually replies with. Two segments, one line each, and
#: the trailing newline `output_str` writes after the last one as well.
TWO_SEGMENTS = '{"text": " Sentence one.\\n Sentence two.\\n"}'


def keys_pressed(events) -> list[int]:
    """The virtual keys a plan would press. Characters are not keys."""
    return [e.code for e in events if e.kind == "vk" and not e.up]


class WhereItCameFrom(unittest.TestCase):
    """The reproduction, in the pure halves, from the reply to the keystrokes."""

    def test_the_server_sends_one_line_per_segment(self):
        """Not a fixture anybody invented: this is the shape `output_str`
        produces, and the reason the rest of this file exists."""
        self.assertIn("\\n", TWO_SEGMENTS)

    def test_the_old_parse_would_have_left_the_newline_in_the_text(self):
        """What `_parse` used to do, spelled out, so the fix below has
        something to be a change from."""
        old = ' Sentence one.\n Sentence two.\n'.strip()
        self.assertIn("\n", old)
        self.assertEqual(keys_pressed(plan_text(old, allow_return=True)),
                         [VK_RETURN])

    def test_the_cleanup_pass_would_not_have_removed_it(self):
        """It collapses runs of spaces and tabs and leaves line breaks alone,
        deliberately - so it was never going to catch this."""
        rules = cleanup_rules_mod.load(REPO / "config" / "cleanup-rules.toml")
        self.assertIn("\n", clean("Sentence one.\nSentence two.", rules).text)

    def test_and_now_the_whole_chain_presses_nothing(self):
        text = WhisperServerClient._parse(TWO_SEGMENTS)
        rules = cleanup_rules_mod.load(REPO / "config" / "cleanup-rules.toml")
        cleaned = clean(text, rules).text
        self.assertEqual(keys_pressed(plan_text(cleaned)), [])
        self.assertEqual(cleaned, "Sentence one. Sentence two.")


class TheServersOwnDelimiter(unittest.TestCase):
    """`_parse`: the fix at the place the newline was actually coming from."""

    def test_segments_are_joined_with_a_space(self):
        self.assertEqual(WhisperServerClient._parse(TWO_SEGMENTS),
                         "Sentence one. Sentence two.")

    def test_one_segment_is_unchanged_apart_from_its_spacing(self):
        self.assertEqual(WhisperServerClient._parse('{"text": " Hello there.\\n"}'),
                         "Hello there.")

    def test_the_words_are_exactly_the_words_whisper_produced(self):
        """This is a delimiter being read, not the transcript being edited: no
        word, no capital and no mark of Whisper's own is touched."""
        raw = '{"text": " Do it, now.\\n Really?\\n No.\\n"}'
        self.assertEqual(WhisperServerClient._parse(raw), "Do it, now. Really? No.")

    def test_an_empty_reply_is_still_empty(self):
        self.assertEqual(WhisperServerClient._parse('{"text": "\\n"}'), "")

    def test_the_verbose_json_branch_already_did_this_and_still_does(self):
        """The branch that proves the intent: where the segments arrive
        separately, this module always joined them with a space."""
        raw = '{"segments": [{"text": " One."}, {"text": " Two."}]}'
        self.assertEqual(WhisperServerClient._parse(raw), "One. Two.")


class NothingDictatedCanPressAKey(unittest.TestCase):
    """The wall in the injector: whatever the text contains, it is typed.

    The parse fix above closes the way the newlines were actually getting in.
    This closes every other one - a rule he writes, a mark he turns on, a model
    that starts writing line breaks of its own - without any of them having to
    know about it.
    """

    def test_a_line_break_is_a_space_by_default(self):
        self.assertEqual(keys_pressed(plan_text("run this\nand this")), [])

    def test_including_the_ones_a_spoken_mark_inserts(self):
        """"new line" is a shipped mark, and its own comment says Return
        submits. With spoken punctuation on and line_breaks left alone, it
        still cannot."""
        rules = punctuation_rules_mod.load(REPO / "config" / "voice-punctuation.toml")
        text = punctuate("Hello new line world.", rules).text
        self.assertIn("\n", text)                       # the mark did its job
        self.assertEqual(keys_pressed(plan_text(text)), [])

    def test_a_tab_cannot_be_pressed_either(self):
        """Tab does not submit, but in a shell it completes and in a dialog it
        moves focus - so the rest of the sentence is typed somewhere else."""
        self.assertEqual(keys_pressed(plan_text("a\tb")), [])

    def test_it_is_the_plan_that_refuses_not_the_caller(self):
        """The guarantee is a property of `plan_text`, so it holds for a caller
        that has not heard of `line_breaks` at all."""
        for text in ("\n", "\r\n", "\r", "\t", "\v", "\f",
                     "\N{LINE SEPARATOR}", "\N{PARAGRAPH SEPARATOR}"):
            with self.subTest(text=text):
                self.assertEqual(keys_pressed(plan_text(text)), [])

    def test_and_when_he_asks_for_them_he_gets_them(self):
        events = plan_text("a\nb", allow_return=True)
        self.assertEqual(keys_pressed(events), [VK_RETURN])
        self.assertEqual(keys_pressed(plan_text("a\tb", allow_return=True)),
                         [VK_TAB])


class TheLineBreakPolicy(unittest.TestCase):
    """`line_breaks`: what "space" and "return" mean, exactly."""

    def test_space_flattens_and_return_does_not(self):
        self.assertEqual(line_breaks.apply("a\nb", line_breaks.SPACE), "a b")
        self.assertEqual(line_breaks.apply("a\nb", line_breaks.RETURN), "a\nb")

    def test_a_flattened_break_leaves_one_space_not_a_gap(self):
        self.assertEqual(line_breaks.flatten("one.\n\n  two."), "one. two.")
        self.assertEqual(line_breaks.flatten("one. \n two."), "one. two.")

    def test_the_words_survive_the_flattening(self):
        """It is a space, not a deletion: the sentence is still both halves."""
        self.assertEqual(line_breaks.flatten("Sentence one.\nSentence two."),
                         "Sentence one. Sentence two.")

    def test_text_with_no_break_is_returned_as_it_was(self):
        self.assertEqual(line_breaks.flatten("Nothing to do here."),
                         "Nothing to do here.")

    def test_counting_what_would_be_pressed(self):
        self.assertEqual(line_breaks.returns_in("a\nb\nc"), 2)
        self.assertEqual(line_breaks.returns_in("a\r\nb"), 1)   # CRLF is one
        self.assertEqual(line_breaks.returns_in("a\tb"), 0)     # tab is not one
        self.assertEqual(line_breaks.returns_in("nothing here"), 0)

    def test_the_two_modes_are_the_two_the_config_offers(self):
        from dictate import config as config_mod

        self.assertEqual(set(line_breaks.MODES), {"space", "return"})
        self.assertEqual(config_mod.PasteConfig().line_breaks, line_breaks.SPACE)


class HisOwnModifiersAreNotPartOfThePaste(unittest.TestCase):
    """`modifier_guard`: the same defect wearing a different hat.

    Letting go of Ctrl+Alt+Space ends the recording as soon as the space bar
    comes up, and the paste follows half a second later. Ctrl still down at that
    moment turns "the" into Ctrl+T, Ctrl+H, Ctrl+E.
    """

    def setUp(self):
        self.now = 0.0
        self.forced: list[list[str]] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 0.001)

    def settle(self, down, **kwargs):
        return modifier_guard.settle(
            lambda name: name in down(), self.forced.append,
            clock=self.clock, sleep=self.sleep, **kwargs)

    def test_nothing_held_costs_one_look_and_no_wait(self):
        held, forced = self.settle(lambda: set(), wait_s=0.4)
        self.assertEqual((held, forced), ([], []))
        self.assertEqual(self.now, 0.0)
        self.assertEqual(self.forced, [])

    def test_a_modifier_he_is_still_letting_go_of_is_waited_for(self):
        state = {modifier_guard.CONTROL}

        def down():
            if self.now > 0.1:
                state.clear()
            return state

        held, forced = self.settle(down, wait_s=0.4)
        self.assertEqual(held, ["control"])
        self.assertEqual(forced, [])
        self.assertEqual(self.forced, [])       # waiting was enough
        self.assertLess(self.now, 0.4)          # and it did not wait it all out

    def test_one_he_never_lets_go_of_is_forced_up_before_anything_is_typed(self):
        held, forced = self.settle(lambda: {modifier_guard.CONTROL, modifier_guard.ALT}, wait_s=0.4)
        self.assertEqual(held, ["control", "alt"])
        self.assertEqual(forced, ["control", "alt"])
        self.assertEqual(self.forced, [["control", "alt"]])
        self.assertGreaterEqual(self.now, 0.4)

    def test_it_will_not_force_a_key_up_while_he_is_speaking_again(self):
        """The trap this guard could walk into. A synthesised key-up goes
        through the same low-level hook the hotkey listens on, so forcing Ctrl
        up while he is holding the chord for his NEXT utterance would end that
        recording - the guard causing the fault it exists to prevent."""
        held, forced = self.settle(
            lambda: {modifier_guard.CONTROL, modifier_guard.ALT},
            wait_s=0.4, recording=lambda: True)
        self.assertEqual(held, ["control", "alt"])
        self.assertEqual(forced, [])
        self.assertEqual(self.forced, [])

    def test_shift_is_not_guarded(self):
        """Shift+E is E. Forcing it up would be interfering with a key he may
        be holding for a reason of his own."""
        self.assertNotIn("shift", modifier_guard.GUARDED)
        held, forced = self.settle(lambda: {"shift"}, wait_s=0.4)
        self.assertEqual((held, forced), ([], []))

    def test_zero_turns_the_guard_off_entirely(self):
        """`[paste] modifier_wait_ms = 0`: not even a look at the keyboard,
        which is exactly how dictate behaved before this existed."""
        looked = []

        def down():
            looked.append(1)
            return {modifier_guard.CONTROL}

        held, forced = self.settle(down, wait_s=0.0)
        self.assertEqual((held, forced, looked), ([], [], []))

    def test_it_only_ever_sends_key_ups(self):
        """It is a guard in front of a paste. Pressing anything would make it
        the thing it exists to prevent."""
        source = (Path(modifier_guard.__file__)).read_text(encoding="utf-8")
        self.assertNotIn("force_down", source)
        self.assertIn("force_up", source)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
