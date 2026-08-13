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
        self.assertEqual(
            self.clean("It is, you know, quite hard."), "It is, quite hard.")

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
