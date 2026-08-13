"""The commands that have to work when the app will not start.

`doctor`, `init` and `clean` are the ones the product owner reaches for when
something is wrong, so they must run on any platform and must never end in a
traceback. That is what these tests hold in place.
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

from dictate import cli
from dictate.errors import PlatformUnsupportedError
from dictate.platform import factory

REPO = Path(__file__).resolve().parent.parent


def run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class CleanCommand(unittest.TestCase):
    def test_cleans_text_from_the_command_line(self):
        code, out, _ = run(["clean", "--rules", str(REPO / "config" / "cleanup-rules.toml"),
                            "Um, so we should, you know, ship it."])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "So we should ship it.")

    def test_explain_names_the_rules_that_fired(self):
        code, _, err = run(["clean", "--rules", str(REPO / "config" / "cleanup-rules.toml"),
                            "--explain", "Um, hello."])
        self.assertEqual(code, 0)
        self.assertIn("filler words", err)

    def test_a_broken_rules_file_gives_a_message_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "rules.toml"
            bad.write_text("nonsense [[[", encoding="utf-8")
            code, _, err = run(["clean", "--rules", str(bad), "hello"])
        self.assertEqual(code, 2)
        self.assertIn("not valid TOML", err)
        self.assertNotIn("Traceback", err)

    def test_a_missing_rules_file_gives_a_message_not_a_traceback(self):
        code, _, err = run(["clean", "--rules", "/nonexistent/rules.toml", "hi"])
        self.assertEqual(code, 2)
        self.assertIn("not found", err)


class DoctorCommand(unittest.TestCase):
    def test_doctor_runs_and_reports_on_any_platform(self):
        code, out, _ = run(["--config", str(REPO / "config" / "dictate.example.toml"),
                            "doctor"])
        self.assertIn("checking your setup", out)
        self.assertIn("Hotkey", out)
        self.assertIn("What to do:", out)   # the example points at C:\ paths
        self.assertIsInstance(code, int)
        self.assertNotIn("Traceback", out)

    def test_doctor_on_a_broken_config_explains_rather_than_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "dictate.toml"
            bad.write_text('[captions]\nnum_threads = 12\n', encoding="utf-8")
            code, _, err = run(["--config", str(bad), "doctor"])
        self.assertEqual(code, 2)
        self.assertIn("num_threads = 2", err)
        self.assertNotIn("Traceback", err)


class InitCommand(unittest.TestCase):
    def test_writes_both_files_and_they_load(self):
        from dictate import config as config_mod
        from dictate.cleanup import rules as rules_mod

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "dictate.toml"
            code, out, _ = run(["init", str(target)])
            self.assertEqual(code, 0)
            self.assertIn("wrote", out)
            self.assertTrue(target.exists())
            self.assertTrue((target.parent / "cleanup-rules.toml").exists())
            self.assertTrue((target.parent / "voice-punctuation.toml").exists())

            cfg = config_mod.load(target)
            rules_mod.load(cfg.resolve(cfg.cleanup.rules_file))

    def test_does_not_clobber_an_existing_file_without_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "dictate.toml"
            target.write_text("# mine\n", encoding="utf-8")
            code, out, _ = run(["init", str(target)])
            self.assertEqual(code, 0)
            self.assertIn("kept", out)
            self.assertEqual(target.read_text(encoding="utf-8"), "# mine\n")

            run(["init", str(target), "--force"])
            self.assertIn("hotkey", target.read_text(encoding="utf-8"))


class Parser(unittest.TestCase):
    def test_no_arguments_prints_help_and_succeeds(self):
        code, out, _ = run([])
        self.assertEqual(code, 0)
        self.assertIn("usage:", out)

    def test_version(self):
        with self.assertRaises(SystemExit) as ctx:
            run(["--version"])
        self.assertEqual(ctx.exception.code, 0)

    def test_update_is_offered_with_the_ways_out_of_it(self):
        """`--check` and `--restore` are the two he will want when he is not
        sure: one changes nothing, the other undoes the last one."""
        args = cli.build_parser().parse_args(["update", "--check"])
        self.assertTrue(args.check)
        self.assertFalse(args.force)
        self.assertEqual(args.branch, "main")
        self.assertTrue(cli.build_parser().parse_args(["update", "--restore"]).restore)


class PlatformSeam(unittest.TestCase):
    """No fallbacks: on a machine without the platform layer, every constructor
    refuses out loud instead of returning something that pretends to work."""

    @unittest.skipIf(sys.platform == "win32", "these are the non-Windows refusals")
    def test_every_platform_constructor_refuses_off_windows(self):
        from dictate import config as config_mod

        cfg = config_mod.load(None)
        cases = [
            ("window tracker", lambda: factory.make_window_tracker()),
            ("injector", lambda: factory.make_injector(cfg, None)),
            ("overlay", lambda: factory.make_overlay(cfg)),
            ("audio", lambda: factory.make_audio_capture(cfg)),
            ("hotkey", lambda: factory.make_hotkey_listener(cfg)),
            # A guard that guarded nothing, or a rescue that ended nothing while
            # reporting success, would be the same lie as a no-op overlay.
            ("child guard", lambda: factory.make_child_guard()),
            ("process tools", lambda: factory.make_process_tools()),
            ("tray icon", lambda: factory.make_tray_icon(None, icon_dir=".")),
        ]
        for name, build in cases:
            with self.subTest(component=name):
                with self.assertRaises(PlatformUnsupportedError) as ctx:
                    build()
                self.assertIn("Windows", ctx.exception.message)
                self.assertTrue(ctx.exception.remedy)

    def test_the_package_ships_no_stand_in_implementations(self):
        """Guard for the build brief's rule against faking anything into the
        product: `src/` must contain no test doubles."""
        banned = ("class Fake", "class Dummy", "class Mock", "class Null")
        offenders = []
        for path in (REPO / "src").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for token in banned:
                if token in text:
                    offenders.append(f"{path.relative_to(REPO)}: {token}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
