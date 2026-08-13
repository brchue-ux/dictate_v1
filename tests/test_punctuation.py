"""Spoken punctuation - the stage, its rules file, and the guarantee it carries.

Two things in here are worth reading before changing anything:

* `WhisperReallyWritesThis` holds transcripts that came out of Whisper
  large-v3-turbo, not sentences anyone made up. Everything the mark/word rule
  does is built on them. The PR that added this stage records how they were
  produced.
* `WhereTheRuleIsWrong` asserts the CASES IT GETS WRONG. They are there so
  nobody has to find them one at a time, and so that fixing one is a visible
  change to this file rather than a silent change of behaviour.
"""

from __future__ import annotations

import logging
import time
import unittest
from pathlib import Path

from dictate import config as config_mod
from dictate.cleanup import rules as cleanup_rules_mod
from dictate.cleanup.engine import clean, words, words_are_subsequence
from dictate.errors import ConfigError
from dictate.punctuation import rules as rules_mod
from dictate.punctuation.engine import apply
from dictate.punctuation.service import PunctuationService

REPO = Path(__file__).resolve().parent.parent
SHIPPED = REPO / "config" / "voice-punctuation.toml"
SHIPPED_CLEANUP = REPO / "config" / "cleanup-rules.toml"


def shipped() -> rules_mod.PunctuationRules:
    return rules_mod.load(SHIPPED)


def punctuate(text: str) -> str:
    return apply(text, shipped()).text


def dictated(text: str) -> str:
    """What dictate actually pastes: cleanup first, then spoken punctuation."""
    cleaned = clean(text, cleanup_rules_mod.load(SHIPPED_CLEANUP)).text
    return apply(cleaned, shipped()).text


def rules_from(body: str, tmp: Path) -> rules_mod.PunctuationRules:
    path = tmp / "marks.toml"
    path.write_text(body, encoding="utf-8")
    return rules_mod.load(path)


class TheGuarantee(unittest.TestCase):
    """This stage substitutes, so it needs a guarantee of its own. It is: only
    punctuation ever goes in, so no word can ever be invented."""

    def test_a_rule_that_would_insert_a_word_is_refused_at_load(self):
        with self.assertRaises(ConfigError) as ctx:
            rules_mod.from_mapping(
                {"marks": [{"name": "bad", "say": ["comma"], "insert": " and "}]},
                Path("x.toml"))
        self.assertIn("letters or digits", ctx.exception.message)

    def test_a_rule_that_would_insert_a_digit_is_refused_at_load(self):
        with self.assertRaises(ConfigError):
            rules_mod.from_mapping(
                {"marks": [{"name": "bad", "say": ["comma"], "insert": "2"}]},
                Path("x.toml"))

    def test_punctuation_and_whitespace_are_allowed(self):
        for insert in (",", ".", "\n", "\n\n", '"', "(", " - ", "…", "—", "•"):
            with self.subTest(insert=insert):
                rules_mod.from_mapping(
                    {"marks": [{"name": "ok", "say": ["comma"], "insert": insert}]},
                    Path("x.toml"))

    def test_the_output_words_are_always_a_subsequence_of_the_input_words(self):
        speech = [
            "Hello, comma, world.",
            "That is all period, the meeting is over.",
            "We need three things, colon, milk, eggs and bread.",
            "He said open, quote, hello there close, quote.",
            "First item new line second item.",
            "The comma goes here.",
            "I need to add periods and commas.",
        ]
        for line in speech:
            with self.subTest(line=line):
                out = punctuate(line)
                self.assertTrue(words_are_subsequence(words(out), words(line)),
                                f"{out!r} is not a subsequence of {line!r}")

    def test_a_rule_that_breaks_it_throws_the_whole_stage_away(self):
        """The schema check cannot be the only defence, so this exercises the
        second one directly: a Mark built past the loader still cannot get a
        word into the output."""
        rules = rules_mod.PunctuationRules(marks=[
            rules_mod.Mark(name="sneaky", say=(("comma",),), insert="and")
        ])
        result = apply("Hello comma world.", rules)
        self.assertEqual(result.text, "Hello comma world.")
        self.assertIn("added or changed words", result.rejected_reason)


class TheCleanupGuaranteeIsExactlyWhereItWas(unittest.TestCase):
    """The whole reason this is a separate stage. If any of these fail, spoken
    punctuation has been allowed to weaken the pass next door."""

    def test_a_deletion_rule_still_has_no_replacement_field(self):
        import dataclasses
        names = {f.name for f in dataclasses.fields(cleanup_rules_mod.Deletion)}
        self.assertEqual(names, {"name", "pattern", "ignorecase", "comment", "regex"})

    def test_a_replace_field_is_still_refused(self):
        with self.assertRaises(ConfigError) as ctx:
            cleanup_rules_mod.from_mapping(
                {"deletions": [{"name": "x", "pattern": "a", "replace": "b"}]},
                Path("x.toml"))
        self.assertIn("rules can only delete", ctx.exception.remedy)

    def test_cleanup_still_discards_a_rule_that_invents_words(self):
        rules = cleanup_rules_mod.from_mapping(
            {"deletions": [{"name": "eats a word", "pattern": "world"}]},
            Path("x.toml"))
        # A deletion is fine; what is checked is that the check itself is live.
        self.assertEqual(clean("hello world", rules).text, "hello")
        self.assertTrue(words_are_subsequence(["hello"], ["hello", "world"]))
        self.assertFalse(words_are_subsequence(["hello", "there"], ["hello", "world"]))

    def test_the_punctuation_stage_only_ever_reads_from_the_cleanup_pass(self):
        """It imports the subsequence check rather than copying it, and that is
        the ONLY thing it takes. Nothing in here may reach in and change how
        cleanup behaves."""
        source = "\n".join(p.read_text(encoding="utf-8")
                           for p in (REPO / "src" / "dictate" / "punctuation").rglob("*.py"))
        imports = [line.strip() for line in source.splitlines()
                   if "cleanup" in line and line.strip().startswith("from")]
        self.assertEqual(imports,
                         ["from ..cleanup.engine import words, words_are_subsequence"])

    def test_the_pipeline_hands_cleanup_whispers_text_byte_for_byte(self):
        """Cleanup runs FIRST and is handed exactly what it was handed before
        this feature existed, so its subsequence check is evaluated over the
        same text it always was. This is the "same position" half of "same
        schema, same check, same position"."""
        from dictate.pipeline import Pipeline
        from tests import fakes

        whisper_said = "Um, hello, comma, world."
        seen = []

        def recording_cleaner(text):
            seen.append(text)
            return clean(text, cleanup_rules_mod.load(SHIPPED_CLEANUP))

        service = PunctuationService(SHIPPED, enabled=True)
        service.load()
        pipeline = Pipeline(
            batch=fakes.FakeBatch(whisper_said),
            cleaner=recording_cleaner,
            injector=fakes.FakeInjector(),
            windows=fakes.FakeWindows(),
            overlay=fakes.FakeOverlay(),
            punctuator=service,
            submit=fakes.InlineSubmit(),
        )
        pipeline.start_utterance()
        pipeline.push_audio(b"\0" * 32000)
        pipeline.finish_utterance()

        self.assertEqual(seen, [whisper_said])
        self.assertEqual([text for text, _ in pipeline.injector.sent], ["Hello, world."])


class WhisperReallyWritesThis(unittest.TestCase):
    """Transcripts MEASURED from Whisper large-v3-turbo, spoken by a TTS voice.

    Every one of these is a real string that came back from the model, not an
    invented example - which matters, because the shape it comes back in is the
    whole design. Whisper both punctuates the dictated mark AND spells it out:
    "hello comma world" arrives as "Hello, comma, world." and "are you sure
    question mark" as "Are you sure? Question mark." Absorbing what Whisper
    wrote is what stops the result being "Hello,, world."
    """

    def test_a_comma_arrives_fenced_by_commas(self):
        self.assertEqual(dictated("Hello, comma, world."), "Hello, world.")

    def test_a_full_stop_arrives_with_whispers_own_full_stop_after_it(self):
        self.assertEqual(dictated("That is all period."), "That is all.")
        self.assertEqual(dictated("That is all full stop."), "That is all.")

    def test_a_full_stop_mid_utterance_arrives_with_a_comma_after_it(self):
        self.assertEqual(dictated("That is all period, the meeting is over."),
                         "That is all. The meeting is over.")

    def test_whisper_had_already_written_the_question_mark(self):
        self.assertEqual(dictated("Are you sure? Question mark."), "Are you sure?")

    def test_whisper_sometimes_does_the_substitution_itself(self):
        """MEASURED: "hello comma how are you question mark" came back with the
        first comma already a comma and the word gone. There is nothing left to
        match, and the right answer falls out anyway."""
        self.assertEqual(dictated("Hello, how are you? Question mark."),
                         "Hello, how are you?")

    def test_a_colon_introducing_a_list(self):
        self.assertEqual(dictated("We need three things, colon, milk, eggs and bread."),
                         "We need three things: milk, eggs and bread.")

    def test_a_semicolon(self):
        self.assertEqual(dictated("It was late, semicolon, we went home."),
                         "It was late; we went home.")

    def test_whisper_writes_semi_colon_with_a_hyphen_in_it(self):
        """MEASURED. A plain string search for "semicolon" finds nothing here,
        which is why phrases are matched word by word."""
        self.assertEqual(dictated("It was late. Semi-colon. We went home."),
                         "It was late; We went home.")

    def test_whisper_splits_open_quote_with_a_comma_between_the_two_words(self):
        """MEASURED: "he said open quote hello there close quote" came back as
        "He said open, quote, hello there close, quote." - the fence lands
        INSIDE the phrase, and word-by-word matching is what survives it."""
        self.assertEqual(dictated("He said open, quote, hello there close, quote."),
                         'He said "hello there".')

    def test_brackets(self):
        self.assertEqual(dictated("The result open bracket see below close bracket "
                                  "was good."),
                         "The result (see below) was good.")

    def test_a_dash(self):
        self.assertEqual(dictated("We should go dash if we can."),
                         "We should go - if we can.")

    def test_a_new_line_with_no_punctuation_around_it_at_all(self):
        self.assertEqual(dictated("First item new line second item."),
                         "First item\nsecond item.")

    def test_a_full_stop_and_a_new_paragraph_back_to_back(self):
        self.assertEqual(
            dictated("That is all period, new paragraph, the next thing is different."),
            "That is all.\n\nThe next thing is different.")

    def test_the_paused_delivery_where_whisper_makes_the_mark_its_own_sentence(self):
        """MEASURED: pausing either side of the mark makes Whisper write it as a
        sentence of its own - "That is all. Period. The meeting is over." The
        full stop in front of it is absorbed the same way a fence comma is."""
        self.assertEqual(dictated("That is all. Period. The meeting is over."),
                         "That is all. The meeting is over.")
        self.assertEqual(dictated("First item. New line. Second item."),
                         "First item\nSecond item.")


class PhrasesThatAreAlsoRealSpeech(unittest.TestCase):
    """The whole job. Every one of these is a transcript MEASURED from Whisper
    for a sentence where the mark word was meant literally, and every one of
    them has to come through untouched."""

    REAL = [
        "The comma goes here.",
        "Set the period to five minutes.",
        "What is the question mark for?",
        "I had a comma after the word.",
        "put it on a new line.",
        "Start a new paragraph here.",
        "The colon is a punctuation mark.",
        "He used a dash instead.",
        "Delete that semicolon.",
        "She said the exclamation mark was too much.",
        "During that period we grew.",
        "Commas are hard.",
        "I need to add periods and commas.",
        "The word comma is spelled like this.",
        "Add another comma at the end.",
        "My comma was in the wrong place.",
        "This dash is too long.",
        "Those brackets are unbalanced.",
    ]

    def test_real_speech_survives_untouched(self):
        for line in self.REAL:
            with self.subTest(line=line):
                self.assertEqual(dictated(line), line)

    def test_a_plural_is_never_a_mark_however_it_is_written(self):
        for line in ("Commas are hard.", "Two periods in a row.",
                     "Question marks everywhere.", "Count the colons."):
            with self.subTest(line=line):
                self.assertEqual(punctuate(line), line)

    def test_the_guard_does_not_reach_across_a_sentence_ending(self):
        """"...was the. Comma world" is not "the comma": the sentence ended in
        between. The guard only guards the phrase directly after it."""
        self.assertEqual(punctuate("That was the. Comma world."), "That was the, world.")

    def test_the_shipped_guard_list_is_determiners_and_possessives(self):
        """A guard word costs a mark he might have wanted, so the list stays
        short and predictable. Numbers were tried and taken out: "one comma two
        comma three" is a thing you say."""
        self.assertEqual(
            shipped().guard_words,
            ["a", "an", "the", "word", "this", "that", "these", "those", "my",
             "your", "his", "her", "its", "our", "their", "another"])
        self.assertEqual(punctuate("one comma two comma three"),
                         "one, two, three")


class WhereTheRuleIsWrong(unittest.TestCase):
    """The cases the mark/word rule gets WRONG, asserted on purpose.

    A rule that decides this from one preceding word cannot be right every
    time, and pretending otherwise would be worse than saying where it breaks.
    Each of these has the escape as its answer - see `TheEscape`. If a change
    fixes one of them, this test fails, and that is the point: the list in the
    PR and in the rules file has to be updated with it."""

    def test_the_british_emphatic_full_stop_is_taken_as_a_mark(self):
        """"I am not going, full stop" means "and that is final". Nothing in
        the text distinguishes it from a dictated full stop."""
        self.assertEqual(dictated("I am not going full stop."), "I am not going.")

    def test_a_bare_noun_use_with_no_determiner_is_taken_as_a_mark(self):
        self.assertEqual(punctuate("Add comma here."), "Add, here.")

    def test_a_determiner_in_front_of_a_mark_he_did_want_suppresses_it(self):
        """The mirror image: he wanted the mark, and the word before it happened
        to be a determiner."""
        self.assertEqual(punctuate("It cost me a comma then I fixed it"),
                         "It cost me a comma then I fixed it")

    def test_a_capital_left_behind_by_an_absorbed_sentence_break_stays(self):
        """When Whisper made the spoken mark its own sentence, the next word
        already has a capital. Lower-casing it would re-case the transcript,
        which no stage in dictate does - Whisper's capitals are the only signal
        that a word is a name."""
        self.assertEqual(dictated("It was late. Semi-colon. We went home."),
                         "It was late; We went home.")


class Spacing(unittest.TestCase):
    def test_the_mark_goes_against_the_word_before_it(self):
        self.assertEqual(punctuate("hello comma world"), "hello, world")

    def test_no_space_is_left_in_front_of_the_mark(self):
        self.assertNotIn(" ,", punctuate("hello comma world"))

    def test_a_space_follows_the_mark(self):
        self.assertNotIn(",w", punctuate("hello comma world"))

    def test_a_mark_at_the_very_end_leaves_no_trailing_space(self):
        self.assertEqual(punctuate("that is all period"), "that is all.")

    def test_an_opening_quote_takes_a_space_before_and_none_after(self):
        self.assertEqual(punctuate("he said open quote hello"), 'he said "hello')

    def test_a_closing_mark_does_not_take_the_sentences_full_stop_with_it(self):
        self.assertEqual(punctuate("he said open quote hi close quote."),
                         'he said "hi".')

    def test_a_dash_gets_a_space_on_both_sides(self):
        self.assertEqual(punctuate("we should go dash if we can"),
                         "we should go - if we can")

    def test_a_line_break_has_no_space_around_it(self):
        self.assertEqual(punctuate("first item new line second item"),
                         "first item\nsecond item")

    def test_the_next_sentence_is_capitalised_the_way_whisper_would_have(self):
        self.assertEqual(punctuate("that is all period the next thing"),
                         "that is all. The next thing")

    def test_the_first_word_of_the_text_is_left_exactly_as_whisper_wrote_it(self):
        """Capitals are repaired only where this stage destroyed one. Whisper
        writes the opening capital and it is not this stage's to second-guess."""
        self.assertEqual(punctuate("that is all period"), "that is all.")

    def test_nothing_is_capitalised_after_a_comma(self):
        self.assertEqual(punctuate("hello comma world"), "hello, world")

    def test_a_word_that_is_already_capitalised_is_left_alone(self):
        self.assertEqual(punctuate("that is all period Peter went home"),
                         "that is all. Peter went home")

    def test_two_marks_running_together(self):
        self.assertEqual(punctuate("that is all period new paragraph next"),
                         "that is all.\n\nNext")


class TheEscape(unittest.TestCase):
    def test_saying_literal_keeps_the_word(self):
        self.assertEqual(punctuate("the literal comma"), "the comma")
        self.assertEqual(punctuate("literal comma"), "comma")

    def test_the_escape_rescues_every_case_the_rule_gets_wrong(self):
        self.assertEqual(punctuate("I am not going literal full stop."),
                         "I am not going full stop.")
        self.assertEqual(punctuate("Add literal comma here."), "Add comma here.")

    def test_the_escape_word_itself_is_the_only_thing_removed(self):
        self.assertEqual(punctuate("say literal question mark out loud"),
                         "say question mark out loud")

    def test_the_escape_does_not_eat_whispers_punctuation(self):
        self.assertEqual(punctuate("Well, literal comma, then."),
                         "Well, comma, then.")

    def test_the_escape_in_front_of_an_ordinary_word_does_nothing(self):
        self.assertEqual(punctuate("a literal translation"), "a literal translation")

    def test_the_escape_can_be_turned_off(self):
        rules = rules_mod.from_mapping(
            {"escape_word": "",
             "marks": [{"name": "comma", "say": ["comma"], "insert": ","}]},
            Path("x.toml"))
        self.assertEqual(apply("literal comma", rules).text, "literal,")

    def test_an_escape_word_of_more_than_one_word_is_refused(self):
        with self.assertRaises(ConfigError):
            rules_mod.from_mapping({"escape_word": "say the words"}, Path("x.toml"))


class DoesNothingWhenThereIsNothingToDo(unittest.TestCase):
    """The stage runs over every dictation, and almost none of them contain a
    spoken mark. On those it has to be the identity, not merely harmless."""

    ORDINARY = [
        "So the thing I wanted to say is that the overlay should be calm.",
        "It was late and we went home.",
        "e-mail me at some point, would you?",
        "The result was 3.5 per cent, which is fine.",
        "Don't touch it - it's working.",
        "",
        "   ",
    ]

    def test_ordinary_speech_comes_back_identical(self):
        for line in self.ORDINARY:
            with self.subTest(line=line):
                result = apply(line, shipped())
                self.assertEqual(result.text, line)
                self.assertFalse(result.changed)
                self.assertEqual(result.applied, [])

    def test_a_rule_set_with_no_marks_is_the_identity(self):
        rules = rules_mod.PunctuationRules(marks=[])
        self.assertEqual(apply("hello comma world", rules).text, "hello comma world")


class EverySubstitutionIsVisibleInTheLog(unittest.TestCase):
    """A mark in the wrong place has to be traceable. The word he spoke is gone
    from the pasted text, so the log is the only way back to it."""

    def setUp(self):
        # tests/__init__.py silences logging for the whole suite; this class is
        # about what reaches the log, so it is turned back on just here.
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, logging.CRITICAL)

    def test_each_substitution_is_logged_with_the_words_that_produced_it(self):
        with self.assertLogs("dictate.punctuation.engine", level=logging.INFO) as caught:
            punctuate("Hello, comma, world. That is all period.")
        joined = "\n".join(caught.output)
        self.assertEqual(len(caught.output), 2)
        self.assertIn('"comma" -> \',\'', joined)
        self.assertIn('"period" -> \'.\'', joined)

    def test_the_log_line_carries_whispers_own_text_around_it(self):
        with self.assertLogs("dictate.punctuation.engine", level=logging.INFO) as caught:
            punctuate("Hello, comma, world.")
        self.assertIn("⟦comma⟧", caught.output[0])
        self.assertIn("Hello,", caught.output[0])

    def test_the_same_lines_are_on_the_result_for_dictate_punctuate(self):
        result = apply("Hello, comma, world.", shipped())
        self.assertEqual(len(result.applied), 1)
        self.assertIn("comma", result.applied[0])

    def test_nothing_is_logged_when_nothing_happened(self):
        logger = logging.getLogger("dictate.punctuation.engine")
        with self.assertLogs(logger, level=logging.INFO) as caught:
            logger.info("marker")
            punctuate("The comma goes here.")
        self.assertEqual(caught.output, ["INFO:dictate.punctuation.engine:marker"])


class TheShippedMarks(unittest.TestCase):
    def test_every_phrase_he_can_say(self):
        expected = {
            "full stop": ["period", "full stop"],
            "comma": ["comma"],
            "question mark": ["question mark"],
            "exclamation mark": ["exclamation mark", "exclamation point"],
            "colon": ["colon"],
            "semicolon": ["semicolon", "semi colon"],
            "dash": ["dash"],
            "open quote": ["open quote", "open quotes"],
            "close quote": ["close quote", "close quotes"],
            "open bracket": ["open bracket", "open brackets", "open parenthesis"],
            "close bracket": ["close bracket", "close brackets", "close parenthesis"],
            "new line": ["new line"],
            "new paragraph": ["new paragraph"],
        }
        got = {m.name: [" ".join(p) for p in m.say] for m in shipped().marks}
        self.assertEqual(got, expected)

    def test_each_one_produces_its_mark(self):
        cases = [
            ("say it period", "."),
            ("say it full stop", "."),
            ("say it comma", ","),
            ("say it question mark", "?"),
            ("say it exclamation mark", "!"),
            ("say it exclamation point", "!"),
            ("say it colon", ":"),
            ("say it semicolon", ";"),
            ("say it semi colon", ";"),
        ]
        for said, mark in cases:
            with self.subTest(said=said):
                self.assertEqual(punctuate(said), "say it" + mark)

    def test_a_longer_phrase_wins_over_a_shorter_one(self):
        """"new paragraph" must never be read as "new line", and "question
        mark" must never leave a stray "mark"."""
        self.assertEqual(punctuate("first new paragraph second"), "first\n\nSecond")
        self.assertEqual(punctuate("is it question mark"), "is it?")

    def test_hyphen_is_deliberately_not_a_mark(self):
        """An unspaced hyphen welds two words together when it lands wrong, and
        Whisper writes real hyphens itself."""
        self.assertNotIn("hyphen", {p[0] for m in shipped().marks for p in m.say})
        self.assertEqual(punctuate("a hyphen goes here"), "a hyphen goes here")

    def test_bare_quote_and_bare_bracket_are_deliberately_not_marks(self):
        self.assertEqual(punctuate("he read a quote from the report"),
                         "he read a quote from the report")
        self.assertEqual(punctuate("check the bracket"), "check the bracket")


class RulesFileValidation(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())

    def test_the_shipped_file_loads(self):
        rules = shipped()
        self.assertEqual(len(rules.marks), 13)
        self.assertEqual(rules.escape_word, "literal")

    def test_unknown_setting_is_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            rules_from('escape_words = "literal"\n', self.tmp)
        self.assertIn("escape_words", ctx.exception.message)

    def test_unknown_mark_field_is_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            rules_from('[[marks]]\nname = "x"\nsay = ["comma"]\ninsert = ","\n'
                       'pattern = "x"\n', self.tmp)
        self.assertIn("pattern", ctx.exception.message)

    def test_an_unknown_spacing_names_the_valid_ones(self):
        with self.assertRaises(ConfigError) as ctx:
            rules_from('[[marks]]\nname = "x"\nsay = ["comma"]\ninsert = ","\n'
                       'spacing = "sideways"\n', self.tmp)
        self.assertIn("around", ctx.exception.remedy)

    def test_two_marks_claiming_the_same_phrase_are_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            rules_from('[[marks]]\nname = "a"\nsay = ["comma"]\ninsert = ","\n'
                       '[[marks]]\nname = "b"\nsay = ["comma"]\ninsert = ";"\n', self.tmp)
        self.assertIn("both", ctx.exception.message)

    def test_a_phrase_with_punctuation_in_it_is_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            rules_from('[[marks]]\nname = "x"\nsay = ["semi-colon"]\ninsert = ";"\n',
                       self.tmp)
        self.assertIn("word by word", ctx.exception.remedy)

    def test_a_mark_with_no_insert_is_rejected(self):
        with self.assertRaises(ConfigError):
            rules_from('[[marks]]\nname = "x"\nsay = ["comma"]\n', self.tmp)

    def test_a_byte_order_mark_does_not_look_like_a_broken_file(self):
        path = self.tmp / "bom.toml"
        path.write_bytes(b"\xef\xbb\xbfescape_word = \"literal\"\n")
        self.assertEqual(rules_mod.load(path).escape_word, "literal")

    def test_a_missing_file_explains_the_options(self):
        with self.assertRaises(ConfigError) as ctx:
            rules_mod.load(self.tmp / "nope.toml")
        self.assertIn("enabled = false", ctx.exception.remedy)

    def test_the_new_section_example_in_the_comments_really_works(self):
        """The file ends with a copy-and-edit block for "new section", because
        adding one without asking anyone is the point of it being a data file.
        A worked example that does not work is worse than none, so it is taken
        out of the comments and run."""
        text = SHIPPED.read_text(encoding="utf-8")
        block = text.split("# [[marks]]\n", 1)[1].split("#\n", 1)[0]
        example = "[[marks]]\n" + "\n".join(
            line.removeprefix("# ") for line in block.splitlines())
        rules = rules_from(example, self.tmp)
        self.assertEqual(apply("one new section two", rules).text,
                         "one\n\n---\n\nTwo")

    def test_the_toml_trap_is_documented_in_the_shipped_file(self):
        """A plain setting written after a [[marks]] block silently becomes a
        field of it - the same trap as cleanup-rules.toml."""
        text = SHIPPED.read_text(encoding="utf-8")
        self.assertIn("belongs to that table", text)
        self.assertLess(text.index("guard_words = ["), text.index("\n[[marks]]"))


class OffByDefaultIsExactlyTodaysBehaviour(unittest.TestCase):
    def test_the_default_is_off(self):
        self.assertFalse(config_mod.Config().punctuation.enabled)

    def test_a_config_with_no_punctuation_section_still_loads(self):
        """His dictate.toml was written before this existed."""
        cfg = config_mod.from_mapping({"hotkey": {"mode": "hold"}})
        self.assertFalse(cfg.punctuation.enabled)
        self.assertEqual(cfg.punctuation.rules_file, "voice-punctuation.toml")

    def test_the_shipped_example_ships_it_off(self):
        cfg = config_mod.load(REPO / "config" / "dictate.example.toml")
        self.assertFalse(cfg.punctuation.enabled)

    def test_a_disabled_service_is_the_identity(self):
        service = PunctuationService(SHIPPED, enabled=False)
        service.load()
        for line in ("hello comma world", "that is all period"):
            with self.subTest(line=line):
                self.assertEqual(service(line).text, line)

    def test_the_pipeline_pastes_the_cleaned_text_when_there_is_no_punctuator(self):
        from tests import fakes
        pipeline = _pipeline(fakes, punctuator=None, said="Um, hello comma world.")
        pipeline.start_utterance()
        pipeline.push_audio(b"\0" * 32000)
        pipeline.finish_utterance()
        self.assertEqual([text for text, _ in pipeline.injector.sent],
                         ["Hello comma world."])

    def test_the_pipeline_punctuates_after_cleanup_when_there_is_one(self):
        from tests import fakes
        service = PunctuationService(SHIPPED, enabled=True)
        service.load()
        pipeline = _pipeline(fakes, punctuator=service, said="Um, hello comma world.")
        pipeline.start_utterance()
        pipeline.push_audio(b"\0" * 32000)
        pipeline.finish_utterance()
        self.assertEqual([text for text, _ in pipeline.injector.sent],
                         ["Hello, world."])

    def test_a_punctuator_that_explodes_still_pastes_the_dictation(self):
        from tests import fakes

        def boom(text):
            raise RuntimeError("no")

        pipeline = _pipeline(fakes, punctuator=boom, said="hello comma world")
        pipeline.start_utterance()
        pipeline.push_audio(b"\0" * 32000)
        pipeline.finish_utterance()
        self.assertEqual([text for text, _ in pipeline.injector.sent],
                         ["hello comma world"])


def _pipeline(fakes, *, punctuator, said):
    from dictate.pipeline import Pipeline
    return Pipeline(
        batch=fakes.FakeBatch(said),
        cleaner=lambda text: clean(text, cleanup_rules_mod.load(SHIPPED_CLEANUP)),
        injector=fakes.FakeInjector(),
        windows=fakes.FakeWindows(),
        overlay=fakes.FakeOverlay(),
        punctuator=punctuator,
        submit=fakes.InlineSubmit(),
    )


class Service(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "marks.toml"
        self.path.write_text('[[marks]]\nname = "comma"\nsay = ["comma"]\n'
                             'insert = ","\n', encoding="utf-8")

    def test_it_punctuates(self):
        service = PunctuationService(self.path, enabled=True)
        service.load()
        self.assertEqual(service("hello comma world").text, "hello, world")

    def test_it_reloads_when_the_file_changes(self):
        service = PunctuationService(self.path, enabled=True)
        service.load()
        self.assertEqual(service("hello comma world").text, "hello, world")
        time.sleep(0.01)
        self.path.write_text('[[marks]]\nname = "comma"\nsay = ["comma"]\n'
                             'insert = ";"\n', encoding="utf-8")
        self.assertEqual(service("hello comma world").text, "hello; world")

    def test_a_broken_edit_keeps_the_previous_rules_and_reports(self):
        told = []
        service = PunctuationService(self.path, enabled=True,
                                     notify=lambda level, msg: told.append((level, msg)))
        service.load()
        time.sleep(0.01)
        self.path.write_text("this is not toml at all [[[", encoding="utf-8")
        self.assertEqual(service("hello comma world").text, "hello, world")
        self.assertEqual(told[0][0], "error")
        self.assertIn("still using the previous", told[0][1])

    def test_a_broken_file_at_startup_is_reported_there_and_then(self):
        self.path.write_text("nonsense [[[", encoding="utf-8")
        with self.assertRaises(ConfigError):
            PunctuationService(self.path, enabled=True).load()


if __name__ == "__main__":
    unittest.main()
