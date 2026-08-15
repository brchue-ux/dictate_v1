"""Config loading and validation.

The point of these tests is that a typo in a config file produces a sentence the
product owner can act on, rather than a KeyError or - worse - a silently ignored
setting.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dictate import config as config_mod
from dictate.errors import ConfigError

EXAMPLE = Path(__file__).resolve().parent.parent / "config" / "dictate.example.toml"


class Defaults(unittest.TestCase):
    def test_no_file_gives_valid_defaults(self):
        cfg = config_mod.load(None)
        self.assertEqual(cfg.audio.sample_rate, 16000)
        self.assertEqual(cfg.captions.num_threads, 2)
        self.assertEqual(cfg.paste.method, "sendinput")
        self.assertFalse(cfg.whisper.flash_attn)

    def test_block_frames_is_derived_correctly(self):
        cfg = config_mod.load(None)
        self.assertEqual(cfg.block_frames, 512)      # 32 ms at 16 kHz

    def test_relative_paths_resolve_against_the_config_file(self):
        cfg = config_mod.from_mapping({}, source_dir=Path("/opt/dictate"))
        self.assertEqual(cfg.resolve("cleanup-rules.toml"),
                         Path("/opt/dictate/cleanup-rules.toml"))

    def test_absolute_paths_are_left_alone(self):
        cfg = config_mod.from_mapping({}, source_dir=Path("/opt/dictate"))
        self.assertEqual(cfg.resolve("/etc/x.toml"), Path("/etc/x.toml"))


class ShippedExample(unittest.TestCase):
    def test_the_example_config_actually_loads(self):
        cfg = config_mod.load(EXAMPLE)
        self.assertEqual(cfg.captions.num_threads, 2)
        self.assertEqual(cfg.hotkey.mode, "hold")
        self.assertEqual(cfg.source_path, EXAMPLE.resolve())

    def test_the_example_matches_the_built_in_defaults(self):
        """If they drift, someone editing the example silently changes behaviour
        for people who deleted a line."""
        example = config_mod.load(EXAMPLE)
        defaults = config_mod.load(None)
        for section in ("hotkey", "audio", "captions", "overlay", "cleanup",
                        "paste", "history", "logging"):
            with self.subTest(section=section):
                got = getattr(example, section)
                want = getattr(defaults, section)
                if section == "logging":
                    continue  # the example sets a log file on purpose
                self.assertEqual(got, want)

    def test_every_key_the_example_ships_still_exists(self):
        """His dictate.toml was written from this file. A key that disappears
        from a dataclass makes that file fail to load on startup, so keys are
        added and deprecated by ignoring - never deleted. Held for every
        section, not just [overlay]."""
        import dataclasses
        import re

        text = EXAMPLE.read_text(encoding="utf-8")
        sections = re.split(r"(?m)^\[(\w+)\]$", text)[1:]
        self.assertTrue(sections)
        for name, block in zip(sections[::2], sections[1::2]):
            with self.subTest(section=name):
                cls = config_mod._SECTIONS[name]
                known = {f.name for f in dataclasses.fields(cls)}
                keys = set(re.findall(r"^(\w+)\s*=", block, re.MULTILINE))
                self.assertEqual(keys - known, set())


class Typos(unittest.TestCase):
    def test_unknown_section(self):
        with self.assertRaises(ConfigError) as ctx:
            config_mod.from_mapping({"hotkeys": {}})
        self.assertIn("hotkeys", ctx.exception.message)
        self.assertIn("[hotkey]", ctx.exception.remedy)

    def test_unknown_key_lists_the_valid_ones(self):
        with self.assertRaises(ConfigError) as ctx:
            config_mod.from_mapping({"paste": {"methodd": "sendinput"}})
        self.assertIn("methodd", ctx.exception.message)
        self.assertIn("method", ctx.exception.remedy)

    def test_wrong_type(self):
        with self.assertRaises(ConfigError) as ctx:
            config_mod.from_mapping({"whisper": {"port": "8178"}})
        self.assertIn("should be int", ctx.exception.message)

    def test_bool_does_not_satisfy_an_int_field(self):
        with self.assertRaises(ConfigError):
            config_mod.from_mapping({"whisper": {"port": True}})

    def test_an_int_is_accepted_for_a_float_field(self):
        cfg = config_mod.from_mapping({"overlay": {"opacity": 1}})
        self.assertEqual(cfg.overlay.opacity, 1.0)

    def test_a_byte_order_mark_does_not_look_like_a_broken_config(self):
        """Notepad and Windows PowerShell both save UTF-8 with a byte order
        mark. Read as plain utf-8 that mark is a TOML syntax error on line 1,
        so a config that is perfectly fine reads as broken."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dictate.toml"
            path.write_bytes(b"\xef\xbb\xbf[hotkey]\ncombination = \"ctrl + alt + f9\"\n")
            cfg = config_mod.load(path)
            self.assertEqual(cfg.hotkey.combination, "ctrl + alt + f9")

    def test_malformed_toml_points_at_the_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dictate.toml"
            path.write_text("[hotkey]\ncombination = \n", encoding="utf-8")
            with self.assertRaises(ConfigError) as ctx:
                config_mod.load(path)
            self.assertIn("not valid TOML", ctx.exception.message)

    def test_missing_file_says_what_to_do(self):
        with self.assertRaises(ConfigError) as ctx:
            config_mod.load("/nonexistent/dictate.toml")
        self.assertIn("dictate init", ctx.exception.remedy)


class SettledDecisionsAreEnforced(unittest.TestCase):
    def test_caption_threads_above_the_ceiling_are_refused_with_the_reason(self):
        with self.assertRaises(ConfigError) as ctx:
            config_mod.from_mapping({"captions": {"num_threads": 8}})
        self.assertIn("worse", ctx.exception.message)
        self.assertIn("num_threads = 2", ctx.exception.remedy)

    def test_two_threads_is_fine(self):
        config_mod.from_mapping({"captions": {"num_threads": 2}})

    def test_sample_rate_cannot_be_changed(self):
        with self.assertRaises(ConfigError) as ctx:
            config_mod.from_mapping({"audio": {"sample_rate": 44100}})
        self.assertIn("16000", ctx.exception.message)

    def test_stereo_is_refused(self):
        with self.assertRaises(ConfigError):
            config_mod.from_mapping({"audio": {"channels": 2}})


class IdleRelease(unittest.TestCase):
    """[whisper] idle_release_minutes: how long the model sits in VRAM when
    nobody is dictating. 0 is the escape hatch back to the old behaviour."""

    def test_the_default_is_five_minutes(self):
        self.assertEqual(config_mod.load(None).whisper.idle_release_minutes, 5.0)

    def test_zero_is_allowed_and_means_never(self):
        cfg = config_mod.from_mapping({"whisper": {"idle_release_minutes": 0}})
        self.assertEqual(cfg.whisper.idle_release_minutes, 0.0)

    def test_a_whole_number_of_minutes_is_accepted(self):
        cfg = config_mod.from_mapping({"whisper": {"idle_release_minutes": 20}})
        self.assertEqual(cfg.whisper.idle_release_minutes, 20.0)

    def test_a_negative_value_is_refused(self):
        with self.assertRaises(ConfigError) as ctx:
            config_mod.from_mapping({"whisper": {"idle_release_minutes": -1}})
        self.assertIn("cannot be negative", ctx.exception.message)
        self.assertIn("0 never gives it back", ctx.exception.remedy)

    def test_a_value_that_would_unload_between_sentences_is_refused(self):
        with self.assertRaises(ConfigError) as ctx:
            config_mod.from_mapping({"whisper": {"idle_release_minutes": 0.05}})
        self.assertIn("between one sentence and the next", ctx.exception.message)
        self.assertIn("or 0 to", ctx.exception.remedy)

    def test_the_shipped_example_says_the_same_as_the_default(self):
        self.assertEqual(config_mod.load(EXAMPLE).whisper.idle_release_minutes,
                         config_mod.load(None).whisper.idle_release_minutes)


class Ranges(unittest.TestCase):
    def test_bad_paste_method(self):
        with self.assertRaises(ConfigError) as ctx:
            config_mod.from_mapping({"paste": {"method": "telepathy"}})
        self.assertIn("sendinput", ctx.exception.remedy)

    def test_deferred_restore_does_not_require_permission_to_raise_windows(self):
        cfg = config_mod.from_mapping({
            "paste": {"on_focus_change": "restore", "restore_focus": False},
        })
        self.assertEqual(cfg.paste.on_focus_change, "restore")
        self.assertFalse(cfg.paste.restore_focus)

    def test_bad_overlay_position_lists_the_options(self):
        with self.assertRaises(ConfigError) as ctx:
            config_mod.from_mapping({"overlay": {"position": "middle"}})
        self.assertIn("bottom-center", ctx.exception.remedy)

    def test_opacity_out_of_range(self):
        with self.assertRaises(ConfigError):
            config_mod.from_mapping({"overlay": {"opacity": 2.0}})

    def test_bad_port(self):
        with self.assertRaises(ConfigError):
            config_mod.from_mapping({"whisper": {"port": 99999}})

    def test_bad_hotkey_mode(self):
        with self.assertRaises(ConfigError):
            config_mod.from_mapping({"hotkey": {"mode": "sometimes"}})

    def test_empty_hotkey(self):
        with self.assertRaises(ConfigError):
            config_mod.from_mapping({"hotkey": {"combination": "  "}})

    def test_bad_log_level(self):
        with self.assertRaises(ConfigError):
            config_mod.from_mapping({"logging": {"level": "CHATTY"}})

    def test_extra_args_must_be_strings(self):
        with self.assertRaises(ConfigError):
            config_mod.from_mapping({"whisper": {"extra_args": [1, 2]}})
        config_mod.from_mapping({"whisper": {"extra_args": ["--x"]}})


if __name__ == "__main__":
    unittest.main()
