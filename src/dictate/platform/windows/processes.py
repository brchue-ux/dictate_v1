"""The three questions a rescue has to ask Windows - and the one a report does.

Who is holding this TCP port, what is that process called, and end it - along
with everything it started. That is all. Everything about *whether* to end
anything is in `dictate/recovery.py`, where it can be read and tested off
Windows; this file is the thin part that cannot be.

It uses `netstat`, `tasklist` and `taskkill` rather than the corresponding Win32
calls, for the same reason `autostart.py` uses `schtasks`: they ship with every
Windows, their output is stable and can be parsed in a test against a real
sample, and they are the same commands the messages tell him to type when
dictate cannot do it for him. What they print is parsed by pure functions in
`recovery.py`; nothing about the parsing lives here.

`command_line_of` is the one thing here for a report rather than a rescue, and
it reaches for something else: none of the three tools above print a command
line, only an image name, so `dictate autostart status --why` asks Windows'
WMI/CIM through `Get-CimInstance` instead - PowerShell, not a fourth console
tool, because this is the one question they cannot answer.

`taskkill /T` and never `/PID` alone: ending dictate without its whisper-server
is exactly the orphan this whole change exists to make impossible.
"""

from __future__ import annotations

import logging
import subprocess

from ... import recovery
from ...errors import DictateError

log = logging.getLogger(__name__)

#: Long enough for a busy machine, short enough that a rescue never looks hung.
TOOL_TIMEOUT_S = 20.0

#: Keeps a console window from flashing up when dictate is running windowless.
_NO_WINDOW = 0x08000000


class WindowsProcessTools:
    """Implements `platform.base.ProcessTools`."""

    def __init__(self, *, run=None) -> None:
        self._run = run or self._run_tool

    # -- the seam --------------------------------------------------------

    def _run_tool(self, argv: list[str]) -> tuple[int, str]:
        try:
            # stdin=DEVNULL: every caller of this is a read-only report and has
            # to stay safe to run mid-sentence. None of netstat, tasklist,
            # taskkill or powershell mean to read stdin, but a child that
            # silently inherited a strange or blocking handle from whatever
            # launched dictate is exactly the shape of the fault this is here
            # to close off before it ever turns a report into a hang.
            proc = subprocess.run(
                argv, capture_output=True, text=True, errors="replace",
                check=False, timeout=TOOL_TIMEOUT_S, creationflags=_NO_WINDOW,
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            raise DictateError(
                f"dictate could not run {argv[0]}, which is the Windows command "
                f"it uses to find and end a stuck transcription process: {exc}",
                f"{argv[0]}.exe lives in C:\\Windows\\System32. If it is not "
                "there, end the process in Task Manager instead - look for "
                "whisper-server.exe.",
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise DictateError(
                f"{argv[0]} did not answer within {TOOL_TIMEOUT_S:.0f} seconds.",
                "Try again, and if it happens twice, restart the PC.",
            ) from exc
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    # -- what recovery.py asks for ---------------------------------------

    def listeners(self, port: int) -> list[recovery.Listener]:
        code, output = self._run(["netstat", "-ano", "-p", "TCP"])
        if code != 0:
            log.debug("netstat exited %s: %s", code, output.strip()[:400])
        return recovery.parse_listeners(output, port)

    def name_of(self, pid: int) -> str:
        code, output = self._run(
            ["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"])
        if code != 0:
            log.debug("tasklist exited %s: %s", code, output.strip()[:400])
        return recovery.parse_task_name(output)

    def command_line_of(self, pid: int) -> str | None:
        """The full command line `pid` was started with, or `None` if it could
        not be read.

        tasklist does not carry this - `name_of` and `pids_named` above only
        ever get the image name. This asks Windows a different way: WMI/CIM,
        through `Get-CimInstance`, which every PowerShell since 3.0 ships with
        and needs no extra install and no elevated rights for an ordinary
        process. `-ErrorAction SilentlyContinue` and the exit-code check both
        turn a refused or empty answer into `None` rather than an exception -
        this is read by a report that may never be the thing that fails.
        """
        code, output = self._run([
            "powershell", "-NoProfile", "-NonInteractive", "-Command",
            f"(Get-CimInstance Win32_Process -Filter \"ProcessId={int(pid)}\" "
            "-ErrorAction SilentlyContinue).CommandLine",
        ])
        if code != 0:
            log.debug("powershell exited %s asking for the command line of "
                      "pid %s: %s", code, pid, output.strip()[:400])
            return None
        return recovery.parse_command_line(output)

    def pids_named(self, image: str) -> list[int]:
        """Everything running under that image name, for the evidence report.

        The same `tasklist` and the same CSV as `name_of` above, filtered the
        other way round. A filter that matches nothing is an ordinary answer
        here, not a failure: "there is no whisper-server" is exactly what the
        report may need to say.
        """
        code, output = self._run(
            ["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"])
        if code != 0:
            log.debug("tasklist exited %s: %s", code, output.strip()[:400])
        return [pid for name, pid in recovery.parse_task_rows(output)
                if name.strip().lower() == image.strip().lower()]

    def end(self, pid: int) -> None:
        # /T so its children go too, /F because this is only ever reached after
        # the polite request has already been given its full timeout.
        code, output = self._run(["taskkill", "/PID", str(int(pid)), "/T", "/F"])
        log.info("taskkill /PID %s /T /F exited %s: %s", pid, code,
                 output.strip()[:200])
        if code != 0:
            raise DictateError(
                f"Windows would not end process {pid}: "
                f"{output.strip()[:200] or f'taskkill stopped with code {code}'}",
                f"Try it yourself from a PowerShell window opened with 'Run as "
                f"administrator':\n  taskkill /PID {pid} /T /F",
            )

    @property
    def describe(self) -> str:
        return "netstat, tasklist, taskkill and powershell"
