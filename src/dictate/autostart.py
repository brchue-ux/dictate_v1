"""Starting dictate when the product owner logs in.

**Not a Windows service.** A service runs in session 0, which has no access to
the interactive desktop: from there the caption overlay cannot appear on his
screen and synthesised keystrokes cannot reach his focused window. Those two
things *are* the product, so a service would install cleanly, start cleanly, and
do nothing. What is wanted is his own desktop session, and that is a **per-user
logon task** in the Windows Task Scheduler:

* it runs in his interactive session, as him, with `LogonType=InteractiveToken`,
  so no password is stored anywhere and the overlay and the paste both work;
* `RunLevel=LeastPrivilege` - no administrator rights at run time;
* the task starts `pythonw.exe`, which has no console at all, so nothing flashes
  up at logon (a Startup-folder shortcut or a `Run` registry key would run
  `dictate.exe`, a console program, and show a window every single time);
* it carries a `Delay`, a `MultipleInstancesPolicy` and an unlimited
  `ExecutionTimeLimit`, none of which a shortcut can express;
* Windows itself records whether it ran and what it returned, which is what
  `dictate autostart status` reads back;
* and turning it off is one command that leaves nothing behind.

**Nobody who wrote this has Windows.** Everything below is split so that the part
which can be checked here - what the task XML says, what `schtasks` is asked,
what its output means, what the retry loop does - is plain data and pure
functions with tests, and the part that cannot - Task Scheduler actually
accepting the task and firing it at logon - is as small as possible and is
checked by CI as far as a machine that never logs on can check it.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

from . import instance
from .config import AutostartConfig, Config
from .errors import ConfigError, DictateError, PlatformUnsupportedError

log = logging.getLogger(__name__)

#: The name it appears under in Task Scheduler. Deleting this one task is the
#: whole of "turn it off"; nothing else is registered anywhere.
TASK_NAME = "dictate"

#: Every logon start writes a block to this file, whether it worked or not. It
#: is deliberately NOT the log named in the config: a config that will not load
#: is one of the things that can go wrong at logon, and the report of that has to
#: land somewhere that does not depend on it.
LOG_NAME = "autostart.log"
BLOCK_MARK = "=== dictate autostart"
#: Rolled at this size into a single `.1` companion. Two files, no more.
LOG_MAX_BYTES = 512_000


def log_path() -> Path:
    return instance.state_dir() / LOG_NAME


# ---------------------------------------------------------------------------
# What the task says. Pure functions - this is the part that can be checked
# without Windows, and the part most likely to be wrong.
# ---------------------------------------------------------------------------


def interactive_user(env: dict[str, str] | None = None) -> str:
    """The account the task runs as, as Task Scheduler wants it written.

    `DOMAIN\\user` when Windows tells us the domain, the bare user name when it
    does not. Never a guess at a domain: a wrong one makes the task register and
    then never fire, which is the worst of both.
    """
    env = os.environ if env is None else env
    user = env.get("USERNAME") or env.get("USER") or ""
    domain = env.get("USERDOMAIN") or ""
    if not user:
        raise DictateError(
            "Windows did not say which account you are logged in as, so dictate "
            "cannot register a task that starts when you log in.",
            "Run `echo %USERNAME%` in the same window. If that is empty, this is "
            "not an ordinary interactive logon - run setup from a normal "
            "PowerShell window opened from the Start menu.",
        )
    return f"{domain}\\{user}" if domain else user


def windowless_python(executable: str | None = None) -> Path:
    """`pythonw.exe`, the interpreter with no console attached.

    This is what keeps a console window from appearing at every single logon, so
    a missing `pythonw.exe` is refused rather than quietly swapped for
    `python.exe` - which would work, and would flash a black window in his face
    every morning.
    """
    exe = Path(executable or sys.executable)
    candidate = exe.with_name("pythonw.exe")
    if candidate.exists():
        return candidate
    raise DictateError(
        f"dictate could not find pythonw.exe next to {exe}. That is the version "
        "of Python with no console window, and without it starting at logon "
        "would flash a black window on screen every time.",
        "This usually means Python was installed without its windowed launcher.\n"
        "Install the ordinary one from python.org (or with\n"
        "  winget install --id Python.Python.3.12\n"
        ") and run `dictate autostart enable` again.",
    )


def quote_argument(value: str) -> str:
    """Quote one command-line argument the way Windows programs expect."""
    if value and not any(ch in value for ch in ' \t"'):
        return value
    return '"' + value.replace('"', r"\"") + '"'


def task_arguments(config_path: str | None = None) -> str:
    """What `pythonw.exe` is asked to run.

    `-m dictate` rather than `dictate.exe` because the console-script wrapper is
    itself a console program: running it under pythonw would still create a
    window. `--config` is only pinned into the task when one was named
    explicitly, so the ordinary case keeps looking wherever dictate looks.
    """
    args = ["-m", "dictate"]
    if config_path:
        args += ["--config", str(config_path)]
    args += ["run", "--autostart"]
    return " ".join(quote_argument(a) for a in args)


def task_xml(
    *,
    user: str,
    command: str,
    arguments: str,
    working_directory: str = "",
    delay_s: int = 30,
    description: str = "",
) -> str:
    """The task definition, in the schema Task Scheduler 1.2 documents.

    Every setting in here is load-bearing:

    * `LogonTrigger` with a `UserId` - his logon, not everybody's.
    * `Delay` - at logon the graphics driver may still be loading and a second
      disk may not be mounted yet. Starting a few seconds later costs nothing
      and avoids the most common reason a logon start fails.
    * `InteractiveToken` - runs in his desktop session, stores no password.
    * `LeastPrivilege` - no administrator rights while it runs.
    * `IgnoreNew` - Task Scheduler's own refusal to start a second copy, on top
      of dictate's lock.
    * `ExecutionTimeLimit PT0S` - unlimited. The default is three days, after
      which Windows would end it and he would find dictate mysteriously dead.
    * batteries left alone in both directions, so a machine on a UPS or a laptop
      does not silently skip it.
    """
    delay = f"      <Delay>PT{int(delay_s)}S</Delay>\n" if delay_s > 0 else ""
    working = (
        f"      <WorkingDirectory>{escape(working_directory)}</WorkingDirectory>\n"
        if working_directory else ""
    )
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        # Element order here is the order Windows itself writes when it exports
        # a task, rather than the order the documentation lists them in. There is
        # no <URI>: schtasks /TN is what names the task, and a URI that disagreed
        # with it would be one more thing to be wrong.
        "  <RegistrationInfo>\n"
        f"    <Author>{escape(user)}</Author>\n"
        f"    <Description>{escape(description)}</Description>\n"
        "  </RegistrationInfo>\n"
        "  <Triggers>\n"
        "    <LogonTrigger>\n"
        "      <Enabled>true</Enabled>\n"
        f"      <UserId>{escape(user)}</UserId>\n"
        f"{delay}"
        "    </LogonTrigger>\n"
        "  </Triggers>\n"
        "  <Principals>\n"
        '    <Principal id="Author">\n'
        f"      <UserId>{escape(user)}</UserId>\n"
        "      <LogonType>InteractiveToken</LogonType>\n"
        "      <RunLevel>LeastPrivilege</RunLevel>\n"
        "    </Principal>\n"
        "  </Principals>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <AllowHardTerminate>true</AllowHardTerminate>\n"
        "    <StartWhenAvailable>false</StartWhenAvailable>\n"
        "    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>\n"
        "    <IdleSettings>\n"
        "      <StopOnIdleEnd>false</StopOnIdleEnd>\n"
        "      <RestartOnIdle>false</RestartOnIdle>\n"
        "    </IdleSettings>\n"
        "    <AllowStartOnDemand>true</AllowStartOnDemand>\n"
        "    <Enabled>true</Enabled>\n"
        "    <Hidden>false</Hidden>\n"
        "    <RunOnlyIfIdle>false</RunOnlyIfIdle>\n"
        "    <WakeToRun>false</WakeToRun>\n"
        "    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\n"
        "    <Priority>7</Priority>\n"
        "  </Settings>\n"
        '  <Actions Context="Author">\n'
        "    <Exec>\n"
        f"      <Command>{escape(command)}</Command>\n"
        f"      <Arguments>{escape(arguments)}</Arguments>\n"
        f"{working}"
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
    )


def task_description() -> str:
    return (
        "Starts dictate in your desktop session when you log in, so the "
        "transcription model is already loaded before your first sentence. "
        "Turn this off with:  dictate autostart disable"
    )


# ---------------------------------------------------------------------------
# Reading Windows' own answer back
# ---------------------------------------------------------------------------

#: 0x00041301, SCHED_S_TASK_RUNNING. Windows reports this for a run it has not
#: seen finish - which is a statement about the PROCESS THE TASK STARTED, and
#: not about dictate. The two are the same thing on a good day and are not on a
#: bad one, which is the whole subject of `reconcile` below.
STILL_RUNNING = 267009

#: Task Scheduler reports these in the "Last Result" field. They are the ones
#: worth translating; anything else is shown as the raw number.
_LAST_RESULT = {
    # dictate's own exit codes, which is what Windows records when it has run.
    0: "started normally",
    1: "dictate stopped with an error - the log below says which",
    2: "dictate could not start and said why - the log below has it",
    3: "another copy was already running, so this one did nothing",
    # Task Scheduler's own SCHED_S_* results, 0x00041300 upwards.
    267008: "the task is ready to run",
    # NOT "it is running right now". That sentence was read as "dictate is
    # running", printed above a line saying the copy in front of him had been
    # started by hand, and the two cannot both be the whole truth. What Windows
    # is actually saying is narrower, and this says only that.
    STILL_RUNNING: "Task Scheduler has not seen that run finish",
    267010: "the task is disabled",
    267011: "it has not run yet - this is normal until your next logon",
    267014: "the last run was stopped before it finished",
    # 0x80070002, the one Windows failure worth translating: it means the task
    # points at an interpreter that is no longer there.
    2147942402: "Windows could not find the program the task points at",
}


def describe_last_result(code: int | None) -> str:
    if code is None:
        return "not recorded"
    known = _LAST_RESULT.get(code)
    # Task Scheduler prints these unsigned; PowerShell and some locales sign them.
    if known is None and code < 0:
        known = _LAST_RESULT.get(code + 2 ** 32)
    return f"{code} ({known})" if known else str(code)


def parse_query_fields(text: str) -> dict[str, str]:
    """Turn `schtasks /Query /FO LIST /V` output into its `Field: value` pairs.

    Deliberately forgiving. schtasks translates its field names, so on a Windows
    that is not in English this finds little or nothing - and the callers are
    written to say "I could not read this, here it is raw" rather than to claim
    something they did not read.
    """
    fields: dict[str, str] = {}
    for line in text.splitlines():
        # Only the FIRST colon splits: "Last Run Time: 13/08/2026 09:14:02" has
        # two more in the value.
        key, sep, value = line.partition(":")
        key = key.strip()
        if not sep or not key or len(key) > 40:
            continue
        fields.setdefault(key.lower(), value.strip())
    return fields


def _parse_int(text: str) -> int | None:
    text = text.strip()
    for base in (10, 16):
        try:
            return int(text, base)
        except ValueError:
            continue
    return None


@dataclass
class AutostartStatus:
    """What Windows says about the logon task."""

    registered: bool = False
    enabled: bool | None = None
    state: str = ""
    last_run: str = ""
    last_result: int | None = None
    task_to_run: str = ""
    raw: str = ""
    #: Set when schtasks answered but we could not make sense of the answer.
    unreadable: bool = False


def parse_status(text: str, *, registered: bool) -> AutostartStatus:
    if not registered:
        return AutostartStatus(registered=False, raw=text)
    fields = parse_query_fields(text)
    status = AutostartStatus(registered=True, raw=text)
    status.state = fields.get("status", "")
    status.last_run = fields.get("last run time", "")
    status.task_to_run = fields.get("task to run", "")
    task_state = fields.get("scheduled task state", "")
    if task_state:
        status.enabled = task_state.strip().lower().startswith("enable")
    elif status.state:
        status.enabled = status.state.strip().lower() != "disabled"
    raw_result = fields.get("last result", "")
    if raw_result:
        status.last_result = _parse_int(raw_result)
    if not status.state and not status.last_run and status.enabled is None:
        status.unreadable = True
    return status


#: The `Status:` words schtasks prints, and what each means for "has the run
#: Windows started finished?". schtasks TRANSLATES this field, so a Windows that
#: is not in English falls through to `Last Result` below, and a machine where
#: neither can be read is reported as an unknown rather than as a no.
_RUNNING_STATES = ("running",)
_FINISHED_STATES = ("ready", "disabled", "could not start", "unknown")


def run_is_alive(status: AutostartStatus) -> bool | None:
    """Does Windows still count the run the task started as running?

    `None` is a real answer and the important one: it means nobody here read
    anything that says either way, and a report built on this must then say so
    instead of picking the cheerful option.

    This is deliberately NOT "is dictate running" - that question is the
    instance lock's, and answering it from here is the mistake that produced a
    status report contradicting itself.
    """
    state = status.state.strip().lower()
    if state in _RUNNING_STATES:
        return True
    if state in _FINISHED_STATES:
        return False
    if status.last_result == STILL_RUNNING:
        return True
    if status.last_result is not None:
        return False
    return None


# ---------------------------------------------------------------------------
# What the process the task started is doing
#
# Windows will say whether the RUN has finished. It will not say what it still
# has alive, and "the task is running" was being printed as though it did. The
# logon start writes its own pid into its log at the top of every block, so the
# question can be put to Windows directly: is that process still there?
# ---------------------------------------------------------------------------

#: Written by `run_at_logon` at the top of every block. Parsed back out of the
#: log rather than kept in a second file: this is the same fact, and two records
#: of one fact are two chances to disagree.
PID_MARK = "dictate starting at logon (pid "

#: What `_restart` writes when the tray's Restart item has been used. The copy
#: the task started is gone by then and a different pid is serving, so this
#: SUPERSEDES the pid above - without it the report would find a dead pid and
#: announce a disagreement over an ordinary restart.
RESTART_MARK = "a fresh copy of dictate was started (pid "

#: Written just before the modal "dictate did not start" box goes up. That box
#: keeps the process alive until somebody clicks it, with the instance lock
#: already given back - so Windows counts the run as running for exactly that
#: long while dictate is not running at all. Saying so is the only way anyone
#: reading the log afterwards can tell that state from a healthy one.
DIALOG_MARK = "waiting on a message box"


@dataclass
class LogonStart:
    """The last logon start, as it described itself, and whether it is still
    there. Every field may be empty: this is read from a log that may not exist
    yet, on a machine that may not answer."""

    pid: int | None = None
    #: When that block was written, in dictate's own format - not Windows'
    #: locale-dependent "Last Run Time", which is why the two are printed rather
    #: than compared.
    at: str = ""
    #: The `outcome:` line it wrote, if it got as far as writing one.
    outcome: str = ""
    #: It gave up and put a dialog on screen, and is alive behind it.
    showing_dialog: bool = False
    #: Is that pid still running? `None` when nobody asked or nobody could.
    alive: bool | None = None
    #: What Windows calls it, when it is still there.
    image: str = ""


def parse_logon_start(block: str) -> LogonStart:
    """Read a block of `autostart.log` back. Pure."""
    found = LogonStart()
    for line in block.splitlines():
        if BLOCK_MARK in line:
            _, _, rest = line.partition(BLOCK_MARK)
            found.at = rest.strip().strip("=").strip()
        for mark_text in (PID_MARK, RESTART_MARK):
            _, mark, tail = line.partition(mark_text)
            if not mark:
                continue
            digits = tail.split(")")[0].strip()
            if digits.isdigit():
                found.pid = int(digits)
        if DIALOG_MARK in line:
            found.showing_dialog = True
        _, sep, value = line.partition("outcome:")
        if sep:
            found.outcome = value.strip()
    return found


def read_logon_start(*, block: str | None = None, ask=None) -> LogonStart:
    """`parse_logon_start` over the log, plus Windows' answer to "is that pid
    still there?".

    `ask` is the one impure part and is injected in tests. Off Windows it is
    `None` and `alive` stays `None`: nobody here can ask, and a report that
    turned that into "it is gone" would be claiming something nobody read.
    """
    found = parse_logon_start(read_last_block() if block is None else block)
    if found.pid is None:
        return found
    if ask is None:
        ask = _ask_windows_for_name
    try:
        image = ask(found.pid)
    except Exception:  # noqa: BLE001 - a report may never be the thing that fails
        log.debug("could not ask Windows about pid %s", found.pid, exc_info=True)
        return found
    if image is None:
        return found
    found.alive = bool(image)
    found.image = image
    return found


def _ask_windows_for_name(pid: int) -> str | None:
    """The image name of `pid`, "" if it is gone, `None` if nobody could ask."""
    from . import recovery  # noqa: PLC0415 - circular at import time

    tools = recovery.platform_tools()
    return None if tools is None else tools.name_of(pid)


# ---------------------------------------------------------------------------
# Talking to schtasks
# ---------------------------------------------------------------------------


@dataclass
class ToolResult:
    code: int
    output: str


def _require_windows(action: str) -> None:
    if sys.platform != "win32":
        raise PlatformUnsupportedError(
            f"{action} registers a Windows Task Scheduler task, and this is "
            f"{sys.platform}.",
            "Run it on the Windows PC dictate is installed on.",
        )


def run_schtasks(args: list[str]) -> ToolResult:
    """Run schtasks and keep everything it said.

    Its exit code is the answer to "did that work", and its output is the
    evidence for why not - both are kept, because a failure that arrives without
    the tool's own words is a failure nobody can act on.
    """
    try:
        proc = subprocess.run(
            ["schtasks"] + args,
            capture_output=True, text=True, errors="replace", check=False,
        )
    except OSError as exc:
        raise DictateError(
            f"dictate could not run schtasks, the Windows command that manages "
            f"scheduled tasks: {exc}",
            "schtasks.exe lives in C:\\Windows\\System32. If it is not there, "
            "this is not a Windows PC dictate can start itself on.",
        ) from exc
    return ToolResult(proc.returncode, (proc.stdout or "") + (proc.stderr or ""))


def is_registered() -> bool:
    return run_schtasks(["/Query", "/TN", TASK_NAME]).code == 0


def query_status() -> AutostartStatus:
    if not is_registered():
        return AutostartStatus(registered=False)
    result = run_schtasks(["/Query", "/TN", TASK_NAME, "/FO", "LIST", "/V"])
    return parse_status(result.output, registered=result.code == 0)


def registered_command() -> str:
    """What the task Windows actually has runs, or "" if that cannot be read.

    Empty means "could not read it", never "there is nothing there" - schtasks
    translates its field names, and a caller must not turn an unreadable answer
    into a claim.
    """
    return query_status().task_to_run


def registered_or_unknown() -> bool | None:
    """Is the logon task there? `None` when that could not be answered.

    `status_lines` is the report; this is the same question asked in passing, by
    the two places that mention starting at logon while doing something else -
    the tray icon's menu and the startup banner of a console `dictate run`. Both
    of them ask *this*, and this asks `is_registered`, so the answer on the menu
    and the answer from `dictate autostart status` are the same answer.

    Three values, not two, and the third is the point: off Windows, or when
    schtasks cannot be run at all, neither caller may say "it is off" - that is a
    claim about something nobody read. They say nothing instead.
    """
    if sys.platform != "win32":
        return None
    try:
        return is_registered()
    except DictateError:
        return None


def console_hint(registered: bool | None) -> list[str]:
    """What a console `dictate run` says about starting at logon, or nothing.

    The product owner read that startup banner many times over one evening while
    asking for exactly this feature, and it never mentioned it.

    Only when the answer is a definite no: a copy that already starts at logon
    has nothing to learn from this, and an unreadable answer is not a licence to
    guess. It is four lines, said once at startup and never again - the console
    is not somewhere to nag from, and there are now two other places (the tray
    menu, the end of setup) where the same offer is made.
    """
    if registered is not False:
        return []
    return [
        "this window has to stay open for the hotkey to work - but it does not",
        "have to be this way. dictate can start when you log in, with no window",
        "at all, and the icon by the clock is how you stop it:",
        "  dictate autostart enable",
        "which is also on that icon's menu, as \"Start when I log in\".",
    ]


# ---------------------------------------------------------------------------
# Starting it now, rather than at the next logon
# ---------------------------------------------------------------------------

#: How long to wait for the copy just started to take the instance lock. It
#: takes it at the very top of `run_at_logon`, before any of the slow work, so
#: this is generous rather than tight - and running out of it is reported as
#: "not confirmed", never as a failure.
START_CONFIRM_S = 8.0
START_POLL_S = 0.25


@dataclass
class StartOutcome:
    """What the immediate start did. Deliberately separate from whether the
    logon task registered: half of this working is not this working."""

    #: "started", "already-running", "failed" or "unconfirmed".
    state: str
    pid: int | None = None
    #: How the copy that was already running describes itself.
    holder: str = ""
    #: The real reason, in the words of whatever refused. Never a guess.
    detail: str = ""
    #: How long "unconfirmed" actually waited, so the report can say the number
    #: that was used rather than the one in the constant above.
    waited_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.state in ("started", "already-running")


def start_now(config_path: str | None = None, executable: str | None = None, *,
              wait_s: float = START_CONFIRM_S, poll_s: float = START_POLL_S,
              spawn=None, running=None, sleep=time.sleep,
              monotonic=time.monotonic) -> StartOutcome:
    """Start the windowless copy now. Never starts a second one.

    Enabling something and seeing nothing happen is the same defect as shipping
    it as a command nobody finds, so `enable` does this too. Three things about
    it are load-bearing:

    * **one copy, still.** A running dictate owns the hotkey and the
      transcription port, and `instance.running_instance()` is asked *first*: if
      there is one, nothing is started and the caller says so. This is also what
      the tray's own toggle hits, because the tray is inside a running copy.
    * **the same copy the logon task starts**, through `pythonw.exe` and
      `run --autostart`: no console to flash, and `LogonLog` in place of the
      stdout that a windowless process does not have. `spawn_detached` is what
      makes it outlive the shell, terminal or tray that asked for it.
    * **it claims only what it watched happen.** "started" means the new copy
      took the instance lock while we watched; a copy that stopped straight away
      is a failure carrying its own exit code; and running out of patience is
      `unconfirmed`, which names where the answer is rather than inventing one.
    """
    from . import recovery  # noqa: PLC0415 - circular at import time

    running = instance.running_instance if running is None else running
    holder = running()
    if holder is not None:
        return StartOutcome("already-running", holder=holder.describe())

    argv = recovery.relaunch_argv(config_path, str(executable) if executable else None,
                                  autostart=True)
    kwargs = {"spawn": spawn} if spawn is not None else {}
    try:
        proc = recovery.spawn_detached(argv, **kwargs)
    except OSError as exc:
        return StartOutcome("failed", detail=str(exc))

    pid = getattr(proc, "pid", None)
    deadline = monotonic() + wait_s
    while True:
        if running() is not None:
            return StartOutcome("started", pid=pid)
        code = proc.poll() if hasattr(proc, "poll") else None
        if code is not None:
            # Its own account of why is in the logon log rather than here:
            # there is no console for it to have told us through, which is the
            # whole reason that log exists.
            return StartOutcome(
                "failed", pid=pid,
                detail=f"it started and stopped again at once, with exit code "
                       f"{code}. What it said about that is in {log_path()}")
        if monotonic() >= deadline:
            return StartOutcome("unconfirmed", pid=pid, waited_s=wait_s)
        sleep(poll_s)


def start_lines(outcome: StartOutcome) -> list[str]:
    """What to tell him about the immediate start, and nothing more than that."""
    if outcome.state == "already-running":
        return [
            f"It is already running ({outcome.holder}), so nothing new was "
            "started -",
            "one copy at a time is the rule, or two of them fight over the "
            "hotkey.",
        ]
    if outcome.state == "started":
        where = f" (process {outcome.pid})" if outcome.pid else ""
        return [
            f"It is also running NOW{where}, with no window of its own: the "
            "icon by the",
            "clock is where it lives, and the hotkey works from this moment.",
        ]
    if outcome.state == "unconfirmed":
        where = f" (process {outcome.pid})" if outcome.pid else ""
        return [
            f"It was started now{where}, but it had not finished starting "
            f"{outcome.waited_s:.0f} seconds later,",
            "so dictate cannot tell you here whether it came up. What it says "
            "about itself:",
            "  dictate autostart status",
            f"and anything that went wrong is written to {log_path()}.",
        ]
    return [
        "It could NOT be started now, and this is what stopped it:",
        f"  {outcome.detail or 'no reason was given'}",
        "The logon task above is registered and unaffected - it will still "
        "start at",
        "your next logon. To start it now instead, and see the reason in full:",
        "  dictate run",
    ]


def enable(cfg: Config, *, config_path: str | None = None,
           executable: str | None = None, start: bool = True) -> list[str]:
    """Register the logon task, start the windowless copy, and say what each of
    those two did. Returns the lines to show the user.

    `start=False` is for a caller that only wants the registration; nothing
    ships passing it, and the three ways he reaches this all start it.
    """
    _require_windows("`dictate autostart enable`")
    user = interactive_user()
    command = windowless_python(executable)
    arguments = task_arguments(config_path)
    xml = task_xml(
        user=user,
        command=str(command),
        arguments=arguments,
        working_directory=os.environ.get("USERPROFILE", ""),
        delay_s=cfg.autostart.logon_delay_s,
        description=task_description(),
    )

    # schtasks reads this file as UTF-16; a UTF-8 one is rejected as malformed.
    path = instance.state_dir() / "logon-task.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(xml, encoding="utf-16")
    try:
        result = run_schtasks(["/Create", "/TN", TASK_NAME, "/XML", str(path), "/F"])
    finally:
        try:
            path.unlink()
        except OSError:
            pass

    # The same rule the installer learned the hard way: the exit code is not the
    # authority on success - whether the thing is now there is. Ask Windows.
    if not is_registered():
        raise DictateError(
            "Windows did not accept the task that starts dictate when you log in "
            f"(schtasks stopped with error code {result.code}).\n"
            f"It said:\n{result.output.strip() or '(nothing)'}",
            "If it mentions access being denied, open PowerShell with "
            "'Run as administrator' and run this once more - registering the "
            "task can need it, though running dictate afterwards never does.\n"
            "dictate still starts normally with `dictate run` either way.",
        )

    # Registered is not the same as registered with what we just wrote: /Create
    # could have failed over a task of the same name that was already there, and
    # "it will now start" would then be a claim about somebody else's task.
    in_place = registered_command()
    if in_place and str(command) not in in_place:
        raise DictateError(
            "There is already a scheduled task called "
            f"'{TASK_NAME}' and Windows would not replace it "
            f"(schtasks stopped with error code {result.code}). It runs:\n"
            f"  {in_place}\n"
            f"rather than what dictate just asked for:\n  {command} {arguments}",
            "Remove the old one and try again:\n"
            f"  dictate autostart disable\n"
            "  dictate autostart enable\n"
            "If that will not do it either, delete the task in Task Scheduler by "
            "hand and run enable once more.",
        )

    lines = [
        f"dictate will now start when you log in, about "
        f"{cfg.autostart.logon_delay_s} seconds after you reach the desktop.",
        "",
        f"  runs:      {command} {arguments}",
        f"  as:        {user}, in your own desktop session, without administrator rights",
        f"  task:      Task Scheduler Library -> {TASK_NAME}",
        f"  its log:   {log_path()}",
        "",
    ]

    # The registration is finished and reported above; what follows is a second
    # outcome and is written as one. Turning this on and watching nothing happen
    # is why it is done at all - but a start that failed may never be printed
    # under a sentence that says everything worked.
    if start:
        outcome = start_now(config_path, str(command))
        lines += start_lines(outcome)
        lines.append("")

    lines += _memory_cost_lines(cfg)
    lines += [
        "",
        "  dictate autostart disable",
        "",
        "Check on it any time with `dictate autostart status`, and stop the copy",
        "that is running now with `dictate stop`.",
    ]
    return lines


def _memory_cost_lines(cfg: Config) -> list[str]:
    """What leaving this on actually costs the graphics card.

    Which depends on `[whisper] idle_release_minutes`, so it is read rather than
    assumed: dictate gives the model's ~1.6 GB back on its own after that long
    without dictating, and only with the release turned off does leaving this on
    mean holding that memory all day.
    """
    from .engines.residency import minutes_text  # noqa: PLC0415

    idle = cfg.whisper.idle_release_minutes
    if idle > 0:
        return [
            "The transcription model takes about 1.6 GB of graphics memory, but",
            f"dictate hands it back after {minutes_text(idle * 60)} without dictating and "
            "takes it",
            "again when you next press the hotkey. So leaving this on costs your",
            "card nothing between dictation sessions, and there is no reason to",
            "turn it off for a game. If you want it gone anyway:",
        ]
    return [
        "[whisper] idle_release_minutes is 0 in your config, so the model stays",
        "loaded for as long as you are logged in - about 1.6 GB of graphics",
        "memory a game cannot have. Setting it to 5 gives that memory back by",
        "itself after five idle minutes. Otherwise, to stop starting at logon:",
    ]


def disable() -> list[str]:
    """Remove the logon task, and LEAVE the copy that is running alone.

    Enabling starts one, so the symmetric thing would be for disabling to stop
    it. It deliberately does not. Turning this off is a statement about future
    logons, and he can be dictating into it at the instant he clicks the tray
    item: stopping it there would drop the audio and the words with it, to
    answer a question he did not ask. Nothing is lost by leaving it - it is one
    `dictate stop` away, which is the one command every failure message in this
    product already names - whereas a sentence ended by a menu tick is gone.

    What that costs is a running copy he might not expect, so the copy is named
    below, along with the command that stops it. Saying nothing here would be
    the actual defect.
    """
    _require_windows("`dictate autostart disable`")
    if not is_registered():
        return ["dictate was not set to start when you log in. Nothing to undo."]

    result = run_schtasks(["/Delete", "/TN", TASK_NAME, "/F"])
    if is_registered():
        raise DictateError(
            "The task that starts dictate when you log in could not be removed "
            f"(schtasks stopped with error code {result.code}).\n"
            f"It said:\n{result.output.strip() or '(nothing)'}",
            "Open PowerShell with 'Run as administrator' and run this instead:\n"
            f"  schtasks /Delete /TN {TASK_NAME} /F\n"
            "Or open Task Scheduler, find "
            f"'{TASK_NAME}' in the library, and delete it there.",
        )

    lines = ["dictate will no longer start when you log in. The task is gone."]
    log = log_path()
    if log.exists():
        lines += [
            "",
            f"The record of previous logon starts is still at {log}.",
            "Nothing reads it any more - delete it if you like.",
        ]
    holder = instance.running_instance()
    if holder is not None:
        lines += [
            "",
            f"dictate is still RUNNING right now ({holder.describe()}), and has "
            "been left",
            "that way on purpose - you could be in the middle of a sentence. It "
            "keeps",
            "running until you log out, or until you say:",
            "  dictate stop",
        ]
    return lines


# ---------------------------------------------------------------------------
# `dictate autostart status --why`: what is actually alive
#
# One line, no paths to edit and no sequence to get right, because the person
# who has to run it is not a developer and is sometimes reading this off a
# phone. It ends a specific argument: when the logon task is still counted as
# running and the copy serving him is a different one, there are three things
# that can be true, and they need three different fixes.
#
#   two copies of dictate are alive at once     -> the instance lock failed
#   the logon copy died and left something      -> an orphan holds the port
#   the logon copy is alive without serving     -> it never became usable
#
# From inside one process those look identical. From a list of what Windows has
# running, they do not: a second whisper-server means two copies are really
# transcribing, no dictate process with a whisper-server still on the port is
# the orphan, and the logon pid alive while another pid holds the lock is the
# third.
#
# It reports and changes nothing. Nothing here ends a process, writes a file or
# touches the task - it may be run at any time, including in the middle of a
# sentence.
# ---------------------------------------------------------------------------

#: What to list. Image names only - `tasklist` does not print command lines, so
#: this cannot tell a copy of dictate from any other Python program on the PC
#: and the report says so rather than implying otherwise. whisper-server is the
#: one that is unambiguous, and it is the one that settles the question.
IMAGES_WORTH_LISTING = ("pythonw.exe", "python.exe", "dictate.exe",
                        "whisper-server.exe")


@dataclass
class Evidence:
    """What Windows has running, for a question that cannot be answered from
    inside one process."""

    #: Empty when Windows could be asked; otherwise, in plain words, why not.
    unavailable: str = ""
    #: (image name, pid), in the order of `IMAGES_WORTH_LISTING`.
    processes: list[tuple[str, int]] = field(default_factory=list)
    port: int = 0
    #: (pid, image name) for whoever holds the transcription port.
    port_holders: list[tuple[int, str]] = field(default_factory=list)
    lock_file: str = ""
    #: The record at the front of the lock file, whoever wrote it.
    record: instance.Holder | None = None
    #: schtasks' whole answer, unparsed, because a report that could not read it
    #: still has to carry it.
    schtasks_raw: str = ""


def gather_evidence(cfg: Config | None, reading: Reading) -> Evidence:
    """Ask Windows what is running. Impure and deliberately dull."""
    from . import recovery  # noqa: PLC0415 - circular at import time

    found = Evidence(lock_file=str(instance.lock_path()),
                     record=instance.read_record(),
                     schtasks_raw=reading.status.raw)
    if cfg is not None:
        found.port = cfg.whisper.port

    tools = recovery.platform_tools()
    if tools is None:
        found.unavailable = (
            f"this is {sys.platform}, and what is running is a question only the "
            "Windows PC can answer")
        return found

    for image in IMAGES_WORTH_LISTING:
        try:
            pids = tools.pids_named(image)
        except DictateError as exc:
            found.unavailable = exc.message
            return found
        except Exception as exc:  # noqa: BLE001 - a report may never be the fault
            log.debug("listing %s raised", image, exc_info=True)
            found.unavailable = str(exc)
            return found
        found.processes += [(image, pid) for pid in sorted(pids)]

    if found.port:
        try:
            for row in tools.listeners(found.port):
                found.port_holders.append((row.pid, tools.name_of(row.pid)))
        except Exception:  # noqa: BLE001
            log.debug("asking who holds port %s raised", found.port, exc_info=True)
    return found


def evidence_lines(reading: Reading) -> list[str]:
    """Render the evidence, then say what it establishes. Pure.

    The two halves are separate on purpose. The list is what he pastes back and
    is true whatever anybody concludes from it; the reading underneath it is
    dictate's opinion, and is allowed to be "these numbers do not settle it".
    """
    found = reading.evidence
    if found is None:
        return []
    lines = ["-" * 70,
             "What is actually running (this is the part to copy and send on):",
             ""]
    if found.unavailable:
        lines.append(f"  not asked: {found.unavailable}")
        return lines + ["", *_evidence_reading(reading)]

    if not found.processes:
        lines.append("  nothing named python.exe, pythonw.exe, dictate.exe or "
                     "whisper-server.exe")
    width = max((len(str(pid)) for _, pid in found.processes), default=1)
    for image, pid in found.processes:
        marks = []
        if reading.logon.pid == pid:
            marks.append("the logon task started this one")
        if reading.holder is not None and reading.holder.pid == pid:
            marks.append("holds dictate's lock")
        note = f"  <- {', and '.join(marks)}" if marks else ""
        lines.append(f"  {image:<20} pid {pid:<{width}}{note}")

    lines.append("")
    if not found.port:
        lines.append("  transcription port: not checked - no config was loaded")
    elif not found.port_holders:
        lines.append(f"  transcription port {found.port}: free")
    else:
        for pid, name in found.port_holders:
            lines.append(f"  transcription port {found.port}: held by pid {pid} "
                         f"({name or 'no name'})")

    lines.append(f"  lock file: {found.lock_file}")
    holding = reading.holder.describe() if reading.holder is not None else "nobody"
    lines.append(f"  holding it now: {holding}")
    # Who LAST held it, which is a different question and only worth printing
    # when it has a different answer - a lock nobody holds still names the copy
    # that wrote the record, and that is often the whole story.
    if found.record is not None and found.record.describe() != holding:
        lines.append(f"  its record says: {found.record.describe()}")

    if found.schtasks_raw.strip():
        lines.append("")
        lines.append("  what schtasks said, in full:")
        for raw in found.schtasks_raw.strip().splitlines():
            lines.append(f"  | {raw}")
    return lines + ["", *_evidence_reading(reading)]


def _evidence_reading(reading: Reading) -> list[str]:
    """What the list above establishes - and only that.

    Three findings are possible and a fourth is "these numbers do not settle
    it", which is printed as often as it is true. Naming a cause this has not
    established is the one thing it must never do: he acts on what this says,
    and he has already spent a morning checking a proxy because a message
    picked the likeliest explanation instead of the one it had evidence for.
    """
    found = reading.evidence
    logon, holder = reading.logon, reading.holder
    lines = ["What that says:"]
    if found is None or found.unavailable:
        return lines + [
            "  nothing yet - the list above is empty, so there is nothing to "
            "read.",
        ]

    servers = [pid for image, pid in found.processes
               if image.lower() == "whisper-server.exe"]
    maybe_dictate = [pid for image, pid in found.processes
                     if image.lower() != "whisper-server.exe"]
    said = False

    if len(servers) > 1:
        said = True
        lines += [
            f"  * there are {len(servers)} whisper-server processes "
            f"({', '.join(str(p) for p in servers)}).",
            "    One copy of dictate runs at most one of those, so two copies of "
            "dictate are",
            "    really running and the lock that is supposed to make that "
            "impossible did not.",
            "    `dictate stop` clears them; send this whole report on.",
        ]
    if logon.pid is not None and logon.alive is True and holder is not None \
            and holder.pid != logon.pid:
        said = True
        lines += [
            f"  * the process the logon task started (pid {logon.pid}) is alive "
            "and is NOT the",
            f"    copy holding the lock (pid {holder.pid}). It started and did "
            "not become a",
            "    working dictate.",
        ]
        if logon.showing_dialog:
            lines.append('    Its log says it is waiting on the "dictate did not '
                         'start" message box -')
            lines.append("    closing that box is what lets it end.")
        elif "gave up" in logon.outcome:
            lines.append("    Its log says it gave up, and a logon start that "
                         "gives up shows a message")
            lines.append('    box and waits. Look for a window called "dictate '
                         'did not start".')
        else:
            lines.append("    Its log does not say why. The block above is the "
                         "thing to send on.")
    if logon.pid is not None and logon.alive is False and not servers \
            and holder is None:
        said = True
        lines += [
            f"  * the process the logon task started (pid {logon.pid}) is gone, "
            "nothing holds",
            "    the lock, and there is no whisper-server left. dictate is simply "
            "not running;",
            "    `dictate run` starts it, and the log block above says why the "
            "logon start ended.",
        ]
    if logon.pid is not None and logon.alive is False and servers:
        said = True
        lines += [
            f"  * the process the logon task started (pid {logon.pid}) is gone, "
            "but a",
            f"    whisper-server ({', '.join(str(p) for p in servers)}) is still "
            "here. That is the",
            "    orphan: it holds the transcription port and the graphics memory "
            "with nothing",
            "    above it. `dictate stop` clears it.",
        ]

    if not said:
        lines.append("  nothing in the list settles it by itself. Send the whole "
                     "of this on.")
    lines += [
        "",
        f"  ({len(maybe_dictate)} process(es) above could be a copy of dictate. "
        "That is an image",
        "   name only - tasklist does not print command lines, so anything else "
        "on this PC",
        "   written in Python is in that list too. The whisper-server count and "
        "the lock are",
        "   what actually settle it.)",
    ]
    return lines


# ---------------------------------------------------------------------------
# Putting the two readings side by side
#
# `dictate autostart status` reads two independent things - what Windows says
# about the TASK, and what the instance lock says about the COPY THAT IS
# RUNNING - and used to print them one after the other with nothing between.
# On the morning this was written, that produced:
#
#     last result:   267009 (it is running right now)
#     running now:   YES - process 22188, ... , started by hand
#
# Both readings were correct. Neither sentence was wrong on its own. Together
# they said dictate had started itself and dictate had not, and the report did
# not notice - which is the same defect as an installer announcing an SDK it
# had not installed, and it costs the same thing: the next message this product
# prints is believed a little less.
#
# So the two readings are now compared, and the comparison is the report.
# ---------------------------------------------------------------------------

#: The phrase that introduces a disagreement. One constant because a test greps
#: for it: any pair of readings that cannot both be true has to reach it.
DISAGREE_MARK = "these do not agree:"

#: The one line he pastes when this cannot be settled from here.
WHY_COMMAND = "dictate autostart status --why"


@dataclass
class Reconciliation:
    """What the two readings amount to when they are put side by side."""

    #: A short name for the state, for tests and for nothing else.
    verdict: str
    lines: list[str] = field(default_factory=list)

    @property
    def disagrees(self) -> bool:
        return any(DISAGREE_MARK in line for line in self.lines)


def _which(pid: int | None) -> str:
    return f"process {pid}" if pid else "the copy that is running"


def _how_started(holder: instance.Holder | None) -> str:
    """In his words, not the record's. `Holder.started_by` is "hand" or
    "logon"; "process 22188, hand" is not a sentence."""
    if holder is None:
        return ""
    if holder.started_by == "hand":
        return "started by hand"
    if holder.started_by == "logon":
        return "started at logon"
    return "and dictate does not know how it was started"


def _first_sentence(text: str, limit: int = 60) -> str:
    """The head of an `outcome:` line, for quoting inside a paragraph. The
    whole of it is in the log block below and is printed there."""
    head = text.split(".")[0].strip()
    return head if len(head) <= limit else head[:limit].rstrip() + "…"


def reconcile(status: AutostartStatus, logon: LogonStart,
              holder: instance.Holder | None) -> Reconciliation:
    """Compare Windows' answer about the task with the lock's answer about
    dictate, and say what the pair of them establishes. Pure.

    The rule it is built on: **it may only report what these readings settle.**
    Where they settle nothing - because schtasks was not readable, or because
    nobody could ask Windows whether a pid is still there - it says that, and
    names the one command that would settle it. "I do not know" is a real
    answer here; a cheerful guess is what sent him looking at his internet
    connection for a file lock.
    """
    if not status.registered:
        return Reconciliation("off")

    alive = run_is_alive(status)
    # A pid on its own is not an identity: Windows reuses them, and the pid in
    # the log belongs to a run that may have ended hours ago. So the two are
    # only the same copy when the lock's own record agrees about HOW it was
    # started - a holder that says "hand" against the logon pid is a collision,
    # and is reported as an unknown rather than as a match.
    reused = (logon.pid is not None and holder is not None
              and holder.pid == logon.pid and holder.started_by == "hand")
    matched = (logon.pid is not None and holder is not None
               and holder.pid == logon.pid and not reused)
    started_at = f" started at {status.last_run}" if status.last_run else ""

    if reused and alive is not False:
        return Reconciliation("pid-reused", [
            DISAGREE_MARK,
            f"  the logon task's log names {_which(logon.pid)}, and a process "
            "with that number",
            "  is holding dictate's lock - but its record says it was started by "
            "hand.",
            "  Windows reuses process numbers, so those may be two different "
            "programs and",
            "  dictate will not treat them as one.",
        ] + _ask_for_evidence())

    if matched:
        if alive is False:
            return Reconciliation("same-copy-uncounted", [
                f"the copy answering the hotkey now ({_which(logon.pid)}) IS the "
                "one the logon",
                "task started, though Windows no longer counts that run as "
                "running. dictate is",
                "working; only Task Scheduler's bookkeeping is behind.",
            ])
        return Reconciliation("same-copy", [
            f"the copy answering the hotkey now ({_which(logon.pid)}) is the one "
            "the logon task",
            "started - these two readings agree.",
        ])

    # The process reading leads whenever it is known, and it leads whether or
    # not Windows has finished counting the run: a process the task started,
    # alive, that is not the copy answering the hotkey is the finding either
    # way. Windows' bookkeeping only decides how the first sentence opens.
    if logon.alive is True and not matched:
        what = f" ({logon.image})" if logon.image else ""
        if holder is None:
            opening = (
                [f"  Windows says the run it{started_at} has not finished, and "
                 "the process it",
                 f"  started ({_which(logon.pid)}{what}) IS still there."]
                if alive is True else
                [f"  the process the logon task started ({_which(logon.pid)}"
                 f"{what}) is still there,",
                 "  though Windows no longer counts that run as running."])
            return Reconciliation("alive-but-serving-nobody", [
                DISAGREE_MARK, *opening,
                "  Nothing holds dictate's lock, so DICTATE IS NOT RUNNING. That",
                "  process is alive without being a working dictate.",
            ] + _dialog_note(logon) + _ask_for_evidence())
        return Reconciliation("not-the-copy", [
            DISAGREE_MARK,
            f"  the process the logon task started ({_which(logon.pid)}{what}) "
            "is still",
            "  running, and it is NOT the copy answering the hotkey - that one is",
            f"  {_which(holder.pid)}, {_how_started(holder)}. Whatever the logon "
            "task still has",
            "  alive, it is not what is serving you.",
        ] + _dialog_note(logon) + _ask_for_evidence())

    if alive is True and logon.alive is False:
        return Reconciliation("process-gone-run-alive", [
            DISAGREE_MARK,
            f"  Windows says the run it{started_at} has not finished, but the "
            "process it",
            f"  started ({_which(logon.pid)}) is GONE. So something ELSE that run "
            "started is",
            "  still alive - that is what Task Scheduler is still counting. A "
            "whisper-server",
            "  with no dictate above it is the one that has happened before, and",
            "  `dictate stop` clears it.",
        ] + _ask_for_evidence())

    if alive is True and holder is not None and holder.started_by == "logon":
        # Not settled - the pids could not be compared - but not a disagreement
        # either: the copy that is running says it was started at logon, and so
        # does Windows. Crying wolf here would cost the paragraph above the
        # attention it needs on the day it is right.
        return Reconciliation("probably-same-copy", [
            f"the copy answering the hotkey now ({_which(holder.pid)}) says it "
            "was started at logon,",
            "and Windows says that run has not finished. dictate could not check "
            "that they are",
            "the same process, but nothing here disagrees.",
        ])

    if alive is True:
        # Either the log has no pid in it (a task that has not run since this
        # version), or nobody could ask Windows about it. Both are unknowns and
        # neither is licence to say the logon copy is fine.
        head = [DISAGREE_MARK,
                f"  Windows says the run it{started_at} has not finished, and "
                "dictate could not"]
        if holder is None:
            tail = [
                "  establish what that run still has alive. Nothing holds "
                "dictate's lock, so",
                "  DICTATE IS NOT RUNNING - and something from that run may be.",
            ]
        else:
            tail = [
                "  establish what that run still has alive. The copy answering "
                "the hotkey now",
                f"  ({_which(holder.pid)}) was {_how_started(holder)}; whether "
                "that is the same",
                "  process is not settled here.",
            ]
        return Reconciliation("cannot-tell", head + tail + _ask_for_evidence())

    if alive is None:
        return Reconciliation("run-unknown", [
            "Windows did not say whether the run it started has finished, so "
            "nothing here",
            "claims either way. Its own words are above.",
        ])

    if holder is not None and holder.started_by == "logon":
        return Reconciliation("logon-copy-uncounted", [
            f"the copy answering the hotkey now ({_which(holder.pid)}) says it was "
            "started at",
            "logon, and Windows no longer counts that run as running. That is what "
            "a restart",
            "from the icon's menu leaves behind, and it is not a fault.",
        ])

    return Reconciliation("quiet")


def _dialog_note(logon: LogonStart) -> list[str]:
    """What the logon start's own log says about why it is still there.

    Three answers, and the third is "it did not say". The middle one is for a
    log written before this version, which recorded giving up but not that it
    then waited on a dialog.
    """
    if logon.showing_dialog:
        return [
            '  Its own log says it is waiting on the "dictate did not start" '
            "message box.",
            "  That box is what is keeping it alive: it is modal, so the process "
            "sits in it",
            "  until somebody clicks it, and Windows counts the logon task as "
            "running for",
            "  exactly that long. Close the box and this clears.",
        ]
    if "gave up" in logon.outcome:
        return [
            f"  Its own log says it {_first_sentence(logon.outcome)}, and a logon "
            "start that gives",
            "  up then puts a message box on screen and waits for a click. Look "
            'for a window',
            '  called "dictate did not start" - closing it lets that process end.',
        ]
    return ["  Its own log does not say why it is still there; the log is below."]


def _ask_for_evidence() -> list[str]:
    return [
        "",
        "  what is actually alive, in one line you can paste:",
        f"    {WHY_COMMAND}",
    ]


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


@dataclass
class Reading:
    """Everything `dictate autostart status` looked at. Gathered in one place
    so that rendering it is pure and can be driven through every combination
    off Windows - including the ones nobody here can produce."""

    platform: str = "win32"
    status: AutostartStatus = field(default_factory=AutostartStatus)
    logon: LogonStart = field(default_factory=LogonStart)
    holder: instance.Holder | None = None
    log_file: str = ""
    block: str = ""
    evidence: Evidence | None = None


def take_reading(*, why: bool = False, cfg: Config | None = None) -> Reading:
    """Ask everything, decide nothing."""
    reading = Reading(platform=sys.platform, log_file=str(log_path()),
                      block=read_last_block(max_lines=200 if why else 40))
    if sys.platform == "win32":
        reading.status = query_status()
    reading.logon = read_logon_start(block=reading.block)
    reading.holder = instance.running_instance()
    if why:
        reading.evidence = gather_evidence(cfg, reading)
    return reading


def render(reading: Reading) -> list[str]:
    """The report, from a reading and nothing else. Pure."""
    lines: list[str] = []
    status = reading.status

    if reading.platform != "win32":
        lines.append(f"start at logon:  not available on {reading.platform} - it "
                     "is a Windows scheduled task")
    else:
        if not status.registered:
            lines.append("start at logon:  OFF")
            lines.append("                 turn it on with `dictate autostart enable`")
        elif status.enabled is False:
            lines.append("start at logon:  REGISTERED BUT DISABLED")
            lines.append("                 something disabled the task in Task "
                         "Scheduler; re-enable it there,")
            lines.append("                 or run `dictate autostart disable` then "
                         "`enable` to rebuild it")
        else:
            lines.append("start at logon:  ON")
        if status.registered:
            lines.append(f"  task:          Task Scheduler Library -> {TASK_NAME}")
            if status.task_to_run:
                lines.append(f"  runs:          {status.task_to_run}")
            if status.state:
                lines.append(f"  state:         {status.state}")
            if status.last_run:
                lines.append(f"  last started:  {status.last_run}")
            if status.last_result is not None:
                lines.append(f"  last result:   {describe_last_result(status.last_result)}")
            lines += _its_process_lines(reading.logon)
            if status.unreadable:
                lines.append("  (Windows answered in a form dictate could not read - "
                             "its own words follow)")
                for raw in status.raw.strip().splitlines():
                    lines.append(f"  | {raw}")

    lines.append("")
    holder = reading.holder
    if holder is None:
        lines.append("running now:     NO")
        lines.append("                 start it with `dictate run`")
    else:
        lines.append(f"running now:     YES - {holder.describe()}")
        lines.append("  stop it with:  dictate stop")

    if reading.platform == "win32":
        verdict = reconcile(status, reading.logon, holder)
        if verdict.lines:
            lines.append("")
            lines += verdict.lines

    lines.append("")
    lines.append(f"the logon log:   {reading.log_file}")
    if reading.block:
        lines.append("")
        lines.append("What happened the last time it started at logon:")
        for line in reading.block.splitlines():
            lines.append(f"  {line}")
    elif reading.platform == "win32":
        lines.append("                 (nothing in it yet - it is written the first "
                     "time dictate starts at logon)")

    if reading.evidence is not None:
        lines.append("")
        lines += evidence_lines(reading)
    return lines


def _its_process_lines(logon: LogonStart) -> list[str]:
    """Whether the process the task started is still there.

    This is the line the old report could not print, and its absence is what
    let "the task is running" stand in for "dictate is running". Windows says
    whether the RUN has finished; the logon start writes its own pid down; so
    the process itself can be asked about by name.
    """
    if logon.pid is None:
        return []
    where = f"pid {logon.pid}"
    when = f", started at logon {logon.at}" if logon.at else ""
    if logon.alive is True:
        what = f" ({logon.image})" if logon.image else ""
        return [f"  its process:   {where}{what}{when} - STILL RUNNING"]
    if logon.alive is False:
        return [f"  its process:   {where}{when} - gone"]
    return [f"  its process:   {where}{when} - dictate could not ask Windows "
            "whether it is still there"]


def status_lines(*, why: bool = False, cfg: Config | None = None) -> list[str]:
    """The answer to all three of "is it on?", "is it running?" and "why did it
    not start?", in one command, because those are the questions an always-on
    thing owning a global hotkey has to be able to answer.

    It claims nothing it did not read: a `schtasks` whose output it cannot parse
    is reported as unparsed output, not as an absence, and two readings that
    cannot both be true are printed as a disagreement rather than as a fact.
    """
    return render(take_reading(why=why, cfg=cfg))


# ---------------------------------------------------------------------------
# The logon log
# ---------------------------------------------------------------------------


def last_block(text: str) -> str:
    """The most recent logon start's block out of the log."""
    index = text.rfind(BLOCK_MARK)
    return text if index < 0 else text[index:].rstrip()


def read_last_block(*, path: Path | None = None, max_lines: int = 40) -> str:
    path = path or log_path()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    lines = last_block(text).splitlines()
    if len(lines) > max_lines:
        # Keep the first TWO lines - when this happened, and the pid it wrote
        # down, which is what `parse_logon_start` reads and what lets the report
        # ask Windows whether that process is still there. Dropping the second
        # one was silently turning a long block into an unanswerable one. The
        # end is kept because that is where the reason and the remedy are; the
        # middle is whisper.cpp being chatty.
        head, rest = lines[:2], lines[2:]
        keep = rest[-(max_lines - 3):]
        lines = head + [
            f"... {len(rest) - len(keep)} line(s) not shown; the whole file "
            f"is at {path} ..."] + keep
    return "\n".join(lines)


class LogonLog:
    """Where a logon start says what happened to it.

    Under `pythonw.exe` there is no console at all - `sys.stdout` and
    `sys.stderr` are `None`, and a bare `print()` would raise. So this both
    collects dictate's own output and stands in for those two streams, which is
    what stops a logon failure disappearing into a window that was never there.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or log_path()
        self._handle = None
        self._saved: tuple = ()

    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._roll_if_large()
        self._handle = open(self.path, "a", encoding="utf-8", errors="replace",
                            buffering=1)
        self._saved = (sys.stdout, sys.stderr)
        sys.stdout = self._handle
        sys.stderr = self._handle

    def _roll_if_large(self) -> None:
        try:
            if self.path.stat().st_size <= LOG_MAX_BYTES:
                return
            previous = self.path.with_suffix(self.path.suffix + ".1")
            try:
                previous.unlink()
            except OSError:
                pass
            self.path.rename(previous)
        except OSError:
            pass

    def write(self, message: str = "") -> None:
        if self._handle is None:
            return
        try:
            stamp = time.strftime("%H:%M:%S")
            for line in str(message).splitlines() or [""]:
                self._handle.write(f"{stamp}  {line}\n")
        except (OSError, ValueError):
            pass

    def start_block(self) -> None:
        self.write("")
        self.write(f"{BLOCK_MARK} {time.strftime('%Y-%m-%d %H:%M:%S')} ===")

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if self._saved:
            sys.stdout, sys.stderr = self._saved
            self._saved = ()
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass

    def __enter__(self) -> LogonLog:
        self.open()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


# ---------------------------------------------------------------------------
# The logon start itself
# ---------------------------------------------------------------------------


def _will_notify(cfg_autostart: AutostartConfig) -> bool:
    """Is a message box going to go up? Asked separately from showing one,
    because what the log has to say about this process is decided by the
    answer: with a box on screen it stays alive until somebody clicks, and
    without one it ends here."""
    return bool(cfg_autostart.notify_on_failure) and sys.platform == "win32"


def dialog_lines(pid: int) -> list[str]:
    """What the log says while the modal box is up.

    Written because nothing said it, and the silence was readable as health.
    The box is modal: this process sits in it until it is clicked, with the
    instance lock already given back - so for exactly that long Windows counts
    the logon task as Running while dictate is not running at all, and
    `dictate autostart status` had no way to tell that apart from a copy that
    started fine. `DIALOG_MARK` is what the report greps for.
    """
    return [
        f"{DIALOG_MARK}: dictate could not start, and this process (pid {pid}) "
        "stays",
        "  alive until that box is closed. dictate is NOT running in the "
        "meantime, and",
        "  Task Scheduler goes on counting the logon task as Running for as "
        "long as",
        "  the box is on screen.",
    ]


def _notify_failure(cfg_autostart: AutostartConfig, summary: str) -> None:
    """Put a failure where he will see it without having been told to look.

    The log always has the detail; this is so he learns at all. It can never be
    the reason a start fails, so everything about it is wrapped.
    """
    if not _will_notify(cfg_autostart):
        return
    if len(summary) > 1200:
        summary = summary[:1200].rstrip() + "\n..."
    try:
        from .platform.windows.notify import show_error  # noqa: PLC0415 - Windows only

        show_error(
            "dictate did not start",
            f"{summary}\n\nThe full detail is in:\n{log_path()}\n\n"
            "Once that is fixed dictate starts by itself at your next logon, or "
            "type `dictate run` now.\nTo stop it starting at logon: "
            "`dictate autostart disable`.\n\n"
            # True every time this box is shown, and nothing else on screen says
            # it: the box is modal, so this process waits here, and Windows goes
            # on counting the logon task as running for as long as it is up.
            "While this box is open dictate is NOT running, and Windows still "
            "counts the logon task as running. Closing it lets this process end.",
        )
    except Exception:  # a failed message box must never change the outcome
        pass


def _attempt(config_path: str | None, log: LogonLog) -> int:
    """One go at starting the app. Raises `DictateError` if it could not."""
    from . import app as app_mod, config as config_mod, doctor as doctor_mod  # noqa: PLC0415
    from .logging_setup import configure  # noqa: PLC0415

    if config_path:
        cfg = config_mod.load(config_path)
    else:
        path = config_mod.default_config_path()
        cfg = config_mod.load(path if path.exists() else None)
    configure(cfg.logging.level, cfg.logging.file)

    failures = [r for r in doctor_mod.collect(cfg)
                if r.status is doctor_mod.Status.FAIL]
    if failures:
        raise DictateError(
            "dictate cannot start yet:\n" + doctor_mod.format_report(failures),
            "Run `dictate doctor` on this PC for the full check.",
        )
    log.write(f"config: {cfg.source_path or 'built-in defaults'}")
    return app_mod.run(cfg, console=log.write)


def run_at_logon(config_path: str | None = None) -> int:
    """`dictate run --autostart`: the entry point the logon task calls.

    Two things make this different from `dictate run`. It writes everything down,
    because there is no console to write to. And it waits and tries again,
    because at logon the graphics driver may still be loading, a second disk may
    not be mounted, and a whisper-server from the previous session may still be
    letting go of the port - all of which fix themselves within a minute. It
    gives up after a bounded number of tries and *reports*, rather than retrying
    into eternity.
    """
    from .app import EXIT_RESTART as _EXIT_RESTART  # noqa: PLC0415

    started = time.time()
    settings = _settings_or_default(config_path, AutostartConfig())
    with LogonLog() as log:
        log.start_block()
        # PID_MARK, not a string of its own: `parse_logon_start` reads this line
        # back and that is what lets the report ask Windows whether the process
        # the task started is still there.
        log.write(f"{PID_MARK}{os.getpid()})")

        lock = instance.InstanceLock(started_by="logon")
        try:
            lock.acquire()
        except DictateError as exc:
            # Not a failure: he started one by hand before the task fired.
            log.write(exc.message)
            log.write("outcome: nothing to do, dictate is already running.")
            return 0

        restart = False
        try:
            instance.clear_stop_request()
            last: DictateError | None = None
            attempt = 0
            for attempt in range(1, settings.startup_attempts + 1):
                log.write(f"attempt {attempt} of {settings.startup_attempts}")
                try:
                    code = _attempt(config_path, log)
                    if code == _EXIT_RESTART:
                        log.write("outcome: a restart was asked for from the "
                                  "tray icon; a fresh copy starts as soon as "
                                  "this one has let go of the lock.")
                        restart = True
                        break
                    log.write(f"outcome: dictate ran and exited normally (code {code}).")
                    return code
                except ConfigError as exc:
                    # A broken config file will not repair itself while we wait.
                    log.write(exc.report())
                    last = exc
                    break
                except DictateError as exc:
                    log.write(exc.report())
                    last = exc
                except Exception as exc:
                    # Something nobody anticipated. Under pythonw.exe there is
                    # no stderr for Python to print a traceback to, so an
                    # unexpected fault is the one failure that really would
                    # vanish without a trace. It goes in the log instead.
                    log.write("something went wrong that dictate did not expect:")
                    log.write(traceback.format_exc())
                    last = DictateError(
                        f"dictate stopped with an unexpected error: {exc}",
                        "The technical detail is in the log named below - that "
                        "is the thing to send on.",
                    )
                if attempt >= settings.startup_attempts:
                    break
                if instance.stop_requested(since=started):
                    log.write("outcome: a stop was asked for while waiting; giving up.")
                    instance.clear_stop_request()
                    return 0
                log.write(f"waiting {settings.retry_delay_s:.0f}s and trying again "
                          "(at logon, what it needs may not be ready yet)")
                time.sleep(settings.retry_delay_s)

            if restart:
                failure = ""
            else:
                log.write(f"outcome: gave up after {attempt} attempt(s). dictate "
                          "is NOT running. Start it by hand with `dictate run` "
                          "once the problem above is fixed.")
                failure = last.report() if last else "dictate could not start."
        finally:
            lock.release()

    # Everything above has let go before this point, and deliberately. For the
    # restart, because the new copy takes the same lock the moment it starts.
    # For the dialog, because it is modal and waits for a click that may not
    # come until he sits down tomorrow: holding the single-instance lock behind
    # it would mean `dictate run` answering "dictate is already running" for a
    # copy that gave up hours ago - exactly the baffling failure this whole
    # guard exists to prevent.
    if restart:
        return _restart(config_path)

    # The box is what keeps this process alive from here, so the log says so
    # before it goes up and again once it has gone. Without those two lines the
    # only outward sign of this state is Task Scheduler reporting the task as
    # Running - which is exactly what got read as "dictate started fine".
    showing = _will_notify(settings)
    if showing:
        _write_to_log(dialog_lines(os.getpid()))
    _notify_failure(settings, failure)
    if showing:
        _write_to_log(["the message box was closed; this process is ending now "
                       "(exit code 2), and the logon task's run ends with it."])
    return 2


def _write_to_log(lines: list[str]) -> None:
    """Append to the logon log after the run's own block has been closed.

    Wrapped in every direction: this is a note about a failure and may never
    become a second one.
    """
    try:
        with LogonLog() as log:
            for line in lines:
                log.write(line)
    except Exception:  # noqa: BLE001 - a log that will not open changes nothing
        pass


def _restart(config_path: str | None) -> int:
    """Start the fresh copy the tray's Restart item asked for.

    Under the logon task there is no console to report to, so the outcome goes
    in the same log every other logon start writes to.
    """
    from . import recovery  # noqa: PLC0415

    with LogonLog() as log:
        try:
            # --autostart again, because that is what this copy is: it has no
            # console, and only that entry point gives the new one somewhere to
            # write. See `recovery.relaunch_argv`.
            pid = recovery.relaunch(config_path, autostart=True)
        except DictateError as exc:
            log.write(exc.report())
            return 2
        # RESTART_MARK, so the report follows the pid across a restart instead
        # of finding the dead one and calling an ordinary restart a fault.
        log.write(f"{RESTART_MARK}{pid}).")
    return 0


def _settings_or_default(config_path: str | None,
                         current: AutostartConfig) -> AutostartConfig:
    """Pick up his `[autostart]` settings once the config can be read.

    The first attempt runs on the defaults, because the config is one of the
    things that might not be readable yet.
    """
    from . import config as config_mod  # noqa: PLC0415

    try:
        path = Path(config_path) if config_path else config_mod.default_config_path()
        if path.exists():
            return config_mod.load(path).autostart
    except DictateError:
        pass
    return current
