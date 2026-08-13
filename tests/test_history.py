"""The dictation history: what it keeps, what it looks like, and how it ends.

This is a file of everything he has said at his computer, so the tests that
matter most are not the happy ones: that it stays bounded, that deleting it
really deletes it, that turning it off writes nothing at all, and that a disk
that will not take it costs him a message rather than a dictation.

All of it runs anywhere - the store is plain standard library, like
`recovery.py` and `tray.py`. The only part that needs his machine is the double
click that opens the file, which is `os.startfile`.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from dictate import config as config_mod, history as history_mod
from dictate.history import Entry, HistoryStore, entries_in

#: A fixed moment, so the rendered stamps in these tests are stable.
NOON = time.mktime((2026, 8, 13, 12, 30, 15, 0, 0, -1))


class HistoryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "history.txt"
        self.notices: list[tuple[str, str]] = []

    def store(self, **kwargs) -> HistoryStore:
        kwargs.setdefault("notify", lambda level, message:
                          self.notices.append((level, message)))
        return HistoryStore(self.path, **kwargs)

    def read(self) -> str:
        return self.path.read_text(encoding="utf-8")


class WhatItKeeps(HistoryTestCase):
    def test_the_text_that_was_pasted_is_in_there(self):
        self.assertTrue(self.store().record("Ship it on Thursday."))
        self.assertIn("Ship it on Thursday.", self.read())

    def test_it_says_when_and_for_how_long(self):
        entry = Entry.of("Hello.", spoke_s=4.25, when=NOON)
        rendered = entry.render()
        self.assertIn("2026", rendered)
        self.assertIn("12:30:15", rendered)
        self.assertIn("4.2s", rendered)

    def test_what_whisper_said_is_kept_when_dictate_changed_it(self):
        """Either stage counts: the cleanup rules delete and spoken punctuation
        substitutes, and both are things he may want to see the before of."""
        entry = Entry.of("Ship it.", raw="Um, ship it.", when=NOON)
        self.assertEqual(entry.raw, "Um, ship it.")
        self.assertIn("as Whisper heard it", entry.render())

    def test_and_is_not_kept_when_they_did_not(self):
        """Otherwise every entry is the same sentence twice, and the pair stops
        meaning "look, here is what was removed"."""
        entry = Entry.of("Ship it.", raw="Ship it. ", when=NOON)
        self.assertEqual(entry.raw, "")
        self.assertNotIn("as Whisper heard it", entry.render())

    def test_nothing_else_about_the_dictation_is_recorded(self):
        """Deliberate: how long transcription took is a developer's question
        and is already in the log, and where the text was pasted would make
        this a record of his day rather than of his words."""
        fields = {f for f in Entry.__dataclass_fields__}
        self.assertEqual(fields, {"when", "spoke_s", "text", "raw"})

    def test_an_empty_dictation_is_not_an_entry(self):
        store = self.store()
        self.assertFalse(store.record("   "))
        self.assertFalse(self.path.exists())


class WhatItLooksLike(HistoryTestCase):
    def test_the_newest_dictation_is_the_first_thing_he_sees(self):
        store = self.store()
        store.record("The first thing I said.")
        store.record("The second thing I said.")
        text = self.read()
        self.assertLess(text.index("second thing"), text.index("first thing"))

    def test_it_opens_with_what_it_is_and_how_to_get_rid_of_it(self):
        self.store(keep=25).record("Anything.")
        head = self.read().split(history_mod.RULE, 1)[0]
        self.assertIn("newest first", head)
        self.assertIn("dictate history --delete", head)
        self.assertIn("keep = 25", head)
        self.assertIn("enabled = false", head)
        self.assertIn("stays on this computer", head)

    def test_a_long_dictation_is_wrapped_so_it_can_be_read_in_notepad(self):
        long_one = " ".join(["word"] * 400)
        self.store().record(long_one)
        lines = self.read().splitlines()
        self.assertTrue(all(len(line) <= 80 for line in lines))
        # Wrapped, not shortened: every word he said is still in there.
        self.assertEqual(self.read().count("word"), 400)

    def test_his_own_line_breaks_survive(self):
        self.store().record("First line.\nSecond line.")
        body = self.read()
        self.assertIn("  First line.\n  Second line.", body)

    def test_every_line_of_a_dictation_is_indented(self):
        """Which is what makes splitting the file back into entries exact: no
        line of his text can ever look like the rule between entries."""
        self.store().record(history_mod.RULE + "\nand more")
        # Everything after the rule and the one stamp line under it is his.
        his = self.read().split(history_mod.RULE + "\n", 1)[1].split("\n", 1)[1]
        for line in his.splitlines():
            if line.strip():
                self.assertTrue(line.startswith(history_mod.INDENT), line)

    def test_a_dictation_that_reads_out_the_rule_does_not_split_the_file(self):
        store = self.store()
        store.record(history_mod.RULE)
        store.record("An ordinary sentence.")
        self.assertEqual(store.count(), 2)

    def test_it_is_utf_8_and_keeps_what_he_actually_said(self):
        self.store().record("Naïve café — €5, £4.")
        self.assertIn("Naïve café — €5, £4.", self.read())


class HowItIsBounded(HistoryTestCase):
    def test_it_stops_at_the_cap_and_the_oldest_go(self):
        store = self.store(keep=5)
        for index in range(12):
            store.record(f"Dictation number {index}.")
        self.assertEqual(store.count(), 5)
        text = self.read()
        self.assertIn("number 11.", text)
        self.assertIn("number 7.", text)
        self.assertNotIn("number 6.", text)

    def test_the_cap_is_his_to_change(self):
        big = self.store(keep=500)
        self.assertEqual(config_mod.HistoryConfig().keep, 200)
        self.assertIn("500", big.describe)

    def test_a_cap_of_one_keeps_only_the_last_thing_he_said(self):
        store = self.store(keep=1)
        store.record("Old.")
        store.record("New.")
        self.assertEqual(store.count(), 1)
        self.assertIn("New.", self.read())
        self.assertNotIn("Old.", self.read())

    def test_a_cap_of_zero_is_the_same_as_off(self):
        store = self.store(keep=0)
        self.assertFalse(store.enabled)
        self.assertFalse(store.record("Anything at all."))
        self.assertFalse(self.path.exists())

    def test_the_file_does_not_grow_without_limit(self):
        store = self.store(keep=20)
        for index in range(200):
            store.record(f"A sentence of a fairly ordinary length, number {index}.")
        self.assertEqual(store.count(), 20)
        self.assertLess(self.path.stat().st_size, 20_000)


class TurningItOffAndDeletingIt(HistoryTestCase):
    def test_off_means_nothing_is_written_at_all(self):
        store = self.store(enabled=False)
        self.assertFalse(store.record("Something I would rather not keep."))
        self.assertFalse(self.path.exists())
        self.assertIn("off", store.describe)

    def test_delete_removes_the_file_itself(self):
        store = self.store()
        store.record("Something.")
        self.assertTrue(store.delete())
        self.assertFalse(self.path.exists())
        self.assertEqual(store.count(), 0)

    def test_delete_with_nothing_there_says_so_rather_than_failing(self):
        self.assertFalse(self.store().delete())

    def test_delete_works_even_with_the_history_turned_off(self):
        """He turns it off; what is already on disk is still his to be rid of,
        and turning it off must not be the thing that traps it there."""
        self.store().record("From before he turned it off.")
        self.assertTrue(self.store(enabled=False).delete())
        self.assertFalse(self.path.exists())

    def test_delete_takes_a_half_written_file_with_it(self):
        store = self.store()
        store.record("Something.")
        leftover = self.path.with_name(self.path.name + ".writing")
        leftover.write_text("half of a rewrite", encoding="utf-8")
        store.delete()
        self.assertFalse(leftover.exists())


class WhenTheDiskWillNotTakeIt(HistoryTestCase):
    def store_at(self, path: Path) -> HistoryStore:
        return HistoryStore(path, notify=lambda level, message:
                            self.notices.append((level, message)))

    def test_it_reports_and_carries_on_rather_than_raising(self):
        # A directory where the file should be: writing it can only fail.
        blocked = self.dir / "history.txt"
        blocked.mkdir()
        store = self.store_at(blocked)
        self.assertFalse(store.record("Hello."))
        self.assertEqual(self.notices[0][0], "warning")
        self.assertIn("history", self.notices[0][1])

    def test_it_says_so_once_and_then_stops_going_on_about_it(self):
        blocked = self.dir / "history.txt"
        blocked.mkdir()
        store = self.store_at(blocked)
        for _ in range(5):
            store.record("Hello.")
        self.assertEqual(len(self.notices), 1)

    def test_a_file_that_cannot_be_read_is_not_an_entry_count(self):
        store = self.store()
        self.assertEqual(store.count(), 0)

    def test_the_write_is_atomic_so_an_interrupted_one_leaves_the_old_file(self):
        """`os.replace` is a rename: the history is the old one or the new one,
        never half of one. This is the property, checked where it is decided."""
        source = Path(history_mod.__file__).read_text(encoding="utf-8")
        write = source.split("def _write(", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("os.replace(temp, self.path)", write)


class WhereItLives(unittest.TestCase):
    """`config.load` resolves the config's own path, so these compare against a
    resolved one too. On Windows that is not pedantry: a temporary folder comes
    back as the 8.3 short name (`RUNNER~1`), and `resolve()` turns it into the
    long one - two spellings of the same folder, and the history has to be
    beside the config whichever spelling he typed."""

    def test_it_sits_beside_his_config_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dictate.toml"
            path.write_text("[history]\nkeep = 10\n", encoding="utf-8")
            cfg = config_mod.load(path)
            self.assertEqual(history_mod.path_for(cfg),
                             path.resolve().parent / "history.txt")

    def test_with_no_config_file_it_goes_where_dictate_keeps_its_own_state(self):
        from dictate import instance

        cfg = config_mod.load(None)
        self.assertEqual(history_mod.path_for(cfg),
                         instance.state_dir() / "history.txt")

    def test_he_can_put_it_somewhere_else(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dictate.toml"
            path.write_text('[history]\nfile = "notes/said.txt"\n', encoding="utf-8")
            cfg = config_mod.load(path)
            self.assertEqual(history_mod.path_for(cfg),
                             path.resolve().parent / "notes" / "said.txt")

    def test_a_folder_that_does_not_exist_yet_is_made(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "not" / "there" / "history.txt"
            HistoryStore(target).record("Hello.")
            self.assertTrue(target.exists())


class TheConfigSection(unittest.TestCase):
    def test_it_is_on_by_default_and_keeps_two_hundred(self):
        cfg = config_mod.load(None)
        self.assertTrue(cfg.history.enabled)
        self.assertEqual(cfg.history.keep, 200)

    def test_a_negative_cap_is_refused_with_a_reason(self):
        from dictate.errors import ConfigError

        with self.assertRaises(ConfigError) as ctx:
            config_mod.from_mapping({"history": {"keep": -1}})
        self.assertIn("cannot be negative", ctx.exception.message)

    def test_an_absurd_cap_is_refused_because_of_what_it_would_cost_him(self):
        from dictate.errors import ConfigError

        with self.assertRaises(ConfigError) as ctx:
            config_mod.from_mapping({"history": {"keep": 10_000_000}})
        self.assertIn("rewritten", ctx.exception.message)

    def test_a_typo_in_the_section_is_an_error_like_every_other_section(self):
        from dictate.errors import ConfigError

        with self.assertRaises(ConfigError):
            config_mod.from_mapping({"history": {"kepe": 10}})


class TheWholeWayThrough(HistoryTestCase):
    """The pipeline and the store, wired together the way `app.py` wires them,
    so the seam between "a dictation happened" and "a line in his file" is
    covered as well as each side of it."""

    def dictate(self, store: HistoryStore, said: str, cleaned: str | None = None):
        from dictate.cleanup.engine import CleanResult
        from dictate.pipeline import Pipeline

        from . import fakes

        overlay = fakes.FakeOverlay()
        pipeline = Pipeline(
            batch=fakes.FakeBatch(said),
            cleaner=lambda text: CleanResult(text=cleaned or text, original=text),
            injector=fakes.FakeInjector(),
            windows=fakes.FakeWindows(),
            overlay=overlay,
            streaming=fakes.FakeStreaming(),
            submit=fakes.InlineSubmit(),
            record=store.record,
            sample_rate=16000,
        )
        pipeline.start_utterance()
        pipeline.push_audio(b"\x00\x00" * 16000)   # one second
        pipeline.finish_utterance()
        return pipeline, overlay

    def test_a_dictation_becomes_a_line_he_can_read(self):
        store = self.store()
        self.dictate(store, "Um, ship it on Thursday.", "Ship it on Thursday.")
        text = self.read()
        self.assertIn("Ship it on Thursday.", text)
        self.assertIn("as Whisper heard it: Um, ship it on Thursday.", text)
        self.assertIn("(1.0s of speaking)", text)
        self.assertEqual(store.count(), 1)

    def test_the_caption_text_is_nowhere_in_the_file(self):
        """Constraint 4 again, in the one place it would be easy to leak: a file
        that is written on the same thread that just pasted."""
        store = self.store()
        _, overlay = self.dictate(store, "Ship it.")
        self.assertNotIn("CHUNK", self.read())

    def test_a_history_that_is_off_leaves_no_file_behind_a_real_dictation(self):
        store = self.store(enabled=False)
        pipeline, _ = self.dictate(store, "Ship it.")
        self.assertEqual(pipeline.completed, 1)
        self.assertFalse(self.path.exists())


class SplittingTheFileBack(unittest.TestCase):
    def test_an_empty_file_has_no_entries(self):
        self.assertEqual(entries_in(""), [])

    def test_a_file_of_only_the_header_has_no_entries(self):
        self.assertEqual(entries_in(history_mod.header(200)), [])

    def test_each_entry_is_returned_whole(self):
        one = Entry.of("First.", when=NOON).render()
        two = Entry.of("Second.", when=NOON).render()
        blocks = entries_in(history_mod.header(200) + one + two)
        self.assertEqual(blocks, [one, two])


if __name__ == "__main__":
    unittest.main()
