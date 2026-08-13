"""Starting at logon, tested as far as a machine that never logs on can.

What is checked here: what the scheduled task actually says, because a wrong
`UserId` or a missing `ExecutionTimeLimit` registers perfectly and then misbehaves
months later; what `schtasks`' answers mean, including an answer we cannot read;
and the logon start's own behaviour - that it stands aside for a copy that is
already running, that it waits and tries again rather than dying at the first
failure, and that when it does give up it writes down why.

What is NOT checked here, and cannot be: that Windows Task Scheduler accepts the
task and fires it at logon. CI registers and removes the real task on a real
Windows machine, which covers acceptance; nothing anywhere covers the logon.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from dictate import autostart, config as config_mod
from dictate.errors import DictateError, PlatformUnsupportedError

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}

# What `schtasks /Query /TN dictate /FO LIST /V` prints, trimmed to the fields
# that are read. Both the shape and the field names come from schtasks' own
# documented output on an English Windows.
SCHTASKS_READY = """
Folder: \\
HostName:                             DESKTOP-DICTATE
TaskName:                             \\dictate
Next Run Time:                        N/A
Status:                               Ready
Logon Mode:                           Interactive only
Last Run Time:                        13/08/2026 09:14:02
Last Result:                          267009
Author:                               DESKTOP-DICTATE\\owner
Task To Run:                          C:\\Python312\\pythonw.exe -m dictate run --autostart
Start In:                             C:\\Users\\owner
Comment:                              Starts dictate in your desktop session
Scheduled Task State:                 Enabled
Run As User:                          owner
Schedule Type:                        At logon time
"""

SCHTASKS_DISABLED = SCHTASKS_READY.replace(
    "Scheduled Task State:                 Enabled",
    "Scheduled Task State:                 Disabled",
).replace("Status:                               Ready",
          "Status:                               Disabled")


def build_xml(**overrides) -> str:
    settings = {
        "user": "DESKTOP-DICTATE\\owner",
        "command": r"C:\Python312\pythonw.exe",
        "arguments": autostart.task_arguments(),
        "working_directory": r"C:\Users\owner",
        "delay_s": 30,
        "description": autostart.task_description(),
    }
    settings.update(overrides)
    return autostart.task_xml(**settings)


class TheTaskDefinition(unittest.TestCase):
    def test_it_is_well_formed_xml(self):
        ET.fromstring(build_xml())

    def test_it_starts_at_this_user_s_logon_and_not_anyone_else_s(self):
        root = ET.fromstring(build_xml(user="DOMAIN\\owner"))
        trigger = root.find(".//t:LogonTrigger", NS)
        self.assertIsNotNone(trigger)
        self.assertEqual(trigger.find("t:UserId", NS).text, "DOMAIN\\owner")
        self.assertEqual(trigger.find("t:Enabled", NS).text, "true")

    def test_it_waits_before_starting_and_the_wait_is_configurable(self):
        root = ET.fromstring(build_xml(delay_s=45))
        self.assertEqual(root.find(".//t:LogonTrigger/t:Delay", NS).text, "PT45S")
        # Zero means "do not wait", not "wait for zero seconds".
        root = ET.fromstring(build_xml(delay_s=0))
        self.assertIsNone(root.find(".//t:LogonTrigger/t:Delay", NS))

    def test_it_runs_in_his_own_session_without_administrator_rights(self):
        """The whole reason this is not a Windows service: a service runs in
        session 0, where the overlay cannot be seen and keystrokes reach
        nothing."""
        principal = ET.fromstring(build_xml()).find(".//t:Principal", NS)
        self.assertEqual(principal.find("t:LogonType", NS).text, "InteractiveToken")
        self.assertEqual(principal.find("t:RunLevel", NS).text, "LeastPrivilege")

    def test_windows_refuses_a_second_copy_too(self):
        settings = ET.fromstring(build_xml()).find(".//t:Settings", NS)
        self.assertEqual(settings.find("t:MultipleInstancesPolicy", NS).text, "IgnoreNew")

    def test_windows_will_not_end_it_after_three_days_or_on_battery(self):
        """The default execution time limit is three days, after which Windows
        stops the task and dictate is mysteriously dead."""
        settings = ET.fromstring(build_xml()).find(".//t:Settings", NS)
        self.assertEqual(settings.find("t:ExecutionTimeLimit", NS).text, "PT0S")
        self.assertEqual(settings.find("t:DisallowStartIfOnBatteries", NS).text, "false")
        self.assertEqual(settings.find("t:StopIfGoingOnBatteries", NS).text, "false")
        self.assertEqual(settings.find("t:Enabled", NS).text, "true")

    def test_it_runs_the_windowless_interpreter_so_nothing_flashes_up(self):
        exec_node = ET.fromstring(build_xml()).find(".//t:Exec", NS)
        self.assertTrue(exec_node.find("t:Command", NS).text.endswith("pythonw.exe"))
        self.assertEqual(exec_node.find("t:Arguments", NS).text,
                         "-m dictate run --autostart")
        self.assertEqual(exec_node.find("t:WorkingDirectory", NS).text, r"C:\Users\owner")

    def test_the_description_says_how_to_turn_it_off(self):
        root = ET.fromstring(build_xml())
        description = root.find(".//t:RegistrationInfo/t:Description", NS).text
        self.assertIn("autostart disable", description)

    def test_a_path_with_xml_in_it_does_not_break_the_task(self):
        root = ET.fromstring(build_xml(command=r"C:\R & D\pythonw.exe",
                                       user="A<B>\\o'wner"))
        self.assertEqual(root.find(".//t:Exec/t:Command", NS).text,
                         r"C:\R & D\pythonw.exe")


class TheCommandItRuns(unittest.TestCase):
    def test_the_ordinary_case_pins_no_config_path(self):
        self.assertEqual(autostart.task_arguments(), "-m dictate run --autostart")

    def test_a_config_that_was_named_is_pinned_and_quoted(self):
        args = autostart.task_arguments(r"C:\Users\o wner\dictate.toml")
        self.assertEqual(args,
                         '-m dictate --config "C:\\Users\\o wner\\dictate.toml" '
                         "run --autostart")

    def test_quoting_leaves_ordinary_arguments_alone(self):
        self.assertEqual(autostart.quote_argument("--autostart"), "--autostart")
        self.assertEqual(autostart.quote_argument("a b"), '"a b"')

    def test_the_windowless_interpreter_is_required_rather_than_swapped(self):
        """python.exe would work and would flash a console window at every
        logon, which is exactly what this feature is for."""
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "python.exe"
            exe.write_text("", encoding="utf-8")
            with self.assertRaises(DictateError) as ctx:
                autostart.windowless_python(str(exe))
            self.assertIn("pythonw.exe", ctx.exception.message)
            self.assertIn("winget", ctx.exception.remedy)

            (Path(tmp) / "pythonw.exe").write_text("", encoding="utf-8")
            self.assertEqual(autostart.windowless_python(str(exe)).name, "pythonw.exe")


class TheAccountItRunsAs(unittest.TestCase):
    def test_a_domain_account_is_written_the_way_windows_wants_it(self):
        self.assertEqual(
            autostart.interactive_user({"USERDOMAIN": "DESKTOP", "USERNAME": "owner"}),
            "DESKTOP\\owner")

    def test_no_domain_means_no_domain_rather_than_a_guess(self):
        self.assertEqual(autostart.interactive_user({"USERNAME": "owner"}), "owner")

    def test_no_user_at_all_is_refused_with_a_remedy(self):
        with self.assertRaises(DictateError) as ctx:
            autostart.interactive_user({})
        self.assertTrue(ctx.exception.remedy)


class ReadingWindowsAnswer(unittest.TestCase):
    def test_a_registered_task_is_read_back_in_full(self):
        status = autostart.parse_status(SCHTASKS_READY, registered=True)
        self.assertTrue(status.registered)
        self.assertTrue(status.enabled)
        self.assertFalse(status.unreadable)
        self.assertEqual(status.state, "Ready")
        self.assertEqual(status.last_run, "13/08/2026 09:14:02")
        self.assertEqual(status.last_result, 267009)
        self.assertIn("pythonw.exe", status.task_to_run)

    def test_a_disabled_task_is_not_reported_as_on(self):
        status = autostart.parse_status(SCHTASKS_DISABLED, registered=True)
        self.assertTrue(status.registered)
        self.assertFalse(status.enabled)

    def test_an_answer_we_cannot_read_is_said_to_be_unread(self):
        """schtasks translates its field names. On a Windows that is not in
        English this finds nothing, and claiming "not registered" then would be
        a lie - so it says so and keeps the raw text."""
        status = autostart.parse_status("Aufgabenname: \\dictate\n", registered=True)
        self.assertTrue(status.registered)
        self.assertTrue(status.unreadable)
        self.assertIn("dictate", status.raw)

    def test_the_result_codes_that_matter_are_translated(self):
        self.assertIn("running right now", autostart.describe_last_result(267009))
        self.assertIn("not run yet", autostart.describe_last_result(267011))
        self.assertIn("already running", autostart.describe_last_result(3))
        self.assertIn("normally", autostart.describe_last_result(0))
        self.assertEqual(autostart.describe_last_result(None), "not recorded")

    def test_an_unknown_code_is_shown_rather_than_invented(self):
        self.assertEqual(autostart.describe_last_result(4294901760), "4294901760")

    def test_a_signed_code_is_recognised(self):
        """0x80070002 - Windows hands it back signed in some places and
        unsigned in others, and it is the same failure either way."""
        self.assertIn("could not find", autostart.describe_last_result(2147942402))
        self.assertIn("could not find", autostart.describe_last_result(-2147024894))

    def test_a_value_with_colons_in_it_survives_parsing(self):
        fields = autostart.parse_query_fields(SCHTASKS_READY)
        self.assertEqual(fields["last run time"], "13/08/2026 09:14:02")

    @unittest.skipIf(sys.platform == "win32", "schtasks exists on Windows")
    def test_a_missing_schtasks_is_a_message_not_a_traceback(self):
        with self.assertRaises(DictateError) as ctx:
            autostart.run_schtasks(["/Query"])
        self.assertIn("schtasks", ctx.exception.message)
        self.assertTrue(ctx.exception.remedy)


class TheLogonLog(unittest.TestCase):
    def test_it_stands_in_for_the_console_that_is_not_there(self):
        """Under pythonw.exe `sys.stdout` is None and a bare print() raises. The
        log has to be those streams as well as itself."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "autostart.log"
            before = sys.stdout
            with autostart.LogonLog(path) as log:
                log.start_block()
                log.write("hello")
                print("something printed straight out")
                self.assertIsNot(sys.stdout, before)
            self.assertIs(sys.stdout, before)
            text = path.read_text(encoding="utf-8")
        self.assertIn("hello", text)
        self.assertIn("something printed straight out", text)
        self.assertIn(autostart.BLOCK_MARK, text)

    def test_only_the_last_start_is_shown(self):
        text = (f"{autostart.BLOCK_MARK} 2026-08-01 ===\nold and irrelevant\n"
                f"{autostart.BLOCK_MARK} 2026-08-13 ===\nwhat happened this morning\n")
        block = autostart.last_block(text)
        self.assertIn("this morning", block)
        self.assertNotIn("irrelevant", block)

    def test_a_log_with_no_blocks_in_it_is_shown_whole(self):
        self.assertEqual(autostart.last_block("just some text"), "just some text")

    def test_it_does_not_grow_without_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "autostart.log"
            path.write_text("x" * (autostart.LOG_MAX_BYTES + 1), encoding="utf-8")
            with autostart.LogonLog(path) as log:
                log.write("after the roll")
            self.assertTrue(path.with_suffix(".log.1").exists())
            self.assertLess(path.stat().st_size, 1000)

    def test_a_long_block_is_trimmed_rather_than_dumped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "autostart.log"
            path.write_text(f"{autostart.BLOCK_MARK} now ===\n"
                            + "\n".join(str(i) for i in range(200)), encoding="utf-8")
            shown = autostart.read_last_block(path=path, max_lines=10)
        self.assertIn("not shown", shown)
        self.assertLessEqual(len(shown.splitlines()), 10)
        # The header says when it happened and the end says what went wrong.
        # Both survive; the middle is what goes.
        self.assertTrue(shown.startswith(autostart.BLOCK_MARK))
        self.assertTrue(shown.endswith("199"))


class TempState(unittest.TestCase):
    def setUp(self):
        # ignore_cleanup_errors: on Windows a child process that has just
        # been killed can still have the lock file open for a moment, and a
        # temporary directory that outlives a test is not a test failure.
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.dir = Path(self._tmp.name)
        self._previous = os.environ.get("DICTATE_STATE_DIR")
        os.environ["DICTATE_STATE_DIR"] = str(self.dir)
        self.addCleanup(self._restore)

    def _restore(self):
        if self._previous is None:
            os.environ.pop("DICTATE_STATE_DIR", None)
        else:
            os.environ["DICTATE_STATE_DIR"] = self._previous
        self._tmp.cleanup()

    def write_config(self, body: str) -> Path:
        path = self.dir / "dictate.toml"
        path.write_text(body, encoding="utf-8")
        return path


class TheLogonStart(TempState):
    """`dictate run --autostart` - the entry point the task calls."""

    def setUp(self):
        super().setUp()
        # A logon start configures logging, and while it is running stderr IS
        # the logon log - which is right there and wrong here, where the file
        # goes away at the end of the test. Put the root logger back afterwards.
        import logging

        self.addCleanup(logging.getLogger().handlers.clear)

    def test_it_stands_aside_for_a_copy_that_is_already_running(self):
        from dictate import instance

        with instance.InstanceLock(started_by="hand"):
            code = autostart.run_at_logon()
        self.assertEqual(code, 0)
        text = autostart.log_path().read_text(encoding="utf-8")
        self.assertIn("already running", text)
        self.assertIn("nothing to do", text)

    def test_it_tries_again_before_giving_up_and_writes_down_why(self):
        """At logon the graphics driver may still be loading and a drive may not
        be mounted. Here the failure is permanent - this is not Windows, so the
        platform check fails - which is what makes the give-up path testable."""
        path = self.write_config(
            "[autostart]\nstartup_attempts = 3\nretry_delay_s = 1.0\n"
            "notify_on_failure = false\n"
        )
        code = autostart.run_at_logon(str(path))
        self.assertEqual(code, 2)
        text = autostart.log_path().read_text(encoding="utf-8")
        self.assertIn("attempt 1 of 3", text)
        self.assertIn("attempt 3 of 3", text)
        self.assertIn("gave up after 3 attempt(s)", text)
        self.assertIn("dictate is NOT", text)
        # And the reason itself, not merely that there was one. The Whisper
        # model is missing on any machine these tests run on, Windows included -
        # unlike the platform check, which only fails off Windows.
        self.assertIn("cannot start yet", text)
        self.assertIn("Whisper model", text)

    def test_a_broken_config_is_not_retried_forever(self):
        """A config file that will not parse is not going to fix itself while we
        wait, so it is reported at once rather than after every attempt."""
        path = self.write_config("[captions]\nnum_threads = 99\n")
        code = autostart.run_at_logon(str(path))
        self.assertEqual(code, 2)
        text = autostart.log_path().read_text(encoding="utf-8")
        self.assertIn("attempt 1 of", text)
        self.assertNotIn("attempt 2 of", text)
        self.assertIn("num_threads = 2", text)  # the remedy, not just the fault
        self.assertIn("gave up", text)

    def test_the_failure_is_still_there_afterwards(self):
        """The whole point: a logon failure must not vanish with a window that
        closed. `autostart status` is where he reads it back."""
        path = self.write_config(
            "[autostart]\nstartup_attempts = 1\nnotify_on_failure = false\n")
        autostart.run_at_logon(str(path))
        shown = autostart.read_last_block()
        self.assertIn("gave up", shown)
        self.assertIn(autostart.BLOCK_MARK, shown)

    def test_an_unexpected_fault_is_written_down_rather_than_lost(self):
        """The one failure that really could vanish: under pythonw.exe there is
        no stderr for Python to print a traceback to, so a bug nobody
        anticipated would leave dictate simply not running, with nothing
        anywhere to say why."""
        from unittest import mock

        path = self.write_config(
            "[autostart]\nstartup_attempts = 1\nnotify_on_failure = false\n")
        with mock.patch.object(autostart, "_attempt",
                               side_effect=ZeroDivisionError("nobody saw this coming")):
            code = autostart.run_at_logon(str(path))
        self.assertEqual(code, 2)
        text = autostart.log_path().read_text(encoding="utf-8")
        self.assertIn("did not expect", text)
        self.assertIn("ZeroDivisionError", text)
        self.assertIn("nobody saw this coming", text)
        self.assertIn("Traceback", text)  # the technical detail, in the log
        self.assertIn("gave up", text)

    def test_it_releases_the_lock_when_it_gives_up(self):
        from dictate import instance

        path = self.write_config(
            "[autostart]\nstartup_attempts = 1\nnotify_on_failure = false\n")
        autostart.run_at_logon(str(path))
        self.assertIsNone(instance.running_instance())


class TheStatusReport(TempState):
    def test_it_answers_all_three_questions(self):
        lines = "\n".join(autostart.status_lines())
        self.assertIn("start at logon:", lines)
        self.assertIn("running now:     NO", lines)
        self.assertIn(str(autostart.log_path()), lines)

    @unittest.skipIf(sys.platform == "win32", "this is the non-Windows report")
    def test_off_windows_it_says_so_rather_than_claiming_it_is_off(self):
        lines = "\n".join(autostart.status_lines())
        self.assertIn("not available on", lines)
        self.assertNotIn("start at logon:  OFF", lines)

    def test_it_names_the_copy_that_is_running(self):
        from dictate import instance

        with instance.InstanceLock(started_by="logon"):
            lines = "\n".join(autostart.status_lines())
        self.assertIn("running now:     YES", lines)
        self.assertIn("dictate stop", lines)
        self.assertIn(str(os.getpid()), lines)


class TheCommands(TempState):
    """The command surface, through `cli.main`, the way he would type it."""

    def test_autostart_on_its_own_reports_rather_than_changing_anything(self):
        code, out = run_cli(["autostart"])
        self.assertEqual(code, 0)
        self.assertIn("running now:", out)
        self.assertNotIn("Traceback", out)

    def test_status_is_the_same_report(self):
        code, out = run_cli(["autostart", "status"])
        self.assertEqual(code, 0)
        self.assertIn("start at logon:", out)

    @unittest.skipIf(sys.platform == "win32", "these are the non-Windows refusals")
    def test_enable_and_disable_refuse_off_windows_with_a_message(self):
        for command in (["autostart", "enable"], ["autostart", "disable"]):
            with self.subTest(command=command):
                code, out = run_cli(command)
                self.assertEqual(code, 2)
                self.assertIn("Windows", out)
                self.assertNotIn("Traceback", out)

    @unittest.skipIf(sys.platform == "win32", "these are the non-Windows refusals")
    def test_the_refusal_is_the_documented_platform_error(self):
        with self.assertRaises(PlatformUnsupportedError):
            autostart.enable(config_mod.load(None))
        with self.assertRaises(PlatformUnsupportedError):
            autostart.disable()


class TheConfigSection(unittest.TestCase):
    def test_the_defaults_are_the_ones_the_task_is_built_from(self):
        cfg = config_mod.load(None)
        self.assertEqual(cfg.autostart.logon_delay_s, 30)
        self.assertEqual(cfg.autostart.startup_attempts, 5)
        self.assertTrue(cfg.autostart.notify_on_failure)

    def test_a_retry_count_that_never_reports_is_refused(self):
        with self.assertRaises(Exception) as ctx:
            config_mod.from_mapping({"autostart": {"startup_attempts": 0}})
        self.assertIn("bounded", ctx.exception.remedy)

    def test_an_impossible_delay_is_refused_with_a_reason(self):
        with self.assertRaises(Exception) as ctx:
            config_mod.from_mapping({"autostart": {"logon_delay_s": 10_000}})
        self.assertIn("logon_delay_s", ctx.exception.message)

    def test_a_typo_in_the_section_is_an_error_like_everywhere_else(self):
        with self.assertRaises(Exception) as ctx:
            config_mod.from_mapping({"autostart": {"logon_delay": 5}})
        self.assertIn("Unknown setting", ctx.exception.message)


def run_cli(argv: list[str]) -> tuple[int, str]:
    from dictate import cli

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue() + err.getvalue()


if __name__ == "__main__":
    unittest.main()
