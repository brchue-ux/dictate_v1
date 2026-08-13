"""The one command that gets him out of any stuck state.

`dictate stop` is the whole of the answer to "there is no easy way I see to stop
it beyond powershell commands I wont remember", so what it does is worth holding
in place in detail: it must clear a copy that is running, a copy that will not
answer, and a whisper-server an earlier run left holding the transcription port
- without being told which of those it is looking at, and without ever ending
something that is not ours.

The port is a REAL socket in these tests, held by the real stub server, so the
"is it still held?" question is answered the way it is on his machine. Only the
three things that would need Windows - who holds a port, what a process is
called, end it - are supplied, because there is no netstat here.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from dictate import config as config_mod, instance, recovery
from dictate.errors import DictateError

from . import fakes, stub_server

CHILD = Path(__file__).resolve().parent / "lock_child.py"

#: Real `netstat -ano` output, kept exactly as Windows prints it, including the
#: leading blank line and the two-space indent. Parsing this is the difference
#: between finding the orphan and telling him to open Task Manager.
NETSTAT = """
Active Connections

  Proto  Local Address          Foreign Address        State           PID
  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       1204
  TCP    127.0.0.1:8178         0.0.0.0:0              LISTENING       23188
  TCP    127.0.0.1:8178         127.0.0.1:54121        ESTABLISHED     23188
  TCP    127.0.0.1:54121        127.0.0.1:8178         ESTABLISHED     9012
  TCP    [::]:445               [::]:0                 LISTENING       4
  UDP    0.0.0.0:5353           *:*                                    3120
"""

TASKLIST = ('"whisper-server.exe","23188","Console","1","1,684,132 K"\n')


def free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class WhatWindowsPrints(unittest.TestCase):
    def test_it_finds_the_process_holding_the_port(self):
        rows = recovery.parse_listeners(NETSTAT, 8178)
        self.assertEqual([r.pid for r in rows], [23188, 23188])
        owner = recovery.pick_listener(rows)
        self.assertEqual(owner.pid, 23188)
        # The listening row, not the connection that happens to be on the same
        # port - and judged by the foreign address, because "LISTENING" is a
        # word Windows translates.
        self.assertTrue(owner.listening)
        self.assertEqual(owner.local, "127.0.0.1:8178")

    def test_a_connection_to_the_port_is_not_mistaken_for_the_listener(self):
        rows = recovery.parse_listeners(NETSTAT, 54121)
        self.assertEqual([r.pid for r in rows], [9012])
        self.assertFalse(rows[0].listening)

    def test_other_ports_are_left_alone(self):
        self.assertEqual(recovery.parse_listeners(NETSTAT, 445)[0].pid, 4)
        self.assertEqual(recovery.parse_listeners(NETSTAT, 8179), [])

    def test_nonsense_is_not_a_crash(self):
        self.assertEqual(recovery.parse_listeners("", 8178), [])
        self.assertEqual(recovery.parse_listeners("no idea\n\n  what\n", 8178), [])
        self.assertIsNone(recovery.pick_listener([]))

    def test_it_reads_the_image_name_back(self):
        self.assertEqual(recovery.parse_task_name(TASKLIST), "whisper-server.exe")
        self.assertTrue(recovery.is_whisper_server("WHISPER-SERVER.EXE"))
        self.assertFalse(recovery.is_whisper_server("chrome.exe"))

    def test_tasklist_saying_there_is_no_such_process(self):
        # It answers a filter that matches nothing with a sentence, not an
        # error, and that sentence must not be read as a process name.
        said = "INFO: No tasks are running which match the specified criteria."
        self.assertEqual(recovery.parse_task_name(said), "")
        self.assertEqual(recovery.parse_task_name(""), "")


class NothingIsRunning(unittest.TestCase):
    """The state he is in when he is not sure whether anything is running,
    which is exactly when he types this."""

    def setUp(self):
        self.cfg = config_mod.load(None)
        self.cfg.whisper.port = free_port()
        self.said: list[str] = []

    def test_it_says_so_and_reports_the_port(self):
        outcome = recovery.stop(self.cfg, say=self.said.append, tools=None)
        self.assertTrue(outcome.ok)
        self.assertFalse(outcome.was_running)
        joined = "\n".join(self.said)
        self.assertIn("dictate is not running", joined)
        self.assertIn("is free", joined)


class AnOrphanedServer(unittest.TestCase):
    """The failure that cost him the evening: whisper-server holding the port
    with no dictate behind it."""

    def setUp(self):
        self.server, self.port = stub_server.serve_in_thread()
        self.closed = False
        self.addCleanup(self._close)
        self.cfg = config_mod.load(None)
        self.cfg.whisper.port = self.port
        self.said: list[str] = []

    def _close(self, _pid=None):
        if self.closed:
            return
        self.closed = True
        self.server.shutdown()
        self.server.server_close()

    def tools(self, name="whisper-server.exe", **kwargs):
        return fakes.FakeProcessTools(
            listeners={self.port: [recovery.Listener(4242, f"127.0.0.1:{self.port}",
                                                     "0.0.0.0:0")]},
            names={4242: name}, on_end=self._close, **kwargs)

    def test_it_is_ended_without_being_asked_which_failure_this_is(self):
        tools = self.tools()
        outcome = recovery.stop(self.cfg, say=self.said.append, tools=tools)
        self.assertTrue(outcome.ok)
        self.assertEqual(tools.ended, [4242])
        joined = "\n".join(self.said)
        self.assertIn("earlier run", joined)
        self.assertIn("free again", joined)
        self.assertFalse(recovery.port_is_open("127.0.0.1", self.port))

    def test_what_it_looks_at_before_it_ends_anything(self):
        state = recovery.look_at_port("127.0.0.1", self.port, self.tools())
        self.assertTrue(state.held)
        self.assertTrue(state.healthy)      # the stub answers /health
        self.assertTrue(state.ours)
        self.assertIn("whisper-server.exe", state.describe())

    def test_something_that_is_not_ours_is_named_and_left_alone(self):
        tools = self.tools(name="chrome.exe")
        outcome = recovery.stop(self.cfg, say=self.said.append, tools=tools)
        self.assertFalse(outcome.ok)
        self.assertEqual(tools.ended, [])
        joined = "\n".join(self.said)
        self.assertIn("chrome.exe", joined)
        self.assertIn("[whisper] port", joined)   # named exactly, not described

    def test_an_unknown_holder_is_answered_with_the_exact_command(self):
        tools = fakes.FakeProcessTools(listeners={}, names={})
        outcome = recovery.stop(self.cfg, say=self.said.append, tools=tools)
        self.assertFalse(outcome.ok)
        joined = "\n".join(self.said)
        self.assertIn(f"netstat -ano | findstr :{self.port}", joined)
        self.assertIn("taskkill /PID", joined)

    def test_one_that_will_not_die_names_the_command_that_ends_it(self):
        tools = self.tools(lingering=True)
        outcome = recovery.stop(self.cfg, say=self.said.append, tools=tools,
                                port_timeout_s=0.5)
        self.assertFalse(outcome.ok)
        self.assertIn("taskkill /PID 4242 /T /F", "\n".join(self.said))

    def test_taskkill_refusing_is_reported_rather_than_swallowed(self):
        tools = self.tools(refuse=DictateError("Access is denied", "run as admin"),
                           lingering=True)
        outcome = recovery.stop(self.cfg, say=self.said.append, tools=tools,
                                port_timeout_s=0.5)
        self.assertFalse(outcome.ok)
        self.assertIn("Access is denied", "\n".join(self.said))


class AgainstARealSecondProcess(unittest.TestCase):
    """A copy that holds the lock and will not answer. The lock is real and the
    process is real - only the ending of it is supplied, because that is
    taskkill."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._previous = os.environ.get("DICTATE_STATE_DIR")
        os.environ["DICTATE_STATE_DIR"] = self._tmp.name
        self.addCleanup(self._restore)
        self.cfg = config_mod.load(None)
        self.cfg.whisper.port = free_port()
        self.said: list[str] = []

        self.holder = subprocess.Popen(
            [sys.executable, str(CHILD), "hold", str(instance.lock_path()), "30"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(self.holder.kill)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.holder.stdout.readline().strip() == "HELD":
                break

    def _restore(self):
        if self._previous is None:
            os.environ.pop("DICTATE_STATE_DIR", None)
        else:
            os.environ["DICTATE_STATE_DIR"] = self._previous
        self._tmp.cleanup()

    def test_stale_only_leaves_a_running_copy_alone(self):
        """What `setup.ps1` calls. Checking an installation is not a reason to
        take away the thing he is using."""
        tools = fakes.FakeProcessTools()
        outcome = recovery.stop(self.cfg, say=self.said.append, tools=tools,
                                stale_only=True)
        self.assertTrue(outcome.left_alone)
        self.assertTrue(outcome.was_running)
        self.assertEqual(tools.ended, [])
        self.assertIn("left alone", "\n".join(self.said))
        self.assertIsNotNone(instance.running_instance())

    def test_a_copy_that_will_not_answer_is_ended_with_its_child(self):
        pid = self.holder.pid
        tools = fakes.FakeProcessTools(names={pid: "python.exe"})

        def end(target):                       # what taskkill /T would do
            self.assertEqual(target, pid)
            self.holder.kill()
            self.holder.wait(timeout=10)

        tools.on_end = end
        outcome = recovery.stop(self.cfg, say=self.said.append, tools=tools,
                                timeout_s=1.0)
        self.assertTrue(outcome.ok)
        self.assertEqual(tools.ended, [pid])
        joined = "\n".join(self.said)
        self.assertIn("did not stop", joined)
        self.assertIn("whisper-server", joined)   # it went too, and it is said

    def test_a_running_copys_whisper_server_is_never_taken_from_under_it(self):
        """If the copy could not be stopped, the server on the port is not an
        orphan - it is a running dictate's child, and ending it would only make
        that dictate start another one."""
        server, port = stub_server.serve_in_thread()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.cfg.whisper.port = port
        tools = fakes.FakeProcessTools(
            listeners={port: [recovery.Listener(4242, f"127.0.0.1:{port}",
                                                "0.0.0.0:0")]},
            names={4242: "whisper-server.exe",
                   self.holder.pid: "notepad.exe"})

        outcome = recovery.stop(self.cfg, say=self.said.append, tools=tools,
                                timeout_s=1.0)
        self.assertFalse(outcome.ok)
        self.assertEqual(tools.ended, [])
        self.assertIn("left alone", "\n".join(self.said))

    def test_a_pid_that_now_belongs_to_something_else_is_not_ended(self):
        """The lock file records a number, and numbers get reused. Ending a
        stranger's process on the strength of a stale record is not a rescue."""
        tools = fakes.FakeProcessTools(names={self.holder.pid: "notepad.exe"})
        outcome = recovery.stop(self.cfg, say=self.said.append, tools=tools,
                                timeout_s=1.0)
        self.assertFalse(outcome.ok)
        self.assertEqual(tools.ended, [])
        self.assertIn("not dictate", "\n".join(self.said))


class StartingAFreshCopy(unittest.TestCase):
    """What the tray's Restart does once the old copy has let go."""

    def test_it_goes_through_this_interpreter_not_the_console_script(self):
        argv = recovery.relaunch_argv(None, executable=r"C:\Python\pythonw.exe")
        self.assertEqual(argv[:3], [r"C:\Python\pythonw.exe", "-m", "dictate"])
        self.assertEqual(argv[-1], "run")
        self.assertNotIn("dictate.exe", " ".join(argv))

    def test_the_config_it_was_started_with_is_carried_over(self):
        argv = recovery.relaunch_argv(r"D:\conf\dictate.toml")
        self.assertIn("--config", argv)
        self.assertIn(r"D:\conf\dictate.toml", argv)

    def test_a_copy_started_at_logon_restarts_as_one(self):
        """Under pythonw there is no stdout and no stderr, and only the
        --autostart entry point puts a log in their place. A restart that
        dropped the flag would start a copy that fell over on its first print,
        in a window that does not exist."""
        self.assertNotIn("--autostart", recovery.relaunch_argv(None))
        self.assertIn("--autostart", recovery.relaunch_argv(None, autostart=True))

    def test_it_is_detached_so_it_survives_the_window_closing(self):
        seen: list[dict] = []

        class Started:
            pid = 4321

        def spawn(argv, **kwargs):
            seen.append(kwargs)
            return Started()

        pid = recovery.relaunch(None, spawn=spawn)
        self.assertEqual(pid, 4321)
        if sys.platform == "win32":
            self.assertTrue(seen[0]["creationflags"] & 0x00000008)   # DETACHED
        else:
            self.assertTrue(seen[0]["start_new_session"])

    def test_it_says_what_to_type_when_it_cannot(self):
        def spawn(argv, **kwargs):
            raise OSError("nope")

        with self.assertRaises(DictateError) as ctx:
            recovery.relaunch(None, spawn=spawn)
        self.assertIn("dictate run", ctx.exception.remedy)

    def test_something_started_to_replace_us_gets_a_console_and_leaves_the_job(self):
        """The other caller: `dictate update` started from the notification
        area. It has to outlive this process - stopping it is the second thing
        it does - and it has to be able to speak, because the process that
        started it may be running under pythonw.exe with no console at all."""
        seen: list[dict] = []

        class Started:
            pid = 77

        def spawn(argv, **kwargs):
            seen.append(kwargs)
            return Started()

        recovery.spawn_detached(["python", "-m", "dictate", "update"],
                                console=True, spawn=spawn)
        if sys.platform == "win32":
            self.assertTrue(seen[0]["creationflags"] & 0x00000010)   # NEW_CONSOLE
            self.assertFalse(seen[0]["creationflags"] & 0x00000008)  # not DETACHED
            self.assertTrue(seen[0]["creationflags"] & 0x01000000)   # BREAKAWAY
        else:
            self.assertTrue(seen[0]["start_new_session"])

    def test_a_job_that_refuses_breakaway_is_tried_again_without_it(self):
        """A job object that does not permit breakaway refuses the call
        outright, and then the plain form is right: there is no job to escape.

        Windows' half of the decision, driven from here: `sys.platform` is what
        chooses it, so that is what is supplied.
        """
        from unittest import mock

        seen: list[dict] = []

        class Started:
            pid = 88

        def spawn(argv, **kwargs):
            seen.append(kwargs)
            if len(seen) == 1:
                raise OSError("access is denied")
            return Started()

        with mock.patch.object(recovery.sys, "platform", "win32"):
            started = recovery.spawn_detached(["python", "-m", "dictate", "run"],
                                              spawn=spawn)
        self.assertEqual(started.pid, 88)
        self.assertEqual(len(seen), 2)
        self.assertTrue(seen[0]["creationflags"] & 0x01000000)    # tried it
        self.assertFalse(seen[1]["creationflags"] & 0x01000000)   # then did not

    def test_it_raises_what_stopped_it_and_lets_the_caller_say_what_that_means(self):
        def spawn(argv, **kwargs):
            raise OSError("nope")

        with self.assertRaises(OSError):
            recovery.spawn_detached(["python"], spawn=spawn)


if __name__ == "__main__":
    unittest.main()
