"""The three controls over how the captions look, and the file they live in.

Nobody here can see the panel, so what is held instead is everything that
decides it: which names exist, what happens at the ends of the ladder, what
`dictate look` writes into a config file, and - the part with the most ways to
be quietly wrong - that writing one setting never disturbs another line, another
section, or a single one of that file's comments. His `dictate.toml` is his.

The arithmetic these names feed is in `tests/test_overlay.py`.
"""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from dictate import cli, config as config_mod, overlay_size as size_mod
from dictate.errors import ConfigError, DictateError

REPO = Path(__file__).resolve().parent.parent
EXAMPLE = REPO / "config" / "dictate.example.toml"


def run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class TheLadder(unittest.TestCase):
    def test_it_runs_smallest_to_largest_with_no_repeats(self):
        values = [size_mod.multiplier(n) for n in size_mod.names()]
        self.assertEqual(values, sorted(values))
        self.assertEqual(len(set(values)), len(values))

    def test_the_largest_is_the_panel_exactly_as_it_used_to_ship(self):
        """`huge` has to be 1.0, or the pixel values in the config stop meaning
        what they say and "put it back how it was" stops being one word."""
        self.assertEqual(size_mod.multiplier("huge"), 1.0)
        self.assertEqual(size_mod.caption_px("huge"), 24)

    def test_the_shipped_default_is_about_forty_percent_less_type(self):
        self.assertEqual(size_mod.DEFAULT, "compact")
        self.assertEqual(size_mod.caption_px("compact"), 15)
        self.assertAlmostEqual(1 - size_mod.multiplier("compact"), 0.375)

    def test_a_step_stops_at_the_ends_rather_than_falling_off_them(self):
        self.assertEqual(size_mod.step("small", -1), "small")
        self.assertEqual(size_mod.step("huge", 1), "huge")
        self.assertEqual(size_mod.step("compact", 1), "medium")
        self.assertEqual(size_mod.step("compact", -1), "small")

    def test_a_step_from_something_unrecognisable_starts_from_the_default(self):
        """A menu built before a config was reloaded must not raise on a thread
        that owns an icon."""
        self.assertEqual(size_mod.step("", 1), "medium")

    def test_a_name_that_is_not_a_size_is_refused_with_the_list(self):
        with self.assertRaises(ConfigError) as ctx:
            size_mod.multiplier("titchy")
        self.assertIn("titchy", ctx.exception.message)
        for name in size_mod.names():
            self.assertIn(name, ctx.exception.remedy)

    def test_bigger_and_smaller_are_words_it_understands(self):
        self.assertEqual(size_mod.apply_word("bigger", "compact"), "medium")
        self.assertEqual(size_mod.apply_word("smaller", "compact"), "small")
        self.assertEqual(size_mod.apply_word("large", "compact"), "large")
        with self.assertRaises(ConfigError):
            size_mod.apply_word("mahoosive", "compact")

    def test_an_empty_override_follows_the_one_knob(self):
        self.assertEqual(size_mod.effective("medium"), ("medium", "medium"))
        self.assertEqual(size_mod.effective("medium", text_size="huge"),
                         ("huge", "medium"))
        self.assertEqual(size_mod.effective("medium", panel_size="small"),
                         ("medium", "small"))

    def test_the_ladder_says_where_he_is_and_what_is_either_side(self):
        lines = size_mod.ladder("compact", "compact")
        self.assertEqual(len(lines), len(size_mod.names()))
        marked = [line for line in lines if "<-" in line]
        self.assertEqual(len(marked), 1)
        self.assertIn("compact", marked[0])
        self.assertIn("text", marked[0])
        self.assertIn("panel", marked[0])

    def test_the_ladder_marks_both_when_they_have_been_pulled_apart(self):
        marked = [line for line in size_mod.ladder("large", "small")
                  if "<-" in line]
        self.assertEqual(len(marked), 2)


class WritingItToHisConfig(unittest.TestCase):
    def test_it_replaces_the_value_in_place(self):
        text = '[overlay]\nsize = "huge"\nfont_size = 18\n'
        self.assertEqual(size_mod.set_in_text(text, {"size": "small"}),
                         '[overlay]\nsize = "small"\nfont_size = 18\n')

    def test_font_size_is_not_mistaken_for_size(self):
        """`size` is the end of `font_size`, and an unanchored match here would
        turn a font size into a size name and stop the config loading."""
        text = '[overlay]\nfont_size = 18\n'
        self.assertIn('font_size = 18', size_mod.set_in_text(text, {"size": "small"}))
        self.assertIn('size = "small"', size_mod.set_in_text(text, {"size": "small"}))

    def test_text_size_and_size_are_not_each_other(self):
        text = '[overlay]\nsize = "huge"\ntext_size = "small"\n'
        out = size_mod.set_in_text(text, {"text_size": "medium"})
        self.assertIn('size = "huge"', out)
        self.assertIn('text_size = "medium"', out)

    def test_the_comment_on_the_line_survives(self):
        """Through `config_edit`, which is the one writer in this repository
        that touches his config: the value changes and the comment after it is
        the comment it was, gap and all."""
        text = '[overlay]\nsize = "huge"   # small, compact, medium\n'
        out = size_mod.set_in_text(text, {"size": "medium"})
        self.assertIn('size = "medium"   # small, compact, medium', out)

    def test_his_line_endings_and_byte_order_mark_are_not_a_change_he_asked_for(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dictate.toml"
            path.write_bytes(b"\xef\xbb\xbf"
                             + b'[overlay]\r\nsize = "huge"\r\nlines = 2\r\n')
            size_mod.write(path, {"size": "small"})
            data = path.read_bytes()
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
        self.assertIn(b'size = "small"\r\n', data)
        self.assertNotIn(b"size = \"small\"\n\r", data)

    def test_a_missing_key_is_added_where_it_will_be_seen(self):
        text = '[hotkey]\nmode = "hold"\n\n[overlay]\nfont_size = 18\n'
        out = size_mod.set_in_text(text, {"size": "large"})
        lines = out.splitlines()
        self.assertEqual(lines[lines.index("[overlay]") + 1], 'size = "large"')
        self.assertIn('mode = "hold"', out)

    def test_a_key_from_another_section_is_left_alone(self):
        """The trap this exists for: `[history] keep` and `[overlay] size` are
        both one word in one section, and a search that does not stop at the
        next header would write into whichever came first."""
        text = '[overlay]\nfont_size = 18\n\n[whisper]\nsize = "wrong"\n'
        out = size_mod.set_in_text(text, {"size": "small"})
        self.assertIn('size = "wrong"', out)
        self.assertEqual(out.count('size = "small"'), 1)
        self.assertLess(out.index('size = "small"'), out.index("[whisper]"))

    def test_no_overlay_section_at_all_grows_one(self):
        out = size_mod.set_in_text('[hotkey]\nmode = "hold"\n', {"size": "small"})
        self.assertIn("[overlay]", out)
        self.assertTrue(config_mod.from_mapping(_toml(out)))

    def test_all_four_settings_can_be_written_at_once(self):
        out = size_mod.set_in_text(EXAMPLE.read_text(encoding="utf-8"), {
            "size": "large", "text_size": "huge", "panel_size": "small",
            "font_family": "Consolas"})
        cfg = config_mod.from_mapping(_toml(out)).overlay
        self.assertEqual((cfg.size, cfg.text_size, cfg.panel_size, cfg.font_family),
                         ("large", "huge", "small", "Consolas"))

    def test_the_shipped_example_survives_being_edited_by_this(self):
        """It is the file `dictate init` writes, so it is the file this will be
        run against - comments and all, which is the whole point of editing one
        line rather than rewriting the file from the dataclasses."""
        before = EXAMPLE.read_text(encoding="utf-8")
        after = size_mod.set_in_text(before, {"size": "medium"})
        self.assertEqual(before.count("#"), after.count("#"))
        self.assertEqual(len(before.splitlines()), len(after.splitlines()))
        self.assertEqual(config_mod.from_mapping(_toml(after)).overlay.size,
                         "medium")

    def test_a_setting_this_does_not_own_is_refused_rather_than_written(self):
        """It writes four things. Anything else reaching this is a programming
        error, and a config writer that will write anything is a config writer
        that will eventually write the wrong thing."""
        with self.assertRaises(ConfigError):
            size_mod.set_in_text("[overlay]\n", {"opacity": "0.5"})

    def test_a_font_name_with_a_quote_in_it_is_escaped_rather_than_broken(self):
        out = size_mod.set_in_text("[overlay]\n", {"font_family": 'Fira "Code"'})
        self.assertEqual(_toml(out)["overlay"]["font_family"], 'Fira "Code"')

    def test_the_file_is_replaced_whole_or_not_at_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dictate.toml"
            path.write_text('[overlay]\nsize = "huge"\n', encoding="utf-8")
            size_mod.write(path, {"size": "small"})
            self.assertIn('size = "small"', path.read_text(encoding="utf-8"))
            # The temporary file it writes through is not left behind.
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["dictate.toml"])

    def test_the_three_are_written_in_the_order_they_are_read_in(self):
        out = size_mod.set_in_text('[overlay]\nfont_size = 18\n', {
            "size": "small", "text_size": "", "panel_size": ""})
        keys = [line.split(" =")[0] for line in out.splitlines() if " =" in line]
        self.assertEqual(keys[:3], ["size", "text_size", "panel_size"])

    def test_a_file_it_cannot_write_names_the_file_and_the_line_to_type(self):
        """`config_edit`'s message, deliberately: this is not the path a person
        reaches - `dictate look` refuses earlier, naming `dictate init` - so
        what matters here is that a failure still ends in something to do."""
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nowhere" / "dictate.toml"
            with self.assertRaises(ConfigError) as ctx:
                size_mod.write(missing, {"size": "small"})
        self.assertIn(str(missing), ctx.exception.message)
        self.assertIn('size = "small"', ctx.exception.remedy)

    def test_the_command_refuses_a_config_file_that_is_not_there_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = run(["--config", str(Path(tmp) / "nope.toml"), "look"])
        self.assertEqual(code, 2)
        self.assertIn("dictate init", err)

    def test_a_file_written_with_a_byte_order_mark_is_still_readable(self):
        """Notepad and Windows PowerShell both write one, and `config.load`
        already goes out of its way to cope. This has to as well."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dictate.toml"
            path.write_text('[overlay]\nsize = "huge"\n', encoding="utf-8-sig")
            size_mod.write(path, {"size": "small"})
            self.assertEqual(config_mod.load(path).overlay.size, "small")


class TheConfigKeys(unittest.TestCase):
    def test_a_config_with_none_of_the_new_keys_gets_the_new_default(self):
        """His dictate.toml was written before any of this existed. It has to
        keep loading, and it has to get the smaller panel he asked for."""
        cfg = config_mod.from_mapping({"overlay": {"font_size": 18}})
        self.assertEqual(cfg.overlay.size, "compact")
        self.assertEqual(cfg.overlay.text_size, "")
        self.assertEqual(cfg.overlay.panel_size, "")

    def test_the_old_look_is_one_word_away(self):
        cfg = config_mod.from_mapping({"overlay": {"size": "huge"}})
        self.assertEqual(cfg.overlay.size, "huge")

    def test_a_mistyped_size_is_refused_when_the_config_loads(self):
        for key in ("size", "text_size", "panel_size"):
            with self.subTest(key=key), self.assertRaises(ConfigError) as ctx:
                config_mod.from_mapping({"overlay": {key: "titchy"}})
            self.assertIn(key, ctx.exception.message)

    def test_an_empty_override_is_not_a_mistyped_one(self):
        cfg = config_mod.from_mapping({"overlay": {"text_size": "", "panel_size": ""}})
        self.assertEqual(cfg.overlay.text_size, "")

    def test_the_example_file_carries_all_three(self):
        raw = _toml(EXAMPLE.read_text(encoding="utf-8"))
        for key in ("size", "text_size", "panel_size", "font_family"):
            self.assertIn(key, raw["overlay"])


class TheLookCommand(unittest.TestCase):
    """`config.load` resolves the config's own path, and what the command
    prints is that resolved one. On Windows that is not pedantry: a temporary
    folder comes back as its 8.3 short name (`RUNNER~1`) and `resolve()` turns
    it into the long one - two spellings of one folder, so the tests compare
    against the spelling the product will use."""

    def config(self, tmp: str, text: str = "") -> Path:
        path = Path(tmp) / "dictate.toml"
        path.write_text(text or '[overlay]\nsize = "huge"\n', encoding="utf-8")
        return path.resolve()

    def test_it_says_what_the_captions_are_now_and_how_to_change_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.config(tmp)
            code, out, _ = run(["--config", str(path), "look"])
        self.assertEqual(code, 0)
        self.assertIn("huge", out)
        self.assertIn("dictate look smaller", out)
        self.assertIn("dictate overlay", out)
        for name in size_mod.names():
            self.assertIn(name, out)

    def test_one_word_changes_it_and_says_where_it_was_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.config(tmp)
            code, out, _ = run(["--config", str(path), "look", "smaller"])
            self.assertEqual(code, 0)
            self.assertIn(str(path), out)
            self.assertEqual(config_mod.load(path).overlay.size, "large")

    def test_the_one_knob_puts_a_split_pair_back_together(self):
        """Otherwise "make it all smaller" would silently do nothing to a panel
        whose text size and panel size he had pulled apart."""
        with tempfile.TemporaryDirectory() as tmp:
            path = self.config(
                tmp, '[overlay]\nsize = "huge"\ntext_size = "small"\n'
                     'panel_size = "large"\n')
            run(["--config", str(path), "look", "medium"])
            overlay = config_mod.load(path).overlay
        self.assertEqual((overlay.size, overlay.text_size, overlay.panel_size),
                         ("medium", "", ""))

    def test_the_words_and_the_box_can_be_moved_on_their_own(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.config(tmp)
            run(["--config", str(path), "look", "--text", "small",
                 "--panel", "large"])
            overlay = config_mod.load(path).overlay
        self.assertEqual((overlay.text_size, overlay.panel_size), ("small", "large"))
        self.assertEqual(overlay.size, "huge")           # the one knob untouched

    def test_the_font_is_as_easy_to_change_as_the_sizes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.config(tmp)
            code, out, _ = run(["--config", str(path), "look", "--font", "Consolas"])
            self.assertEqual(code, 0)
            self.assertEqual(config_mod.load(path).overlay.font_family, "Consolas")

    def test_a_size_it_does_not_know_is_a_message_and_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.config(tmp)
            code, _, err = run(["--config", str(path), "look", "enormous"])
        self.assertEqual(code, 2)
        self.assertIn("enormous", err)
        self.assertIn("smaller", err)

    def test_nothing_is_written_when_the_new_setting_is_impossible(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.config(tmp)
            before = path.read_text(encoding="utf-8")
            run(["--config", str(path), "look", "--text", "enormous"])
            self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_the_preview_command_can_try_all_three_without_keeping_any(self):
        """`dictate overlay` needs Windows, but the flags it parses do not, and
        which of them reach the config is the part worth holding."""
        args = cli.build_parser().parse_args(
            ["overlay", "--size", "small", "--text", "large",
             "--panel", "medium", "--font", "Consolas"])
        cfg = config_mod.Config()
        cfg.overlay.size = "huge"
        self.assertEqual(cli._look_changes(cfg, args), {
            "size": "small", "text_size": "large", "panel_size": "medium",
            "font_family": "Consolas"})

    def test_the_preview_keeps_nothing_unless_it_is_asked_to(self):
        args = cli.build_parser().parse_args(["overlay"])
        self.assertEqual(cli._look_changes(config_mod.Config(), args), {})
        self.assertFalse(args.keep)

    def test_the_command_that_keeps_what_he_just_looked_at_is_printed(self):
        args = cli.build_parser().parse_args(["overlay", "--size", "small"])
        self.assertEqual(cli._keep_command(args), "dictate overlay --size small --keep")

    def test_answering_the_question_at_the_end_is_what_keeps_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.config(tmp)
            cfg = config_mod.load(path)
            with contextlib.redirect_stdout(io.StringIO()):
                cli._offer_to_keep(cfg, {"size": "small"}, "dictate overlay --keep",
                                   ask=lambda _: "n")
            self.assertEqual(config_mod.load(path).overlay.size, "huge")
            with contextlib.redirect_stdout(io.StringIO()):
                cli._offer_to_keep(cfg, {"size": "small"}, "dictate overlay --keep",
                                   ask=lambda _: "y")
            self.assertEqual(config_mod.load(path).overlay.size, "small")

    def test_a_window_with_no_keyboard_behind_it_prints_the_line_instead(self):
        """`pythonw` has no stdin at all, and a redirected one is not a person.
        Neither may end the preview in a traceback."""
        def none(_prompt):
            raise EOFError

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli._offer_to_keep(config_mod.Config(), {"size": "small"},
                               "dictate overlay --size small --keep", ask=none)
        self.assertIn("dictate overlay --size small --keep", out.getvalue())

    def test_running_on_the_built_in_defaults_says_to_make_a_config_first(self):
        with self.assertRaises(DictateError) as ctx:
            cli._save_changes(config_mod.Config(), {"size": "small"})
        self.assertIn("dictate init", ctx.exception.remedy)


def _toml(text: str) -> dict:
    import tomllib

    return tomllib.loads(text)


if __name__ == "__main__":
    unittest.main()
