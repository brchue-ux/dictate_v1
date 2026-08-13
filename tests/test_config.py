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
                        "paste", "logging"):
            with self.subTest(section=section):
                got = getattr(example, section)
                want = getattr(defaults, section)
                if section == "logging":
                    continue  # the example sets a log file on purpose
                self.assertEqual(got, want)


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


class Ranges(unittest.TestCase):
    def test_bad_paste_method(self):
        with self.assertRaises(ConfigError) as ctx:
            config_mod.from_mapping({"paste": {"method": "telepathy"}})
        self.assertIn("sendinput", ctx.exception.remedy)

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
