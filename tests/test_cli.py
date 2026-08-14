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
from unittest import mock

from dictate import cli
from dictate.errors import DictateError, PlatformUnsupportedError
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


class HistoryCommand(unittest.TestCase):
    """`dictate history` has to work when the app does not - a record of
    everything he has said must not be trapped behind a copy that will not
    start. So it runs on any platform, and so does deleting it."""

    def config_with_history(self, tmp: str) -> Path:
        path = Path(tmp) / "dictate.toml"
        path.write_text("[history]\nkeep = 3\n", encoding="utf-8")
        return path

    def test_it_says_where_the_file_is_and_how_to_delete_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.config_with_history(tmp)
            code, out, _ = run(["--config", str(cfg), "history"])
        self.assertEqual(code, 0)
        self.assertIn("history.txt", out)
        self.assertIn("dictate history --delete", out)

    def test_it_counts_what_is_in_there(self):
        from dictate import config as config_mod, history as history_mod

        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.config_with_history(tmp)
            store = history_mod.HistoryStore(
                history_mod.path_for(config_mod.load(cfg)), keep=3)
            store.record("One.")
            store.record("Two.")
            code, out, _ = run(["--config", str(cfg), "history"])
        self.assertEqual(code, 0)
        self.assertIn("2 of the last 3", out)

    def test_delete_removes_the_file_and_says_so(self):
        from dictate import config as config_mod, history as history_mod

        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.config_with_history(tmp)
            path = history_mod.path_for(config_mod.load(cfg))
            history_mod.HistoryStore(path).record("Something.")
            self.assertTrue(path.exists())

            code, out, _ = run(["--config", str(cfg), "history", "--delete"])
            self.assertEqual(code, 0)
            self.assertIn("deleted", out)
            self.assertFalse(path.exists())

            # And again, with nothing there: a message, not a failure.
            code, out, _ = run(["--config", str(cfg), "history", "--delete"])
            self.assertEqual(code, 0)
            self.assertIn("nothing to delete", out)

    def test_it_says_when_the_history_is_turned_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dictate.toml"
            path.write_text("[history]\nenabled = false\n", encoding="utf-8")
            code, out, _ = run(["--config", str(path), "history"])
        self.assertEqual(code, 0)
        self.assertIn("nothing new is being kept", out)


class HotkeyCommand(unittest.TestCase):
    """`dictate hotkey` is the typed half of the tray's own item, and it is what
    anyone without a notification area has. It writes a config file, so what is
    checked here is that it writes a config file that still loads."""

    def config(self, tmp: str) -> Path:
        path = Path(tmp) / "dictate.toml"
        path.write_bytes((REPO / "config" / "dictate.example.toml").read_bytes())
        return path

    def test_it_says_what_the_hotkey_is_and_what_is_on_offer(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, _ = run(["--config", str(self.config(tmp)), "hotkey"])
        self.assertEqual(code, 0)
        self.assertIn("Ctrl + Alt + Space", out)
        self.assertIn("dictate hotkey", out)

    def test_it_sets_one_and_the_config_still_loads(self):
        from dictate import config as config_mod

        with tempfile.TemporaryDirectory() as tmp:
            path = self.config(tmp)
            code, out, _ = run(["--config", str(path), "hotkey", "ctrl-shift-d"])
            self.assertEqual(code, 0)
            self.assertIn("Ctrl + Shift + D", out)
            self.assertEqual(config_mod.load(path).hotkey.combination,
                             "control + shift + d")

    def test_a_combination_that_cannot_work_is_refused_before_it_is_written(self):
        """A config naming a hotkey that does not work is a dictate that will
        not start, and the way out of it is the text editor he will not open."""
        with tempfile.TemporaryDirectory() as tmp:
            path = self.config(tmp)
            before = path.read_bytes()
            code, _, err = run(["--config", str(path), "hotkey", "ctrl + alt"])
        self.assertEqual(code, 2)
        self.assertNotIn("Traceback", err)
        self.assertIn("modifier", err)
        self.assertEqual(path.read_bytes() if path.exists() else before, before)

    def test_with_no_config_file_it_says_which_command_makes_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(cli.config_mod, "default_config_path",
                                   return_value=Path(tmp) / "nothing.toml"):
                code, _, err = run(["hotkey", "ctrl + alt + d"])
        self.assertEqual(code, 2)
        self.assertIn("dictate init", err)


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

    def test_the_window_the_tray_opens_is_held_open_until_it_is_read(self):
        """`--pause` is how the notification area runs an update. That window
        is the only place its report - and any failure - is ever shown, so it
        may not close on the last line."""
        self.assertTrue(cli.build_parser().parse_args(["update", "--pause"]).pause)
        self.assertFalse(cli.build_parser().parse_args(["update"]).pause)

    def test_the_wait_happens_after_a_failure_has_been_printed_not_instead(self):
        waited: list[str] = []

        def fail(_args):
            raise DictateError("it went wrong", "type something else")

        with mock.patch.object(cli, "cmd_update", fail), \
             mock.patch.object(cli, "wait_for_enter",
                               lambda: waited.append("waited")):
            code, _out, err = run(["update", "--pause"])
        self.assertEqual(code, 2)
        self.assertIn("it went wrong", err)
        self.assertEqual(waited, ["waited"])

    def test_a_window_with_no_keyboard_behind_it_is_not_a_traceback(self):
        """Stdin redirected, closed, or absent entirely means nobody is
        waiting - which is a reason to return, not to end a command that has
        already done its work with a stack trace."""
        for boom in (EOFError, OSError, RuntimeError):
            with self.subTest(boom=boom):
                def read(exc=boom):
                    raise exc("no stdin")

                with contextlib.redirect_stdout(io.StringIO()):
                    cli.wait_for_enter(read=read)


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
