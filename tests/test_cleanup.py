"""The cleanup pass, including the guarantee that it cannot invent words."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dictate.cleanup import rules as rules_mod
from dictate.cleanup.engine import CleanResult, clean, words, words_are_subsequence
from dictate.cleanup.service import CleanupService
from dictate.errors import ConfigError

SHIPPED_RULES = Path(__file__).resolve().parent.parent / "config" / "cleanup-rules.toml"


def load_shipped() -> rules_mod.CleanupRules:
    return rules_mod.load(SHIPPED_RULES)


class SubsequenceGuarantee(unittest.TestCase):
    def test_detects_added_word(self):
        self.assertFalse(words_are_subsequence(["a", "new", "b"], ["a", "b"]))

    def test_detects_reordering(self):
        self.assertFalse(words_are_subsequence(["b", "a"], ["a", "b"]))

    def test_allows_deletion(self):
        self.assertTrue(words_are_subsequence(["a", "c"], ["a", "b", "c"]))

    def test_allows_identity(self):
        self.assertTrue(words_are_subsequence(["a", "b"], ["a", "b"]))

    def test_words_are_case_and_punctuation_insensitive(self):
        self.assertEqual(words("Hello, World's end!"), ["hello", "world's", "end"])

    def test_a_rule_that_invents_words_is_rejected_wholesale(self):
        # A user could write a pattern whose *deletion* still changes a word,
        # e.g. cutting the middle out of one. The engine must notice and paste
        # Whisper's text untouched rather than the mangled version.
        bad = rules_mod.from_mapping(
            {"deletions": [{"name": "eats letters", "pattern": "ell"}],
             "collapse_doubled_words": False,
             "collapse_repeated_phrases": False},
            Path("test"),
        )
        result = clean("Hello there.", bad)
        self.assertEqual(result.text, "Hello there.")
        self.assertIsNotNone(result.rejected_reason)

    def test_every_shipped_rule_preserves_the_guarantee_on_realistic_speech(self):
        rules = load_shipped()
        samples = [
            "Um, so I was thinking, uh, we should probably ship it on Friday.",
            "I want to - I want to make sure the tests actually run.",
            "The the problem is that, like, nobody has the hardware.",
            "You know, it's kind of a hard problem, if that makes sense.",
            "I-I-I think th- th- the answer is no.",
            "Send it to my e-mail and I will re-read it, right?",
            "He said that that was fine, and I had had enough.",
            "Turn right at the lights, then park like this.",
            "So we went home. So, anyway, that was that.",
        ]
        for text in samples:
            with self.subTest(text=text):
                result = clean(text, rules)
                self.assertIsNone(result.rejected_reason, result.rejected_reason)
                self.assertTrue(
                    words_are_subsequence(words(result.text), words(text)),
                    f"{text!r} -> {result.text!r}",
                )


class ShippedRules(unittest.TestCase):
    def setUp(self):
        self.rules = load_shipped()

    def clean(self, text: str) -> str:
        return clean(text, self.rules).text

    def test_strips_leading_filler_and_restores_the_capital(self):
        self.assertEqual(self.clean("Um, so we should go."), "So we should go.")

    def test_strips_mid_sentence_filler(self):
        self.assertEqual(
            self.clean("I think, uh, we should go."), "I think, we should go.")

    def test_strips_filler_phrase(self):
        # Both commas go with it, as for "comma-wrapped like". Until the guarded
        # rules replaced the unconditional phrase list this left "It is, quite
        # hard." - the phrase went and its fence did not.
        self.assertEqual(
            self.clean("It is, you know, quite hard."), "It is quite hard.")

    def test_collapses_doubled_word(self):
        self.assertEqual(self.clean("The the cat sat."), "The cat sat.")

    def test_keeps_legitimate_doubling(self):
        self.assertEqual(self.clean("He said that that was fine."),
                         "He said that that was fine.")
        self.assertEqual(self.clean("I had had enough."), "I had had enough.")

    def test_collapses_a_verbatim_restart(self):
        self.assertEqual(self.clean("I want to - I want to go home."), "I want to go home.")

    def test_keeps_a_repeat_across_a_full_stop(self):
        # Two sentences that happen to start the same way are not a restart.
        text = "We should go. We should go now."
        self.assertEqual(self.clean(text), text)

    def test_comma_wrapped_like_is_filler(self):
        self.assertEqual(self.clean("It was, like, enormous."), "It was enormous.")

    def test_bare_like_is_a_real_word_and_survives(self):
        self.assertEqual(self.clean("I like it and it works like this."),
                         "I like it and it works like this.")

    def test_turn_right_is_a_real_word_and_survives(self):
        self.assertEqual(self.clean("Turn right at the lights."),
                         "Turn right at the lights.")

    def test_the_tag_question_rule_is_off_by_default(self):
        # Deleting ", right?" would take the sentence's only punctuation with
        # it, and this pass may not put a full stop back. Leaving the tic in is
        # the smaller wart; the rule is in cleanup-rules.toml, commented out.
        self.assertEqual(self.clean("We should go, right?"), "We should go, right?")

    def test_the_opt_in_tag_question_rule_works_if_you_enable_it(self):
        rules = rules_mod.from_mapping(
            {"deletions": [{"name": "trailing right", "pattern": r",[ \t]*right\?"}]},
            Path("test"),
        )
        self.assertEqual(clean("We should go, right?", rules).text, "We should go")
        self.assertEqual(clean("Turn right at the lights.", rules).text,
                         "Turn right at the lights.")

    def test_stutter_fragments_go_but_hyphenated_words_stay(self):
        self.assertEqual(self.clean("I-I-I think so."), "I think so.")
        self.assertEqual(self.clean("Send it to my e-mail."), "Send it to my e-mail.")
        self.assertEqual(self.clean("I will re-read it."), "I will re-read it.")

    def test_does_not_add_punctuation(self):
        self.assertEqual(self.clean("no punctuation here at all"),
                         "no punctuation here at all")

    def test_empty_and_whitespace(self):
        self.assertEqual(self.clean(""), "")
        self.assertEqual(self.clean("   \n  "), "")

    def test_leaves_ordinary_text_completely_alone(self):
        text = ("The quick brown fox jumps over the lazy dog. It was the best of "
                "times, it was the worst of times.")
        self.assertEqual(self.clean(text), text)


class PhrasesThatAreAlsoRealSpeech(unittest.TestCase):
    """The regression that made these rules guarded.

    "you know", "I mean", "sort of", "kind of", "like I said" and "if that makes
    sense" were once in `filler_phrases`, which deletes a phrase wherever it
    appears with no regard for what is around it. They are all ordinary English
    somewhere, so ordinary sentences lost their middle - silently, because a
    deletion is exactly what the pass is permitted to do and the subsequence
    guarantee therefore cannot fire on a fault of this shape.

    These are the sentences that were measured coming out wrong, and the filler
    forms that must still be removed. A rule that goes back to deleting one of
    these phrases unconditionally fails the first half of this class.
    """

    def setUp(self):
        self.rules = load_shipped()

    def clean(self, text: str) -> str:
        return clean(text, self.rules).text

    #: Left exactly as Whisper wrote them. The first four are the measured
    #: report cases; what dictate used to paste is in the comment.
    REAL_SPEECH = [
        "Do you know the answer?",                 # was "Do the answer?"
        "That is the kind of thing I mean.",       # was "That is the thing."
        "It is a sort of hybrid.",                 # was "It is a hybrid."
        "What I mean is different.",               # was "What is different."
        "You know what I mean.",                   # was "What."
        "Do you know if it works like I said?",    # was "Do if it works?"
        "If that makes sense to you, ship it.",    # was "To you, ship it."
        "Do it like I said.",                      # was "Do it."
        "I sort of remember it.",
        "What kind of file is it?",
        "I know what you mean by that.",
        "Tell me what you know.",
        "It depends on the kind of hardware he has.",
    ]

    #: Genuine filler, still removed - and now without the stray comma the
    #: unconditional deletion used to leave behind.
    FILLER = [
        ("It is, you know, mostly fine.", "It is mostly fine."),
        ("You know, it is mostly fine.", "It is mostly fine."),
        ("It is fine. You know, really fine.", "It is fine. Really fine."),
        ("It is mostly fine, you know.", "It is mostly fine."),
        ("It is, I mean, mostly fine.", "It is mostly fine."),
        ("I mean, it is mostly fine.", "It is mostly fine."),
        ("It was, sort of, enormous.", "It was enormous."),
        ("It was, kind of, enormous.", "It was enormous."),
        ("We should ship it, like I said, on Friday.", "We should ship it on Friday."),
        ("We should ship it, like I said.", "We should ship it."),
        ("We should ship it, if that makes sense.", "We should ship it."),
        ("The kind of thing, you know, that breaks.", "The kind of thing that breaks."),
    ]

    def test_real_speech_survives_untouched(self):
        for text in self.REAL_SPEECH:
            with self.subTest(text=text):
                self.assertEqual(self.clean(text), text)

    def test_the_filler_forms_are_still_removed(self):
        for text, expected in self.FILLER:
            with self.subTest(text=text):
                self.assertEqual(self.clean(text), expected)

    def test_the_shipped_phrase_list_is_empty(self):
        """`filler_phrases` has no guard of any kind - it deletes the phrase
        anywhere it appears. Every phrase that was ever in it turned out to be
        real speech somewhere, and all six now live in [[deletions]] with the
        comma fencing Whisper writes when it hears one as filler.

        If you are here because you added a phrase and this failed: it is only
        safe if you cannot think of one sentence where you would mean it
        literally. If you can, write a guarded [[deletions]] rule instead, and
        add the sentence to REAL_SPEECH above.
        """
        self.assertEqual(load_shipped().filler_phrases, [])

    def test_the_guarantee_holds_over_all_of_it(self):
        for text in self.REAL_SPEECH + [t for t, _ in self.FILLER]:
            with self.subTest(text=text):
                result = clean(text, self.rules)
                self.assertIsNone(result.rejected_reason, result.rejected_reason)
                self.assertTrue(
                    words_are_subsequence(words(result.text), words(text)),
                    f"{text!r} -> {result.text!r}",
                )


class NothingButPunctuationIsPastedAsNothing(unittest.TestCase):
    """A cleanup that deleted every word must not paste the leftover marks.

    "Um." used to come out as ".", and "That is all I had. Um." as "..". The
    pipeline already has a "did not hear any words in that" path; an empty
    result is what sends it there.
    """

    def setUp(self):
        self.rules = load_shipped()

    def clean(self, text: str) -> str:
        return clean(text, self.rules).text

    def test_a_filler_sentence_leaves_nothing(self):
        self.assertEqual(self.clean("Um."), "")
        self.assertEqual(self.clean("Um, um, um."), "")
        self.assertEqual(self.clean("Hmm."), "")

    def test_a_trailing_filler_sentence_does_not_leave_a_second_full_stop(self):
        self.assertEqual(self.clean("That is all I had. Um."), "That is all I had.")

    def test_the_kept_mark_is_the_one_that_was_already_there(self):
        # Dropping the FIRST mark would turn this into "Really." - swapping a
        # question mark for a full stop is re-punctuating, which this pass does
        # not do.
        self.assertEqual(self.clean("Really? Um."), "Really?")

    def test_a_leading_filler_sentence_does_not_leave_a_full_stop_in_front(self):
        self.assertEqual(self.clean("Um. That is all."), "That is all.")

    def test_a_real_ellipsis_is_left_alone(self):
        self.assertEqual(self.clean("Wait... I mean it."), "Wait... I mean it.")

    def test_the_check_fires_even_when_punctuation_repair_cannot_help(self):
        # The repair only tidies marks it recognises; the quotes here defeat it,
        # so this is the engine's own last look at the result.
        result = clean('"Um."', self.rules)
        self.assertEqual(result.text, "")
        self.assertIn("nothing but punctuation left", result.applied)

    def test_ordinary_text_is_not_emptied(self):
        self.assertEqual(self.clean("Ship it."), "Ship it.")


class RulesFileValidation(unittest.TestCase):
    def test_unknown_key_is_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            rules_mod.from_mapping({"fillerz": ["um"]}, Path("x.toml"))
        self.assertIn("fillerz", str(ctx.exception))

    def test_a_replace_field_is_refused(self):
        # The schema has no replacement field on purpose; make sure a user who
        # adds one is told why rather than having it silently ignored.
        with self.assertRaises(ConfigError) as ctx:
            rules_mod.from_mapping(
                {"deletions": [{"name": "n", "pattern": "x", "replace": "y"}]},
                Path("x.toml"),
            )
        self.assertIn("only delete", ctx.exception.remedy)

    def test_bad_regex_names_the_rule(self):
        with self.assertRaises(ConfigError) as ctx:
            rules_mod.from_mapping(
                {"deletions": [{"name": "oops", "pattern": "([unclosed"}]}, Path("x.toml"))
        self.assertIn("oops", str(ctx.exception))

    def test_a_byte_order_mark_does_not_look_like_a_broken_rules_file(self):
        """This file is meant to be edited, and Notepad saves UTF-8 with a byte
        order mark that would otherwise read as a syntax error on line 1."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cleanup-rules.toml"
            path.write_bytes(b'\xef\xbb\xbffillers = ["um", "uh"]\n')
            loaded = rules_mod.load(path)
            self.assertIn("um", loaded.fillers)

    def test_missing_file_explains_the_options(self):
        with self.assertRaises(ConfigError) as ctx:
            rules_mod.load(Path("/nonexistent/cleanup-rules.toml"))
        self.assertIn("enabled = false", ctx.exception.remedy)


class Service(unittest.TestCase):
    def test_disabled_service_passes_text_through(self):
        service = CleanupService(None, enabled=False)
        result = service("Um, hello.")
        self.assertIsInstance(result, CleanResult)
        self.assertEqual(result.text, "Um, hello.")

    def test_reloads_when_the_file_changes(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.toml"
            path.write_text('fillers = ["um"]\n', encoding="utf-8")
            service = CleanupService(path)
            service.load()
            self.assertEqual(service("Um, hi banana.").text, "Hi banana.")

            path.write_text('fillers = ["um", "banana"]\n', encoding="utf-8")
            import os
            os.utime(path, (0, 0))  # force a different mtime
            self.assertEqual(service("Um, hi banana.").text, "Hi.")

    def test_a_broken_edit_keeps_the_previous_rules_and_reports(self):
        import os
        import tempfile

        messages = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.toml"
            path.write_text('fillers = ["um"]\n', encoding="utf-8")
            service = CleanupService(path, notify=lambda lvl, msg: messages.append((lvl, msg)))
            service.load()

            path.write_text("this is not toml [[[", encoding="utf-8")
            os.utime(path, (0, 0))
            self.assertEqual(service("Um, still working.").text, "Still working.")
            self.assertTrue(any(lvl == "error" for lvl, _ in messages))


if __name__ == "__main__":
    unittest.main()
