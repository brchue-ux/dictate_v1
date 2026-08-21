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
import subprocess
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
        self.assertIn("not run yet", autostart.describe_last_result(267011))
        self.assertIn("already running", autostart.describe_last_result(3))
        self.assertIn("normally", autostart.describe_last_result(0))
        self.assertEqual(autostart.describe_last_result(None), "not recorded")

    def test_still_running_is_about_the_task_and_never_about_dictate(self):
        """267009 is SCHED_S_TASK_RUNNING, and it used to be translated as "it
        is running right now". Printed above a line saying the copy in front of
        him had been started by hand, that read as dictate having started
        itself when it had not. It is a statement about Task Scheduler's run,
        and it may only be written as one."""
        text = autostart.describe_last_result(autostart.STILL_RUNNING)
        self.assertIn("Task Scheduler", text)
        self.assertNotIn("running right now", text)
        # And "dictate" is what it must not be about.
        self.assertNotIn("dictate", text.lower())

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

    def test_the_block_before_the_last_one_is_the_bootstrap(self):
        """`__main__.py` writes its own block, with its own pid line, moments
        before `run_at_logon` writes the block `last_block` actually shows -
        so the breadcrumb one block back, for the SAME pid, is what a run that
        reached `run_at_logon` buried."""
        text = (f"{autostart.BLOCK_MARK} entry 2026-08-18 ===\n"
                f"{autostart.PID_MARK}8804)\n"
                "interpreter: C:\\Python311\\pythonw.exe\n"
                "working directory: C:\\Users\\bchue\n"
                f"{autostart.BLOCK_MARK} 2026-08-18 ===\n"
                f"{autostart.PID_MARK}8804)\n"
                "attempt 1 of 1\n")
        found = autostart.bootstrap_block(text, 8804)
        self.assertIn("interpreter:", found)
        self.assertIn("working directory:", found)
        self.assertNotIn("attempt 1 of 1", found)

    def test_a_bootstrap_block_for_a_different_pid_is_not_shown(self):
        """Windows reuses process numbers; a bootstrap block that names a
        different pid is somebody else's run, not evidence for this one."""
        text = (f"{autostart.BLOCK_MARK} entry 2026-08-18 ===\n"
                f"{autostart.PID_MARK}1111)\n"
                "interpreter: C:\\Python311\\pythonw.exe\n"
                f"{autostart.BLOCK_MARK} 2026-08-18 ===\n"
                f"{autostart.PID_MARK}8804)\n"
                "attempt 1 of 1\n")
        self.assertEqual(autostart.bootstrap_block(text, 8804), "")

    def test_no_pid_or_only_one_block_yields_nothing(self):
        text = f"{autostart.BLOCK_MARK} entry 2026-08-18 ===\ninterpreter: x\n"
        self.assertEqual(autostart.bootstrap_block(text, None), "")
        self.assertEqual(autostart.bootstrap_block(text, 8804), "")
        self.assertEqual(autostart.bootstrap_block("no blocks here", 8804), "")

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


class TheConsolelessPackageEntry(unittest.TestCase):
    """The real boundary the logon task crosses: ``pythonw -m dictate``.

    ``run_at_logon`` already records everything after the CLI dispatch. These
    tests hold the earlier boundary, where importing the CLI used to happen
    before any durable stream existed.
    """

    def test_its_minimal_path_and_markers_match_the_regular_log(self):
        from unittest import mock

        from dictate import __main__ as package_entry
        from dictate import instance

        environments = (
            {"DICTATE_STATE_DIR": r"C:\state"},
            {"LOCALAPPDATA": r"C:\Users\owner\AppData\Local"},
            {"APPDATA": r"C:\Users\owner\AppData\Roaming"},
            {"XDG_STATE_HOME": "/var/tmp/owner-state"},
        )
        for env in environments:
            with self.subTest(env=env), mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(package_entry._state_dir(),  # noqa: SLF001
                                 instance.state_dir())
        self.assertEqual(package_entry._LOG_NAME, autostart.LOG_NAME)  # noqa: SLF001
        self.assertEqual(package_entry._BLOCK_MARK, autostart.BLOCK_MARK)  # noqa: SLF001
        self.assertEqual(package_entry._PID_MARK, autostart.PID_MARK)  # noqa: SLF001

    def test_it_opens_the_log_before_dispatch_with_no_standard_streams(self):
        from dictate import __main__ as package_entry

        with tempfile.TemporaryDirectory() as tmp:
            previous = os.environ.get("DICTATE_STATE_DIR")
            os.environ["DICTATE_STATE_DIR"] = tmp
            saved = sys.stdout, sys.stderr
            seen = []

            def dispatch(argv):
                seen.append((argv, sys.stdout is not None,
                             sys.stderr is sys.stdout))
                print("the first imported code wrote this")
                return 0

            try:
                sys.stdout = None
                sys.stderr = None
                code = package_entry._windowless_main(  # noqa: SLF001
                    ["run", "--autostart"], dispatch=dispatch)
            finally:
                sys.stdout, sys.stderr = saved
                if previous is None:
                    os.environ.pop("DICTATE_STATE_DIR", None)
                else:
                    os.environ["DICTATE_STATE_DIR"] = previous

            text = (Path(tmp) / "autostart.log").read_text(encoding="utf-8")

        self.assertEqual(code, 0)
        self.assertEqual(seen, [(["run", "--autostart"], True, True)])
        self.assertIn("console-less package entry reached", text)
        self.assertIn("the first imported code wrote this", text)
        self.assertIn(autostart.BLOCK_MARK, text)
        self.assertIn(autostart.PID_MARK, text)

    def test_a_fault_before_run_at_logon_is_recorded_and_fails(self):
        from dictate import __main__ as package_entry

        with tempfile.TemporaryDirectory() as tmp:
            previous = os.environ.get("DICTATE_STATE_DIR")
            os.environ["DICTATE_STATE_DIR"] = tmp
            try:
                code = package_entry._windowless_main(  # noqa: SLF001
                    ["run", "--autostart"],
                    dispatch=lambda _argv: 1 / 0)
            finally:
                if previous is None:
                    os.environ.pop("DICTATE_STATE_DIR", None)
                else:
                    os.environ["DICTATE_STATE_DIR"] = previous
            text = (Path(tmp) / "autostart.log").read_text(encoding="utf-8")

        self.assertEqual(code, 1)
        self.assertIn("failed before the logon start could report", text)
        self.assertIn("ZeroDivisionError", text)
        self.assertIn("Traceback", text)

    def test_the_whole_module_path_runs_without_a_console(self):
        """The closest portable equivalent of ``pythonw -m dictate``.

        Windows CI runs the actual executable. Everywhere else this starts a
        fresh interpreter, removes both streams before package execution, and
        uses the exact module arguments the task uses. A known doctor refusal
        keeps the child bounded while exercising CLI import, parsing,
        ``run_at_logon`` and both layers of the durable log.
        """
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            cfg = state / "dictate.toml"
            cfg.write_text(
                "[autostart]\nstartup_attempts = 1\n"
                "notify_on_failure = false\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["DICTATE_STATE_DIR"] = str(state)
            source = str(Path(__file__).resolve().parent.parent / "src")
            env["PYTHONPATH"] = (
                source + os.pathsep + env["PYTHONPATH"]
                if env.get("PYTHONPATH") else source)
            script = (
                "import runpy, sys; "
                "sys.stdout = None; sys.stderr = None; "
                "runpy.run_module('dictate', run_name='__main__')"
            )
            proc = subprocess.run(
                [sys.executable, "-c", script, "--config", str(cfg),
                 "run", "--autostart"],
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                check=False, timeout=15,
            )
            text = (state / "autostart.log").read_text(encoding="utf-8")

        self.assertEqual(proc.returncode, 2)
        self.assertIn("console-less package entry reached", text)
        self.assertIn("attempt 1 of 1", text)
        self.assertIn("gave up after 1 attempt(s)", text)


class TheScheduledLaunchShape(unittest.TestCase):
    """`pythonw.exe -m dictate --autostart`, as Task Scheduler actually starts
    it - console-less (covered above already), from `%USERPROFILE%` rather
    than wherever dictate happens to be checked out, and with none of a
    developer's shell environment inherited. That specific combination had
    never been run together before: every existing consoleless test reused
    `os.environ.copy()` and the parent's own working directory. This is what
    would have caught a `Path.cwd()` or an assumed environment variable
    reaching the resolved state or config path.
    """

    def _run(self, *, cwd: Path, env: dict[str, str], config_body: str,
             timeout_s: float = 15) -> tuple[subprocess.CompletedProcess, str]:
        config = cwd / "dictate.toml"
        config.write_text(config_body, encoding="utf-8")
        source = str(Path(__file__).resolve().parent.parent / "src")
        env = dict(env)
        env["PYTHONPATH"] = (
            source + os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH")
            else source)
        script = (
            "import runpy, sys; "
            "sys.stdout = None; sys.stderr = None; "
            "runpy.run_module('dictate', run_name='__main__')"
        )
        proc = subprocess.run(
            [sys.executable, "-c", script, "--config", str(config),
             "run", "--autostart"],
            cwd=str(cwd), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False, timeout=timeout_s,
        )
        state = Path(env["DICTATE_STATE_DIR"])
        text = (state / "autostart.log").read_text(encoding="utf-8")
        return proc, text

    def test_a_different_working_directory_and_a_bare_environment(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as home, \
             tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as state:
            # The one thing Task Scheduler is documented to set differently:
            # `Start In` is his profile root, not dictate's install directory
            # or a checkout - nothing here is the state directory or the
            # source tree.
            cwd = Path(home)
            # No PYTHONHOME, no VIRTUAL_ENV, no LANG, no shell rc-file leftovers
            # - only what a bare process needs to start and to find dictate.
            # dictate's own state resolution goes through DICTATE_STATE_DIR,
            # which stands in for LOCALAPPDATA here exactly as it does for
            # the real Windows job in ci.yml.
            env = {"DICTATE_STATE_DIR": state}
            if sys.platform == "win32":
                for var in ("SystemRoot", "PATHEXT"):
                    if var in os.environ:
                        env[var] = os.environ[var]
            else:
                env["PATH"] = "/usr/bin:/bin"
            proc, text = self._run(
                cwd=cwd, env=env,
                config_body="[autostart]\nstartup_attempts = 1\n"
                            "notify_on_failure = false\n")

        self.assertEqual(proc.returncode, 2)
        self.assertIn("console-less package entry reached", text)
        self.assertIn(f"working directory: {cwd}", text)
        self.assertIn("attempt 1 of 1", text)
        self.assertIn("gave up after 1 attempt(s)", text)
        # The log itself was found under `state`, not under `cwd` or beside
        # the source tree - state resolution did not follow the process here.

    def test_the_bootstrap_breadcrumb_is_recoverable_from_that_real_log(self):
        """The breadcrumb `__main__.py` writes survives being buried under
        `run_at_logon`'s own block in a REAL two-block log, not just a
        hand-built string - `bootstrap_block` is exercised here against the
        exact file the scheduled-launch shape above produces."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as home, \
             tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as state:
            cwd = Path(home)
            env = {"DICTATE_STATE_DIR": state}
            if sys.platform != "win32":
                env["PATH"] = "/usr/bin:/bin"
            proc, text = self._run(
                cwd=cwd, env=env,
                config_body="[autostart]\nstartup_attempts = 1\n"
                            "notify_on_failure = false\n")

        self.assertEqual(proc.returncode, 2)
        pid = autostart.parse_logon_start(autostart.last_block(text)).pid
        self.assertIsNotNone(pid)
        found = autostart.bootstrap_block(text, pid)
        self.assertIn("console-less package entry reached", found)
        self.assertIn(f"working directory: {cwd}", found)
        # And it is genuinely a DIFFERENT block from the one `last_block`
        # returns - the whole point is that the two do not collapse into one.
        self.assertNotIn("attempt 1 of 1", found)


class LoggingWithoutAConsole(unittest.TestCase):
    def test_a_missing_stderr_installs_a_sink_not_a_broken_handler(self):
        import logging

        from dictate.logging_setup import configure

        root = logging.getLogger()
        saved_handlers, saved_level = list(root.handlers), root.level
        saved_stderr = sys.stderr
        try:
            sys.stderr = None
            configure(file="")
            self.assertEqual(len(root.handlers), 1)
            self.assertIsInstance(root.handlers[0], logging.NullHandler)
            logging.getLogger("dictate.consoleless-test").warning("safe")
        finally:
            sys.stderr = saved_stderr
            root.handlers[:] = saved_handlers
            root.setLevel(saved_level)


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

        # And nothing here may reach the real message box. On Windows that is
        # MessageBoxW, which blocks until somebody clicks it - on a CI machine,
        # forever. This is class-wide rather than per-test on purpose: a test
        # that fails to turn notification off in its config would otherwise hang
        # the suite rather than fail it, which is how this was found.
        from unittest import mock

        patcher = mock.patch.object(autostart, "_notify_failure")
        self.notified = patcher.start()
        self.addCleanup(patcher.stop)

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
        # He is still told, even though the file that could have turned the
        # message off is the very file that will not parse.
        self.assertTrue(self.notified.called)

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

    def test_the_lock_is_already_back_before_he_is_told(self):
        """The message box is modal: it waits for a click that may not come
        until he sits down the next morning. If the single-instance lock were
        still held behind it, `dictate run` would answer "dictate is already
        running" for a copy that gave up hours ago - which is the exact
        confusion the lock exists to prevent."""
        from dictate import instance

        held = []
        self.notified.side_effect = lambda *_: held.append(
            instance.running_instance() is not None)

        path = self.write_config("[autostart]\nstartup_attempts = 1\n")
        autostart.run_at_logon(str(path))

        self.assertEqual(held, [False], "the lock was still held while notifying")

    def test_the_wait_behind_the_message_box_is_written_down(self):
        """The state nothing recorded: it gave up, gave the lock back, and sat
        in a modal box - alive, not running, and counted by Task Scheduler as a
        running task the whole time. `dictate autostart status` had no way to
        tell that apart from a healthy start, so the log now says it, before
        the box goes up and again once it has gone."""
        from unittest import mock

        path = self.write_config("[autostart]\nstartup_attempts = 1\n")
        with mock.patch.object(autostart, "_will_notify", return_value=True):
            code = autostart.run_at_logon(str(path))
        self.assertEqual(code, 2)
        text = autostart.log_path().read_text(encoding="utf-8")
        self.assertIn(autostart.DIALOG_MARK, text)
        self.assertIn("NOT running", text)
        self.assertIn("the message box was closed", text)
        # And the report reads it back as the state it is.
        self.assertTrue(autostart.parse_logon_start(text).showing_dialog)

    def test_nothing_is_written_about_a_box_that_is_not_shown(self):
        """`notify_on_failure = false`, or any machine that is not Windows.
        The process ends here, so a log saying it is waiting on a dialog would
        be a claim about a state that never happened."""
        from unittest import mock

        path = self.write_config(
            "[autostart]\nstartup_attempts = 1\nnotify_on_failure = false\n")
        with mock.patch.object(autostart, "_will_notify", return_value=False):
            autostart.run_at_logon(str(path))
        text = autostart.log_path().read_text(encoding="utf-8")
        self.assertNotIn(autostart.DIALOG_MARK, text)

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

    def test_why_surfaces_the_bootstrap_block_the_plain_report_does_not(self):
        """`status` (40 lines) and `status --why` (200) both read only the last
        block by design - but only `--why` also looks one block back for the
        breadcrumb `__main__.py` wrote before `run_at_logon` buried it."""
        autostart.log_path().parent.mkdir(parents=True, exist_ok=True)
        autostart.log_path().write_text(
            f"{autostart.BLOCK_MARK} entry 2026-08-18 ===\n"
            f"{autostart.PID_MARK}8804)\n"
            "interpreter: C:\\Python311\\pythonw.exe\n"
            "working directory: C:\\Users\\bchue\n"
            f"{autostart.BLOCK_MARK} 2026-08-18 ===\n"
            f"{autostart.PID_MARK}8804)\n"
            "attempt 1 of 1\n"
            "outcome: gave up after 1 attempt(s).\n",
            encoding="utf-8")
        plain = "\n".join(autostart.status_lines(why=False))
        self.assertNotIn("interpreter:", plain)

        why = "\n".join(autostart.status_lines(why=True))
        self.assertIn("Before the CLI was even imported", why)
        self.assertIn("interpreter: C:\\Python311\\pythonw.exe", why)
        self.assertIn("working directory: C:\\Users\\bchue", why)


class WhereHeWillActuallyRead(TempState):
    """The three places that mention starting at logon while doing something
    else - the end of setup, the tray menu, and the startup banner.

    They exist because the feature was finished, correct and installed on his
    machine, and he asked for it anyway: the only way in was a typed command he
    had never been shown. Two of the three read `registered_or_unknown`, and
    that reads `is_registered`, so no two of them can answer differently.
    """

    def test_a_console_start_is_told_it_did_not_have_to_be_one(self):
        lines = autostart.console_hint(False)
        self.assertTrue(lines)
        self.assertIn("dictate autostart enable", "\n".join(lines))
        # Once and briefly. This is a banner, not a campaign.
        self.assertLessEqual(len(lines), 6)

    def test_a_copy_that_already_starts_at_logon_is_told_nothing(self):
        self.assertEqual(autostart.console_hint(True), [])

    def test_an_answer_nobody_could_read_is_not_turned_into_it_is_off(self):
        """The whole of the honesty here: `None` is not `False`."""
        self.assertEqual(autostart.console_hint(None), [])

    @unittest.skipIf(sys.platform == "win32", "this is the non-Windows answer")
    def test_off_windows_the_answer_is_unknown_rather_than_off(self):
        self.assertIsNone(autostart.registered_or_unknown())

    def test_schtasks_refusing_to_answer_is_unknown_rather_than_off(self):
        from unittest import mock

        def refuse():
            raise DictateError("schtasks could not be run", "…")

        with mock.patch.object(sys, "platform", "win32"), \
                mock.patch.object(autostart, "is_registered", refuse):
            self.assertIsNone(autostart.registered_or_unknown())


class FakeProcess:
    """What `spawn_detached` hands back: a process, not a pid."""

    def __init__(self, pid: int = 4242, exits: int | None = None):
        self.pid = pid
        self._exits = exits

    def poll(self):
        return self._exits


class SpawnRecorder:
    """Stands in for `subprocess.Popen` inside `recovery.spawn_detached`."""

    def __init__(self, process=None, raises: OSError | None = None):
        self.calls: list[tuple[list[str], dict]] = []
        self.process = process or FakeProcess()
        self.raises = raises

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), dict(kwargs)))
        if self.raises is not None:
            raise self.raises
        return self.process


class StartingItNow(TempState):
    """Turning it on starts it, and says truthfully what that did.

    This is the second time this feature failed him on discoverability alone:
    it shipped as a command nobody found, was surfaced in setup and on the tray
    for that reason, and then did nothing visible when he used either. Enabling
    something and watching nothing happen is the same defect in a new hat.
    """

    def test_a_copy_that_is_already_running_means_nothing_is_started(self):
        """The hazard the whole product is built around: one copy. Two would
        fight over the hotkey and over the transcription port, and the second
        whisper-server is the orphan class this project has already shipped
        once. The lock is real here - it works on this machine too."""
        from dictate import instance

        spawn = SpawnRecorder()
        with instance.InstanceLock(started_by="hand"):
            outcome = autostart.start_now(spawn=spawn)

        self.assertEqual(outcome.state, "already-running")
        self.assertEqual(spawn.calls, [])
        self.assertIn(str(os.getpid()), outcome.holder)
        said = "\n".join(autostart.start_lines(outcome))
        self.assertIn("already running", said)
        self.assertIn(str(os.getpid()), said)

    def test_the_tray_reaches_that_same_answer(self):
        """The tray menu is drawn by a running copy, so its own enable is
        always the case above. It is decided in `start_now` rather than at each
        call site precisely so that being reached from inside a running copy
        cannot start a second one."""
        from dictate import instance

        spawn = SpawnRecorder()
        with instance.InstanceLock(started_by="logon"):
            self.assertEqual(autostart.start_now(spawn=spawn).state,
                             "already-running")
        self.assertEqual(spawn.calls, [])

    def test_it_starts_the_same_windowless_copy_the_logon_task_starts(self):
        """Not a child of the shell or the tray that asked for it: `pythonw.exe`,
        `run --autostart` (which is the entry point that has a log in place of
        the stdout a windowless process does not have), and detached, so it
        outlives the window it was typed in."""
        spawn = SpawnRecorder()
        running = [None, FakeProcess()]  # not running, then running
        outcome = autostart.start_now(
            executable=r"C:\Python312\pythonw.exe",
            spawn=spawn, running=lambda: running.pop(0), sleep=lambda _s: None)

        self.assertEqual(outcome.state, "started")
        self.assertEqual(outcome.pid, 4242)
        (argv, kwargs), = spawn.calls
        self.assertEqual(argv[0], r"C:\Python312\pythonw.exe")
        self.assertEqual(argv[1:], ["-m", "dictate", "run", "--autostart"])
        # However it is spelled on this platform, it is spelled: a plain Popen
        # with no flags at all would be a child that dies with its parent.
        self.assertTrue(kwargs.get("start_new_session")
                        or kwargs.get("creationflags"))

    def test_the_config_it_was_given_is_carried_over(self):
        spawn = SpawnRecorder()
        running = [None, FakeProcess()]
        autostart.start_now(r"C:\Users\owner\dictate.toml", "pythonw.exe",
                            spawn=spawn, running=lambda: running.pop(0))
        (argv, _kwargs), = spawn.calls
        self.assertIn("--config", argv)
        self.assertIn(r"C:\Users\owner\dictate.toml", argv)

    def test_a_copy_that_stops_again_at_once_is_a_failure_with_its_own_reason(self):
        """`start_now` may not report a start it did not watch happen. A copy
        that came straight back is the case that would otherwise be printed as
        a cheerful success."""
        spawn = SpawnRecorder(process=FakeProcess(pid=99, exits=2))
        outcome = autostart.start_now(spawn=spawn, running=lambda: None,
                                      sleep=lambda _s: None)
        self.assertEqual(outcome.state, "failed")
        self.assertFalse(outcome.ok)
        # 2 is dictate's own "could not start and said why". There was no
        # console for it to say it in, so the log that has it is named.
        self.assertIn("exit code 2", outcome.detail)
        self.assertIn(str(autostart.log_path()), outcome.detail)
        said = "\n".join(autostart.start_lines(outcome))
        self.assertIn("could NOT be started now", said)
        self.assertIn("dictate run", said)

    def test_windows_refusing_to_start_it_is_reported_in_its_own_words(self):
        """Never "something went wrong": the thing that refused said why."""
        spawn = SpawnRecorder(raises=OSError("[WinError 5] Access is denied"))
        outcome = autostart.start_now(spawn=spawn, running=lambda: None)
        self.assertEqual(outcome.state, "failed")
        self.assertIn("Access is denied", outcome.detail)
        self.assertIn("Access is denied", "\n".join(autostart.start_lines(outcome)))

    def test_running_out_of_patience_is_not_reported_as_a_failure(self):
        """It has not been established that anything is wrong - only that
        dictate stopped watching. That is said, and where the answer lives is
        named, rather than either half being claimed."""
        waited: list[float] = []
        clock = iter([0.0, 1.0, 2.0, 99.0])
        outcome = autostart.start_now(
            spawn=SpawnRecorder(), running=lambda: None,
            sleep=waited.append, monotonic=lambda: next(clock), wait_s=5.0)

        self.assertEqual(outcome.state, "unconfirmed")
        self.assertFalse(outcome.ok)
        said = "\n".join(autostart.start_lines(outcome))
        self.assertNotIn("could NOT", said)
        self.assertIn("dictate autostart status", said)
        self.assertIn(str(autostart.log_path()), said)
        self.assertTrue(waited)

    def test_it_waits_rather_than_spinning(self):
        clock = iter([0.0, 0.1, 0.2, 0.3, 99.0])
        waited: list[float] = []
        autostart.start_now(spawn=SpawnRecorder(), running=lambda: None,
                            sleep=waited.append, monotonic=lambda: next(clock),
                            poll_s=0.25, wait_s=5.0)
        self.assertEqual(set(waited), {0.25})


class WhatEnableSaysItDid(TempState):
    """`enable` does two things now, so it reports two outcomes.

    A registration that worked and a start that did not is not a success, and
    this project has twice sent the product owner down a wrong path by printing
    an outcome nobody established. So the two are separate paragraphs, and the
    start's own paragraph is written from what `start_now` watched.
    """

    def _windows(self, outcome: autostart.StartOutcome):
        """Let `enable` run here: everything it does to Windows, stubbed with
        what Windows says when it works."""
        from unittest import mock

        patches = [
            mock.patch.object(autostart, "_require_windows", lambda _a: None),
            mock.patch.object(autostart, "interactive_user",
                              lambda *a, **k: "DESKTOP-DICTATE\\owner"),
            mock.patch.object(autostart, "windowless_python",
                              lambda *a, **k: Path(r"C:\Python312\pythonw.exe")),
            mock.patch.object(autostart, "run_schtasks",
                              lambda args: autostart.ToolResult(0, "SUCCESS")),
            mock.patch.object(autostart, "is_registered", lambda: True),
            mock.patch.object(autostart, "registered_command",
                              lambda: r"C:\Python312\pythonw.exe -m dictate run --autostart"),
            mock.patch.object(autostart, "start_now",
                              lambda *a, **k: (self.started.append((a, k)), outcome)[1]),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def setUp(self):
        super().setUp()
        self.started: list = []

    def test_it_registers_and_then_starts_it(self):
        self._windows(autostart.StartOutcome("started", pid=4242))
        lines = autostart.enable(config_mod.load(None),
                                 config_path=r"C:\Users\owner\dictate.toml")
        text = "\n".join(lines)

        self.assertIn("will now start when you log in", text)
        self.assertIn("running NOW", text)
        self.assertIn("4242", text)
        # And the copy it starts is the one the task would: same interpreter,
        # same config.
        (args, _kwargs), = self.started
        self.assertEqual(args[0], r"C:\Users\owner\dictate.toml")
        self.assertEqual(args[1], r"C:\Python312\pythonw.exe")

    def test_a_start_that_failed_is_never_printed_as_a_success(self):
        self._windows(autostart.StartOutcome("failed", detail="Access is denied"))
        text = "\n".join(autostart.enable(config_mod.load(None)))

        self.assertIn("will now start when you log in", text)   # this DID happen
        self.assertIn("could NOT be started now", text)         # and this did not
        self.assertIn("Access is denied", text)
        # The half that worked is still described as having worked, because it
        # did: the task is registered and the next logon will use it.
        self.assertIn("registered and unaffected", text)

    def test_it_says_when_there_was_already_one_running(self):
        self._windows(autostart.StartOutcome("already-running",
                                             holder="process 1234"))
        text = "\n".join(autostart.enable(config_mod.load(None)))
        self.assertIn("already running", text)
        self.assertIn("process 1234", text)
        self.assertNotIn("running NOW", text)

    def test_the_typed_command_is_the_same_two_paragraphs(self):
        """`dictate autostart enable` is what he types, what the tray runs and
        what setup runs, so it is the one that has to carry both outcomes."""
        self._windows(autostart.StartOutcome("started", pid=4242))
        code, out = run_cli(["autostart", "enable"])
        self.assertEqual(code, 0)
        self.assertIn("will now start when you log in", out)
        self.assertIn("running NOW", out)
        self.assertEqual(len(self.started), 1)

    def test_the_start_can_be_left_out_but_never_is_by_default(self):
        self._windows(autostart.StartOutcome("started", pid=1))
        autostart.enable(config_mod.load(None), start=False)
        self.assertEqual(self.started, [])
        autostart.enable(config_mod.load(None))
        self.assertEqual(len(self.started), 1)


class WhatDisableLeavesRunning(TempState):
    """Enabling starts a copy; disabling does NOT stop one.

    The decision, and the reason: he can be dictating into it at the moment he
    clicks the tray item, and stopping it there would drop the audio and the
    words to answer a question he did not ask. Nothing is lost by leaving it -
    `dictate stop` is one command away and is the one command every failure
    message in this product already names.
    """

    def _task_removed(self):
        """Windows had the task and let it go - the ordinary disable."""
        from unittest import mock

        answers = iter([True, False])
        for patch in (mock.patch.object(autostart, "_require_windows", lambda _a: None),
                      mock.patch.object(autostart, "run_schtasks",
                                        lambda args: autostart.ToolResult(0, "SUCCESS")),
                      mock.patch.object(autostart, "is_registered",
                                        lambda: next(answers))):
            patch.start()
            self.addCleanup(patch.stop)

    def test_a_running_copy_is_named_and_left_alone(self):
        from dictate import instance

        self._task_removed()
        with instance.InstanceLock(started_by="logon"):
            text = "\n".join(autostart.disable())
            # It is still holding the lock when disable has finished.
            self.assertIsNotNone(instance.running_instance())

        self.assertIn("still RUNNING", text)
        self.assertIn(str(os.getpid()), text)
        self.assertIn("dictate stop", text)

    def test_nothing_is_said_about_a_copy_that_is_not_there(self):
        self._task_removed()
        text = "\n".join(autostart.disable())
        self.assertNotIn("still RUNNING", text)
        self.assertIn("no longer start when you log in", text)


# ---------------------------------------------------------------------------
# The two readings, and the morning they disagreed
# ---------------------------------------------------------------------------

#: What Windows said on 2026-08-14, verbatim in shape: the task's run had not
#: finished. Beside it the lock said the copy serving him had been started by
#: hand, forty minutes later. Both readings were right and the report printed
#: them one after the other as though they were one story.
SCHTASKS_RUNNING = """
Folder: \\
TaskName:                             \\dictate
Status:                               Running
Last Run Time:                        2026-08-14 9:17:40 AM
Last Result:                          267009
Task To Run:                          C:\\Python311\\pythonw.exe -m dictate run --autostart
Scheduled Task State:                 Enabled
"""

SCHTASKS_FINISHED = SCHTASKS_RUNNING.replace(
    "Status:                               Running",
    "Status:                               Ready",
).replace("Last Result:                          267009",
          "Last Result:                          2")

#: A logon start that gave up, as the log records it.
GAVE_UP_BLOCK = f"""09:17:41  {autostart.BLOCK_MARK} 2026-08-14 09:17:41 ===
09:17:41  {autostart.PID_MARK}8804)
09:17:41  attempt 1 of 5
09:19:02  outcome: gave up after 5 attempt(s). dictate is NOT running. Start it \
by hand with `dictate run` once the problem above is fixed."""

HAND = "started by hand"


def a_holder(pid=22188, started_by="hand"):
    from dictate import instance

    return instance.Holder(pid=pid, started_at="2026-08-14 09:54:11",
                           started_epoch=1_000.0, started_by=started_by)


def a_reading(*, schtasks=SCHTASKS_RUNNING, block=GAVE_UP_BLOCK,
              alive=None, image="", holder=None, registered=True,
              evidence=None) -> autostart.Reading:
    logon = autostart.parse_logon_start(block)
    logon.alive = alive
    logon.image = image
    return autostart.Reading(
        platform="win32",
        status=autostart.parse_status(schtasks, registered=registered),
        logon=logon, holder=holder, block=block,
        log_file=r"C:\Users\owner\AppData\Local\dictate\autostart.log",
        evidence=evidence,
    )


class TheTwoReadingsAreCompared(unittest.TestCase):
    """`dictate autostart status` reads two independent things - what Windows
    says about the TASK, and what the instance lock says about the COPY THAT IS
    RUNNING - and it used to print them one after the other with nothing
    between. On 2026-08-14 that produced, in the same report:

        last result:   267009 (it is running right now)
        running now:   YES - process 22188, ..., started by hand

    Both readings were correct and neither sentence was wrong on its own.
    Together they told him dictate had started itself and that it had not, and
    the report did not notice. That is the same defect as the installer
    announcing a Vulkan SDK it had not installed - a claim nothing established -
    and it costs the same thing: the next message this product prints is
    believed a little less.
    """

    def test_his_morning_is_reported_as_the_disagreement_it_is(self):
        text = "\n".join(autostart.render(
            a_reading(alive=True, image="pythonw.exe", holder=a_holder())))
        self.assertIn(autostart.DISAGREE_MARK, text)
        # Both facts survive - the point is not to hide either one.
        self.assertIn("267009", text)
        self.assertIn(HAND, text)
        # And it says which process is which, rather than leaving him to work
        # it out from two numbers.
        self.assertIn("8804", text)
        self.assertIn("22188", text)
        self.assertIn("it is not what is serving you", text)
        self.assertIn(autostart.WHY_COMMAND, text)

    def test_no_report_may_claim_the_run_is_alive_and_say_nothing_of_the_hand(self):
        """The acceptance criterion, held over every combination rather than
        over the one that happened.

        Whenever the report says Windows has not seen the run finish AND says
        the copy in front of him was started by hand - or that there is no copy
        at all - it has printed two claims that cannot both be the whole truth.
        The disagreement paragraph is what makes that a report rather than a
        contradiction, so it has to be there. Delete `reconcile`, or put "it is
        running right now" back, and this fails.
        """
        holders = [None, a_holder(), a_holder(started_by="logon"),
                   a_holder(pid=8804), a_holder(pid=8804, started_by="logon"),
                   a_holder(started_by="")]
        for schtasks in (SCHTASKS_RUNNING, SCHTASKS_FINISHED):
            for alive in (True, False, None):
                for holder in holders:
                    for block in (GAVE_UP_BLOCK, "", "nothing readable here"):
                        with self.subTest(alive=alive, block=bool(block),
                                          holder=holder, running="Running" in schtasks):
                            text = "\n".join(autostart.render(a_reading(
                                schtasks=schtasks, block=block, alive=alive,
                                holder=holder)))
                            says_run_alive = (
                                "has not seen that run finish" in text
                                or "state:         Running" in text)
                            says_not_the_logon_copy = (
                                HAND in text or "running now:     NO" in text)
                            if says_run_alive and says_not_the_logon_copy:
                                self.assertIn(autostart.DISAGREE_MARK, text)

    def test_a_run_still_counted_with_nothing_running_is_not_reported_as_healthy(self):
        text = "\n".join(autostart.render(a_reading(alive=True, holder=None)))
        self.assertIn(autostart.DISAGREE_MARK, text)
        self.assertIn("DICTATE IS NOT RUNNING", text)
        self.assertIn(autostart.WHY_COMMAND, text)

    def test_a_run_counted_after_its_process_is_gone_names_the_leftover(self):
        """The other shape: Windows still counting a run whose process has
        ended means something ELSE that run started is alive. A whisper-server
        with no dictate above it is the one that has happened here before, and
        one command clears it."""
        verdict = autostart.reconcile(
            autostart.parse_status(SCHTASKS_RUNNING, registered=True),
            _logon(alive=False), None)
        self.assertEqual(verdict.verdict, "process-gone-run-alive")
        text = "\n".join(verdict.lines)
        self.assertIn(autostart.DISAGREE_MARK, text)
        self.assertIn("whisper-server", text)
        self.assertIn("dictate stop", text)

    def test_a_process_the_task_left_alive_is_named_even_once_the_run_is_counted_done(self):
        """Windows can stop counting the run while the process it started is
        still there - it is what a copy that broke out of the task's job looks
        like, and what the modal box looks like once Task Scheduler has given
        up on it. The process reading leads either way; only the first sentence
        changes."""
        verdict = autostart.reconcile(
            autostart.parse_status(SCHTASKS_FINISHED, registered=True),
            _logon(alive=True), a_holder())
        self.assertEqual(verdict.verdict, "not-the-copy")
        self.assertTrue(verdict.disagrees)
        alone = autostart.reconcile(
            autostart.parse_status(SCHTASKS_FINISHED, registered=True),
            _logon(alive=True), None)
        self.assertEqual(alone.verdict, "alive-but-serving-nobody")
        self.assertIn("no longer counts that run as running",
                      "\n".join(alone.lines))

    def test_the_copy_the_task_started_is_reported_as_agreement(self):
        verdict = autostart.reconcile(
            autostart.parse_status(SCHTASKS_RUNNING, registered=True),
            _logon(alive=True), a_holder(pid=8804, started_by="logon"))
        self.assertEqual(verdict.verdict, "same-copy")
        self.assertFalse(verdict.disagrees)
        self.assertIn("agree", "\n".join(verdict.lines))

    def test_an_ordinary_restart_from_the_tray_is_not_called_a_fault(self):
        """The tray's Restart leaves the pid in the log pointing at a process
        that has ended on purpose. `_restart` writes the new one down, so the
        report follows it rather than announcing a disagreement over a feature
        working exactly as designed."""
        block = GAVE_UP_BLOCK + f"\n09:30:00  {autostart.RESTART_MARK}9101)."
        logon = autostart.parse_logon_start(block)
        self.assertEqual(logon.pid, 9101)
        logon.alive = True
        verdict = autostart.reconcile(
            autostart.parse_status(SCHTASKS_RUNNING, registered=True),
            logon, a_holder(pid=9101, started_by="logon"))
        self.assertFalse(verdict.disagrees)

    def test_an_unknown_is_never_turned_into_either_verdict(self):
        """Nobody could ask Windows whether that pid is still there. That is
        not "it is fine" and it is not "it is gone" - it is an unknown, and it
        is printed as one with the command that settles it."""
        verdict = autostart.reconcile(
            autostart.parse_status(SCHTASKS_RUNNING, registered=True),
            _logon(alive=None), a_holder())
        self.assertEqual(verdict.verdict, "cannot-tell")
        text = "\n".join(verdict.lines)
        self.assertIn("dictate could not", text)
        self.assertIn("establish what that run still has alive", text)
        self.assertIn("not settled here", text)
        self.assertIn(autostart.WHY_COMMAND, text)

    def test_a_reused_process_number_is_not_taken_for_the_same_copy(self):
        """A pid is not an identity. Windows reuses them, and the number in the
        log belongs to a run that may have ended hours ago - so a lock record
        saying "started by hand" against the logon task's pid is a collision,
        and dictate says so rather than reporting a match it has not got."""
        verdict = autostart.reconcile(
            autostart.parse_status(SCHTASKS_RUNNING, registered=True),
            _logon(alive=True), a_holder(pid=8804, started_by="hand"))
        self.assertEqual(verdict.verdict, "pid-reused")
        self.assertTrue(verdict.disagrees)
        self.assertIn("reuses process numbers", "\n".join(verdict.lines))

    def test_windows_saying_nothing_readable_is_not_a_verdict_either(self):
        status = autostart.parse_status("Aufgabenname: \\dictate\n", registered=True)
        self.assertIsNone(autostart.run_is_alive(status))
        verdict = autostart.reconcile(status, _logon(alive=None), None)
        self.assertEqual(verdict.verdict, "run-unknown")
        self.assertFalse(verdict.disagrees)

    def test_a_task_that_is_not_registered_is_nothing_to_reconcile(self):
        verdict = autostart.reconcile(
            autostart.AutostartStatus(registered=False), _logon(), a_holder())
        self.assertEqual(verdict.lines, [])

    def test_the_state_word_decides_and_the_result_code_is_the_fallback(self):
        """schtasks TRANSLATES `Status:`, so a Windows that is not in English
        falls through to `Last Result`, which is a number everywhere. A machine
        where neither can be read answers `None`."""
        running = autostart.parse_status(SCHTASKS_RUNNING, registered=True)
        self.assertTrue(autostart.run_is_alive(running))
        self.assertFalse(autostart.run_is_alive(
            autostart.parse_status(SCHTASKS_FINISHED, registered=True)))
        # Status in German, Last Result in digits.
        german = autostart.parse_status(
            "Status:  Wird ausgeführt\nLast Result:  267009\n", registered=True)
        self.assertTrue(autostart.run_is_alive(german))


def _logon(*, pid=8804, alive=None, block=GAVE_UP_BLOCK):
    found = autostart.parse_logon_start(block)
    found.pid = pid
    found.alive = alive
    return found


class WhatTheLogonStartWroteDownAboutItself(TempState):
    """Windows says whether the RUN has finished. It will not say what that run
    still has alive - so the logon start writes its own pid into its log, and
    the report asks Windows about that pid by name. Without it, "the task is
    running" was standing in for "dictate is running"."""

    def test_the_pid_and_the_outcome_come_back_out_of_the_log(self):
        found = autostart.parse_logon_start(GAVE_UP_BLOCK)
        self.assertEqual(found.pid, 8804)
        self.assertEqual(found.at, "2026-08-14 09:17:41")
        self.assertIn("gave up after 5", found.outcome)
        self.assertFalse(found.showing_dialog)

    def test_a_log_with_nothing_in_it_yields_nothing_rather_than_a_guess(self):
        found = autostart.parse_logon_start("")
        self.assertIsNone(found.pid)
        self.assertIsNone(found.alive)

    def test_the_pid_survives_a_block_long_enough_to_be_trimmed(self):
        """The trim used to keep the first line only, which is the header - so
        a chatty start (whisper.cpp is chatty) lost the pid and made itself
        unanswerable. It keeps the first two now."""
        path = self.dir / "autostart.log"
        path.write_text(GAVE_UP_BLOCK + "\n"
                        + "\n".join(f"09:18:{i:02d}  noise {i}" for i in range(60)),
                        encoding="utf-8")
        shown = autostart.read_last_block(path=path, max_lines=10)
        self.assertIn("not shown", shown)
        self.assertEqual(autostart.parse_logon_start(shown).pid, 8804)

    def test_the_pid_is_asked_about_rather_than_assumed_alive(self):
        found = autostart.read_logon_start(block=GAVE_UP_BLOCK,
                                           ask=lambda _pid: "pythonw.exe")
        self.assertTrue(found.alive)
        self.assertEqual(found.image, "pythonw.exe")
        gone = autostart.read_logon_start(block=GAVE_UP_BLOCK, ask=lambda _pid: "")
        self.assertFalse(gone.alive)

    def test_nobody_able_to_ask_leaves_it_unknown(self):
        """Off Windows there is no tasklist. `None` is the answer, and the
        report says so instead of choosing one."""
        unknown = autostart.read_logon_start(block=GAVE_UP_BLOCK,
                                             ask=lambda _pid: None)
        self.assertIsNone(unknown.alive)
        broke = autostart.read_logon_start(
            block=GAVE_UP_BLOCK,
            ask=lambda _pid: (_ for _ in ()).throw(OSError("no tasklist")))
        self.assertIsNone(broke.alive)

    def test_the_message_box_it_waits_behind_is_written_down(self):
        """The one state where dictate is alive, holds no lock, and is not
        running: it gave up, gave the lock back (which is deliberate - see
        `test_the_lock_is_already_back_before_he_is_told`) and is sitting in a
        modal box. Windows counts the logon task as Running for exactly that
        long. Nothing said so, and the silence read as health."""
        said = "\n".join(autostart.dialog_lines(8804))
        self.assertIn(autostart.DIALOG_MARK, said)
        self.assertIn("8804", said)
        self.assertIn("NOT running", said)
        self.assertIn("Running", said)
        self.assertTrue(autostart.parse_logon_start(said).showing_dialog)


class WhatIsActuallyRunning(TempState):
    """`dictate autostart status --why`.

    One line, no paths to edit and no sequence to get right, because the person
    who runs it is not a developer and reads it off a phone. It exists to end a
    specific argument: when the logon task's run is still counted and the copy
    serving him is a different one, three different things can be true and they
    need three different fixes. From inside one process they look identical.
    """

    def evidence(self, **over) -> autostart.Evidence:
        from dictate import instance

        found = autostart.Evidence(
            lock_file=str(instance.lock_path()),
            port=8178, schtasks_raw=SCHTASKS_RUNNING)
        for key, value in over.items():
            setattr(found, key, value)
        return found

    def test_two_whisper_servers_mean_two_copies_and_it_says_so(self):
        """The lock is what makes one copy the rule, and one copy runs at most
        one whisper-server. Two of those is the lock having failed, which is a
        different fault from anything else here and is worth naming."""
        text = "\n".join(autostart.render(a_reading(
            alive=True, holder=a_holder(),
            evidence=self.evidence(processes=[
                ("pythonw.exe", 8804), ("pythonw.exe", 22188),
                ("whisper-server.exe", 31002), ("whisper-server.exe", 31003)]))))
        self.assertIn("two copies of dictate are", text)
        self.assertIn("31002", text)
        self.assertIn("31003", text)

    def test_a_whisper_server_with_nothing_above_it_is_named_as_the_orphan(self):
        text = "\n".join(autostart.render(a_reading(
            alive=False, holder=None,
            evidence=self.evidence(
                processes=[("whisper-server.exe", 31002)],
                port_holders=[(31002, "whisper-server.exe")]))))
        self.assertIn("orphan", text)
        self.assertIn("dictate stop", text)
        self.assertIn("transcription port 8178: held by pid 31002", text)

    def test_the_logon_process_alive_beside_a_different_lock_holder(self):
        """His case. The list says which pid is which, and the reading under it
        says what that amounts to without naming a cause it has not got."""
        text = "\n".join(autostart.render(a_reading(
            alive=True, image="pythonw.exe", holder=a_holder(),
            evidence=self.evidence(processes=[("pythonw.exe", 8804),
                                              ("pythonw.exe", 22188)]))))
        self.assertIn("the logon task started this one", text)
        self.assertIn("holds dictate's lock", text)
        self.assertIn("did not become a", text)

    def test_it_says_that_an_image_name_is_not_proof_of_anything(self):
        """tasklist does not print command lines, so two pythonw.exe is not two
        dictates and the report may not imply that it is."""
        text = "\n".join(autostart.render(a_reading(
            alive=True, holder=a_holder(),
            evidence=self.evidence(processes=[("pythonw.exe", 8804),
                                              ("pythonw.exe", 22188)]))))
        self.assertIn("image", text)
        self.assertIn("tasklist does not print command lines", text)

    def test_numbers_that_settle_nothing_are_reported_as_settling_nothing(self):
        text = "\n".join(autostart.render(a_reading(
            alive=None, holder=a_holder(),
            evidence=self.evidence(processes=[("pythonw.exe", 22188)]))))
        self.assertIn("nothing in the list settles it", text)

    def test_off_windows_it_says_it_did_not_ask_rather_than_reporting_nothing(self):
        """An empty list and a list nobody could take are not the same answer,
        and reading the first as the second is how "nothing is running" gets
        said about a machine nobody looked at."""
        text = "\n".join(autostart.render(a_reading(
            alive=None, holder=a_holder(),
            evidence=self.evidence(unavailable="this is linux"))))
        self.assertIn("not asked: this is linux", text)
        self.assertNotIn("nothing named python.exe", text)

    def test_it_asks_windows_for_each_image_and_for_the_port(self):
        from unittest import mock

        from dictate import recovery
        from tests import fakes

        tools = fakes.FakeProcessTools(
            listeners={8178: [recovery.Listener(31002, "127.0.0.1:8178", "0.0.0.0:0")]},
            names={8804: "pythonw.exe", 22188: "pythonw.exe",
                   31002: "whisper-server.exe"})
        cfg = config_mod.from_mapping({"whisper": {"port": 8178}})
        with mock.patch.object(recovery, "platform_tools", return_value=tools):
            found = autostart.gather_evidence(cfg, a_reading())
        self.assertEqual(found.unavailable, "")
        self.assertIn(("pythonw.exe", 8804), found.processes)
        self.assertIn(("whisper-server.exe", 31002), found.processes)
        self.assertEqual(found.port_holders, [(31002, "whisper-server.exe")])

    def test_windows_refusing_to_answer_is_recorded_and_not_raised(self):
        """A report may never be the thing that fails."""
        from unittest import mock

        from dictate import recovery

        class Refuses:
            def pids_named(self, _image):
                raise DictateError("tasklist would not run", "…")

        with mock.patch.object(recovery, "platform_tools", return_value=Refuses()):
            found = autostart.gather_evidence(None, a_reading())
        self.assertIn("tasklist", found.unavailable)

    @unittest.skipIf(sys.platform == "win32", "this is the non-Windows answer")
    def test_off_windows_nothing_is_asked_and_nothing_is_claimed(self):
        found = autostart.gather_evidence(None, a_reading())
        self.assertIn("only the Windows PC can answer", found.unavailable)
        self.assertEqual(found.processes, [])


class TheCommands(TempState):
    """The command surface, through `cli.main`, the way he would type it."""

    def test_why_is_one_line_and_needs_no_paths(self):
        """He will not remember a sequence and will not edit a path. Both
        spellings work, because the report tells him one of them and he may
        type the other."""
        for argv in (["autostart", "status", "--why"], ["autostart", "--why"]):
            with self.subTest(argv=argv):
                code, out = run_cli(argv)
                self.assertEqual(code, 0)
                self.assertIn("What is actually running", out)
                self.assertIn("What that says:", out)
                self.assertNotIn("Traceback", out)

    def test_why_still_reports_when_the_config_will_not_load(self):
        """A config that will not parse is one of the things a logon start
        fails on, so it is the last moment to refuse to report."""
        path = self.write_config("[captions]\nnum_threads = 99\n")
        code, out = run_cli(["--config", str(path), "autostart", "status", "--why"])
        self.assertEqual(code, 0)
        self.assertIn("What is actually running", out)
        self.assertIn("not checked", out)
        self.assertNotIn("Traceback", out)

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


class WhatItSaysItCosts(unittest.TestCase):
    """`enable` tells him what leaving this on costs the graphics card, and the
    honest answer depends on `[whisper] idle_release_minutes` - so it reads that
    rather than asserting a number. It said "1.6 GB for as long as you are
    logged in" until the idle release made that untrue."""

    def lines(self, **whisper) -> str:
        cfg = config_mod.from_mapping({"whisper": whisper} if whisper else {})
        return "\n".join(autostart._memory_cost_lines(cfg))

    def test_by_default_it_says_the_memory_comes_back_on_its_own(self):
        text = self.lines()
        self.assertIn("1.6 GB", text)
        self.assertIn("5 minutes", text)
        self.assertIn("hands it back", text)
        self.assertNotIn("as long as you are logged in", text)

    def test_it_uses_the_release_time_that_is_actually_configured(self):
        self.assertIn("20 minutes", self.lines(idle_release_minutes=20.0))
        self.assertIn("1 minute", self.lines(idle_release_minutes=1.0))

    def test_with_the_release_turned_off_it_says_so_plainly(self):
        text = self.lines(idle_release_minutes=0)
        self.assertIn("as long as you are logged in", text)
        self.assertIn("1.6 GB", text)
        # And the cheaper remedy before the drastic one.
        self.assertIn("idle_release_minutes", text)


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
