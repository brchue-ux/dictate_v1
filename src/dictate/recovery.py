"""Getting out of a stuck state - whatever the stuck state turns out to be.

There is one command for this, `dictate stop`, and it is the only one anybody
has to remember. It does not ask which failure this is: it looks at each thing
dictate can leave behind, in order, and clears what it finds.

    a copy that is running          -> asked to stop, the way Ctrl+C asks
    a copy that will not answer     -> ended, with its whisper-server child
    a whisper-server with no parent -> ended (this is the one that bit him)
    the port held by something else -> named, with the exact command to type

The thing this module clears should never exist in the first place. Since the
job object in `platform/windows/job.py`, whisper-server cannot outlive dictate
however dictate dies. But an orphan left by a build from before that is still
sitting on the port, and "run the installer again" has to get him out of it, so
the rescue path stays and is what `setup.ps1` calls too.

**Nothing here kills a parent on its own.** `docs/DESIGN.md` and the sharp-edge
note in AGENTS.md say why: dictate owns a child holding the transcription port
and, while the model is resident, ~1.6 GB of VRAM, so ending the parent alone
strands it. Every forced stop below takes the whole tree (`taskkill /T`), and
only after the polite request has been given its full timeout.

Everything that decides *what to do* is in this file, plain and injectable, so
`tests/test_recovery.py` drives all of it on any platform. The three things that
actually touch Windows - listing who holds a TCP port, naming a process, ending
a process tree - are behind `ProcessTools` in `platform/base.py`, and their
Windows implementation is a thin shell around netstat, tasklist and taskkill in
`platform/windows/processes.py`. The parsing of what those three print is here,
and is tested against their real output.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from . import instance
from .errors import DictateError

log = logging.getLogger(__name__)

#: What our own transcription child is called, whatever built it. Matched
#: case-insensitively against the image name of whoever holds the port, because
#: nothing is ever ended on the strength of a port number alone: a pid can be
#: reused between the moment netstat prints it and the moment we act on it.
WHISPER_SERVER_NAMES = ("whisper-server.exe", "whisper-server")

#: What dictate itself runs as. Same rule: a wedged copy is only ended when the
#: pid recorded in the lock file still belongs to something that looks like us.
DICTATE_NAMES = ("python.exe", "pythonw.exe", "dictate.exe", "python", "python3")

#: How long to wait for a port to come free after ending whoever held it.
PORT_RELEASE_TIMEOUT_S = 10.0


# ---------------------------------------------------------------------------
# What the Windows tools print. Pure, so it is tested against real output.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Listener:
    """One row of `netstat -ano` that concerns the port we asked about."""

    pid: int
    local: str = ""
    foreign: str = ""

    @property
    def listening(self) -> bool:
        """A listening socket has no peer. Judged by the foreign address rather
        than by the state word, which Windows translates."""
        return self.foreign in ("0.0.0.0:0", "[::]:0", "*:*", "")


def _port_of(address: str) -> int | None:
    """The port out of `127.0.0.1:8178` or `[::1]:8178`."""
    _, sep, tail = address.rpartition(":")
    if not sep or not tail.isdigit():
        return None
    return int(tail)


def parse_listeners(text: str, port: int) -> list[Listener]:
    """Rows of `netstat -ano` output whose LOCAL address is on `port`.

    Deliberately positional and state-word-blind: netstat's column headings and
    its state names ("LISTENING") are translated on a Windows that is not in
    English, while the addresses and the pid in the last column are not. The
    listening row is preferred by the caller, but connections to the port are
    returned too - they are held by the same process, and on the day the
    listener row is missing they still name it.
    """
    found: list[Listener] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4 or not parts[-1].isdigit():
            continue
        if parts[0].upper() not in ("TCP", "TCPV6", "UDP"):
            continue
        if _port_of(parts[1]) != port:
            continue
        found.append(Listener(pid=int(parts[-1]), local=parts[1], foreign=parts[2]))
    return found


def pick_listener(listeners: list[Listener]) -> Listener | None:
    """The one that actually holds the port: the listening socket if there is
    one, otherwise whoever else is on it."""
    for entry in listeners:
        if entry.listening:
            return entry
    return listeners[0] if listeners else None


def parse_task_name(text: str) -> str:
    """The image name out of `tasklist /FO CSV /NH /FI "PID eq n"`.

    The rows are quoted CSV - `"whisper-server.exe","1234","Console",...` - and
    the image name is not translated. tasklist answers a filter that matches
    nothing with an ordinary sentence rather than an error, so anything that is
    not a quoted row means "no such process", not "something went wrong".
    """
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith('"'):
            continue
        name = line[1:].split('"', 1)[0].strip()
        if name:
            return name
    return ""


def is_whisper_server(name: str) -> bool:
    return name.strip().lower() in WHISPER_SERVER_NAMES


def is_dictate_process(name: str) -> bool:
    return name.strip().lower() in DICTATE_NAMES


# ---------------------------------------------------------------------------
# The state of the transcription port
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PortState:
    """Who has the transcription port, and whether it is one of ours."""

    host: str
    port: int
    held: bool = False
    healthy: bool = False
    pid: int | None = None
    name: str = ""

    @property
    def ours(self) -> bool:
        return self.pid is not None and is_whisper_server(self.name)

    def describe(self) -> str:
        where = f"{self.host}:{self.port}"
        if not self.held:
            return f"{where} is free"
        who = ""
        if self.name and self.pid:
            who = f" by {self.name} (process {self.pid})"
        elif self.pid:
            who = f" by process {self.pid}"
        elif self.healthy:
            who = " by a whisper-server that answers as healthy"
        return f"{where} is in use{who}"


def port_is_open(host: str, port: int, *, timeout_s: float = 0.5) -> bool:
    """Is anything listening? One socket connect, and it works everywhere."""
    from .engines.whisper_server import WhisperServerClient  # noqa: PLC0415

    return WhisperServerClient(host, port).port_is_open(timeout_s)


def look_at_port(host: str, port: int, tools=None) -> PortState:
    """What is on the transcription port right now.

    `tools` is `None` on a machine that has no way to look up the owner of a
    port. That is not a failure - the socket check above still answers the
    question that matters, "is it held" - and the caller says so plainly rather
    than claiming it knows who is there.
    """
    from .engines.whisper_server import WhisperServerClient  # noqa: PLC0415

    client = WhisperServerClient(host, port)
    if not client.port_is_open():
        return PortState(host=host, port=port, held=False)
    healthy = client.is_healthy()
    pid: int | None = None
    name = ""
    if tools is not None:
        try:
            owner = pick_listener(tools.listeners(port))
        except Exception:  # noqa: BLE001 - a rescue may not fail on diagnosis
            log.debug("could not list who holds port %s", port, exc_info=True)
            owner = None
        if owner is not None:
            pid = owner.pid
            try:
                name = tools.name_of(owner.pid)
            except Exception:  # noqa: BLE001
                log.debug("could not name process %s", owner.pid, exc_info=True)
    return PortState(host=host, port=port, held=True, healthy=healthy,
                     pid=pid, name=name)


def wait_for_port_free(host: str, port: int, timeout_s: float = PORT_RELEASE_TIMEOUT_S,
                       *, poll_s: float = 0.25, sleep=time.sleep) -> bool:
    deadline = time.monotonic() + timeout_s
    while True:
        if not port_is_open(host, port):
            return True
        if time.monotonic() >= deadline:
            return False
        sleep(poll_s)


# ---------------------------------------------------------------------------
# The rescue itself
# ---------------------------------------------------------------------------


Say = Callable[[str], None]


@dataclass
class Outcome:
    """What `dictate stop` found and what it managed to do about it."""

    #: True when nothing dictate owns is left running.
    ok: bool = True
    #: True when a live copy of dictate was found (running, and answering).
    was_running: bool = False
    #: True when a live copy was found and deliberately left alone.
    left_alone: bool = False
    #: What was ended, for the tests and for the log.
    ended: list[int] = field(default_factory=list)


def stop(cfg, *, say: Say, timeout_s: float = 20.0, stale_only: bool = False,
         tools=None, port_timeout_s: float = PORT_RELEASE_TIMEOUT_S) -> Outcome:
    """The whole of `dictate stop`, in the order that does the least damage.

    `stale_only` is what `setup.ps1` passes: clear what an earlier run left
    behind, but never stop a copy that is running and answering, because the
    installer checking itself is not a reason to take away the thing he is
    using. Everything else about it is identical.
    """
    outcome = Outcome()
    if tools is None:
        tools = platform_tools()

    holder = instance.running_instance()
    if holder is None:
        say("dictate is not running.")
    else:
        outcome.was_running = True
        if stale_only:
            say(f"dictate is running ({holder.describe()}), so it has been left "
                "alone.")
            outcome.left_alone = True
            return outcome
        _stop_the_running_copy(holder, outcome, say=say, timeout_s=timeout_s,
                               tools=tools)

    _clear_the_port(cfg, outcome, say=say, tools=tools, timeout_s=port_timeout_s)
    return outcome


def _stop_the_running_copy(holder, outcome: Outcome, *, say: Say, timeout_s: float,
                           tools) -> None:
    say(f"asking dictate to stop ({holder.describe()})…")
    instance.request_stop()
    if instance.wait_until_stopped(timeout_s):
        say("dictate has stopped, and whisper-server with it.")
        if holder.started_by == "logon":
            say("")
            say("It will start again the next time you log in. To prevent that:")
            say("  dictate autostart disable")
        return

    # It did not answer. That is the state he was reduced to Task Manager for,
    # so this ends it - the WHOLE tree, never the parent on its own, or the
    # whisper-server it owns would be exactly the orphan we are here to clear.
    instance.clear_stop_request()
    say(f"dictate did not stop within {timeout_s:.0f} seconds of being asked, "
        "so it is being ended.")
    if not holder.pid:
        say("")
        say("dictate does not know which process it is - its lock file has no "
            "number in it.")
        say("End it in Task Manager: look for pythonw.exe or python.exe, and "
            "for whisper-server.exe,")
        say("which has to go too.")
        outcome.ok = False
        return
    if tools is None:
        say("")
        say(f"dictate can only end a copy that will not answer on Windows, and "
            f"this is {sys.platform}.")
        say(f"It is process {holder.pid}, and the whisper-server.exe it owns has "
            "to go with it.")
        outcome.ok = False
        return

    name = _name_of(tools, holder.pid)
    if name and not is_dictate_process(name):
        # The pid in the lock file belongs to something else now. Ending it
        # would be ending a stranger's process on the strength of a stale record.
        say(f"process {holder.pid} is {name}, which is not dictate, so nothing "
            "was ended.")
        say("Try once more in a moment:")
        say("  dictate stop")
        say("If it says the same thing again, restart the PC.")
        outcome.ok = False
        return

    if _end(tools, holder.pid, say=say, what="dictate"):
        outcome.ended.append(holder.pid)
    if instance.wait_until_stopped(10.0):
        say(f"dictate (process {holder.pid}) was ended, along with the "
            "whisper-server it owned.")
    else:
        say(f"process {holder.pid} would not end. Type this:")
        say(f"  taskkill /PID {holder.pid} /T /F")
        outcome.ok = False


def _clear_the_port(cfg, outcome: Outcome, *, say: Say, tools,
                    timeout_s: float = PORT_RELEASE_TIMEOUT_S) -> None:
    host, port = cfg.whisper.host, cfg.whisper.port
    state = look_at_port(host, port, tools)
    if not state.held:
        say(f"the transcription port {host}:{port} is free.")
        return

    if state.ours:
        # One case where OUR whisper-server must be left where it is: a copy of
        # dictate is still holding the lock, which means the stop above did not
        # work. That server is not an orphan, it is a running dictate's child -
        # and ending it would only make that dictate start another one.
        holder = instance.running_instance()
        if holder is not None:
            say(f"{host}:{port} is held by the whisper-server belonging to the "
                f"copy of dictate that is")
            say(f"still running ({holder.describe()}), so it was left alone.")
            outcome.ok = False
            return
        say(f"a whisper-server from an earlier run is still holding {host}:{port} "
            f"(process {state.pid}).")
        if _end(tools, state.pid, say=say, what="whisper-server"):
            outcome.ended.append(state.pid)
        if wait_for_port_free(host, port, timeout_s):
            say(f"it has been ended and {host}:{port} is free again.")
            return
        say("it would not end. Type this:")
        say(f"  taskkill /PID {state.pid} /T /F")
        outcome.ok = False
        return

    outcome.ok = False
    say("")
    say(f"something is still holding {host}:{port}: {state.describe()}.")
    if state.pid and state.name:
        say("That is not part of dictate, so nothing was ended. Either close "
            "it, or give dictate a")
        say("port of its own: set  [whisper] port  to another number in your "
            "config file.")
    elif tools is None:
        say(f"dictate can only clear a stuck transcription process on Windows, "
            f"and this is {sys.platform}.")
    else:
        say("dictate could not find out which program has it. Type this to see:")
        say(f"  netstat -ano | findstr :{port}")
        say("then, with the number from the last column:")
        say("  taskkill /PID <that number> /T /F")


def _name_of(tools, pid: int) -> str:
    try:
        return tools.name_of(pid)
    except Exception:  # noqa: BLE001 - naming a process may not break the rescue
        log.debug("could not name process %s", pid, exc_info=True)
        return ""


def _end(tools, pid: int | None, *, say: Say, what: str) -> bool:
    if pid is None:
        return False
    try:
        tools.end(pid)
    except DictateError as exc:
        say(f"  ({exc.message})")
        return False
    except Exception as exc:  # noqa: BLE001
        log.debug("ending %s (%s) raised", what, pid, exc_info=True)
        say(f"  (ending process {pid} did not work: {exc})")
        return False
    return True


def platform_tools():
    """The three Windows commands the rescue needs, or `None` off Windows.

    `None` is not a stand-in: nothing pretends to have ended anything. The
    caller says plainly that clearing a stuck process needs the Windows machine,
    and everything that does not need those commands - the lock, the stop
    request, the port check - works here exactly as it does there.
    """
    if sys.platform != "win32":
        return None
    from .platform import factory  # noqa: PLC0415

    return factory.make_process_tools()


# ---------------------------------------------------------------------------
# Starting a fresh copy
# ---------------------------------------------------------------------------


def relaunch_argv(config_path: str | None = None,
                  executable: str | None = None, *,
                  autostart: bool = False) -> list[str]:
    """What to run to start a new copy of dictate.

    Through the interpreter that is running now (`-m dictate`), not through
    `dictate.exe`: under the logon task that interpreter is `pythonw.exe`, and
    the console-script wrapper is a console program that would put a black
    window on screen every restart.

    `autostart` carries over HOW this copy was started, and it is load-bearing
    rather than cosmetic. Under `pythonw.exe` there is no console at all -
    `sys.stdout` and `sys.stderr` are `None` - and only the `--autostart` entry
    point puts a `LogonLog` in their place. A restart that dropped the flag
    would start a copy that fell over on its first `print`, in a window that
    does not exist, where nobody would ever see why.
    """
    argv = [executable or sys.executable, "-m", "dictate"]
    if config_path:
        argv += ["--config", str(config_path)]
    argv += ["run"]
    if autostart:
        argv += ["--autostart"]
    return argv


def relaunch(config_path: str | None = None, *, executable: str | None = None,
             autostart: bool = False, spawn=subprocess.Popen) -> int:
    """Start a fresh copy of dictate, detached from this one.

    Called after the instance lock has been let go, never before: the new copy
    takes the lock the moment it starts, and a restart that raced its own
    predecessor would be refused as "dictate is already running".

    Returns the new process's pid, or raises `DictateError` naming the command
    to type instead.
    """
    argv = relaunch_argv(config_path, executable, autostart=autostart)
    kwargs: dict = {"close_fds": True}
    attempts: list[dict] = []
    if sys.platform == "win32":
        # DETACHED_PROCESS so it does not die with the console this one was
        # started from, and CREATE_BREAKAWAY_FROM_JOB because a copy started by
        # the logon task lives inside Task Scheduler's own job object - which
        # ends when the task ends, taking the new copy with it. A job that does
        # not permit breakaway refuses the call outright, and then the plain
        # form is correct: there is no job to escape.
        detached = 0x00000008 | 0x00000200      # DETACHED_PROCESS | NEW_PROCESS_GROUP
        breakaway = 0x01000000                  # CREATE_BREAKAWAY_FROM_JOB
        attempts = [{"creationflags": detached | breakaway},
                    {"creationflags": detached}]
    else:
        attempts = [{"start_new_session": True}]

    last: OSError | None = None
    for extra in attempts:
        try:
            return spawn(argv, **extra, **kwargs).pid
        except OSError as exc:
            last = exc
            log.info("could not start the new copy with %s: %s", extra, exc)
    raise DictateError(
        f"dictate could not start itself again: {last}",
        "Start it by hand:\n  dictate run",
    )
