"""The reason whisper-server cannot outlive dictate.

**The bug.** Ctrl+C in the window running `dictate run`, then `Y` at Windows'
"Terminate batch job (Y/N)?" prompt. dictate went away without running a line of
its shutdown; `whisper-server.exe` did not. It kept the transcription port, so
every later `dictate run` and every `setup.ps1 -Only verify` failed on a port
that nothing appeared to be using, and the only way out was Task Manager.

**Why a shutdown handler is not the fix.** dictate already stops its child
cleanly on Ctrl+C, on SIGTERM, on `dictate stop` and on a failed start. The case
that bit him is precisely the one where the parent never gets to run any of
that. No amount of code inside this process can cover a process that is no
longer executing, so the containment has to be something the *operating system*
enforces on our behalf.

**What that is.** A Windows job object with
`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`. Every process assigned to the job is
terminated when the last handle to the job closes - and this process's handles
are closed by Windows itself when it ends, however it ends. So:

    Ctrl+C, and the shutdown runs      -> the child is stopped politely, and
                                          leaves the job by dying
    Ctrl+C, terminate prompt, crash,   -> our handle closes with the process,
    End Task, taskkill /F                 the job closes, Windows ends the child

There is no path through this that leaves an orphan, and none of it depends on
dictate behaving well on the way out.

**What this is not.** It is not a process group and it is not `taskkill /T`:
both need someone to still be running to send anything. It is not a substitute
for the clean shutdown either - a job kill is a `TerminateProcess`, which gives
whisper.cpp no chance to free the card, so the ordinary path remains "ask
first". This is the floor underneath it.

Nested jobs have been supported since Windows 8, so being run from something
that is itself a job - Task Scheduler runs the logon task inside one - is fine:
our job nests inside it and both limits apply.

`.github/workflows/ci.yml` proves this on a real Windows runner: a parent starts
a child through `ManagedProcess`, the parent is ended with `Stop-Process -Force`
so that no cleanup of ours can possibly run, and the child is then required to
be gone and the port free. That is the closest anyone can get to his Ctrl+C
without his machine.
"""

from __future__ import annotations

import ctypes
import logging
import os
from ctypes import wintypes

from ...errors import DictateError

log = logging.getLogger(__name__)

#: JOBOBJECTINFOCLASS.JobObjectExtendedLimitInformation
_EXTENDED_LIMIT_INFORMATION = 9
#: JOBOBJECT_BASIC_LIMIT_INFORMATION.LimitFlags
_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

#: What AssignProcessToJobObject needs on the process handle.
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
        ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _kernel32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    return kernel32


class WindowsJobGuard:
    """One job object, created on first use and held for the life of dictate.

    Implements `platform.base.ChildGuard`. The handle is deliberately never
    closed by us before exit: closing it is what kills the children, and Windows
    does that at exactly the right moment - when this process ceases to exist.
    """

    def __init__(self) -> None:
        self._kernel32 = _kernel32()
        self._handle: int | None = None
        self.adopted: list[int] = []

    # -- the job ---------------------------------------------------------

    def _job(self) -> int:
        if self._handle is not None:
            return self._handle
        handle = self._kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise DictateError(
                "Windows would not give dictate a job object, which is what "
                "stops the transcription process outliving it: "
                f"error {ctypes.get_last_error()}.",
                "Restart the PC and try again. If it keeps happening, dictate "
                "still runs - but if it is ever ended without shutting down, "
                "clear what is left with:\n  dictate stop",
            )
        limits = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limits.BasicLimitInformation.LimitFlags = _LIMIT_KILL_ON_JOB_CLOSE
        ok = self._kernel32.SetInformationJobObject(
            handle, _EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits), ctypes.sizeof(limits))
        if not ok:
            error = ctypes.get_last_error()
            self._kernel32.CloseHandle(handle)
            raise DictateError(
                "Windows would not let dictate say that the transcription "
                "process must not outlive it "
                f"(SetInformationJobObject: error {error}).",
                "Restart the PC and try again. If it keeps happening and "
                "whisper-server is ever left behind, clear it with:\n"
                "  dictate stop",
            )
        self._handle = int(handle)
        log.info("job object created; anything put in it dies with this process")
        return self._handle

    # -- the children ----------------------------------------------------

    def adopt(self, process) -> None:
        """Put a just-started child in the job.

        The child's HANDLE is used rather than its pid wherever possible.
        Between `CreateProcess` returning and this call, a pid can in principle
        be released and re-issued to something else, and this call ends whatever
        it names when we exit - so naming it by a handle that Windows is holding
        open for us removes the question entirely. `subprocess.Popen` keeps
        exactly that handle, and there is no public way to it.
        """
        job = self._job()
        pid = getattr(process, "pid", None)
        raw = getattr(process, "_handle", None)
        opened = None
        try:
            if raw is None:
                opened = self._kernel32.OpenProcess(
                    _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, int(pid))
                if not opened:
                    raise DictateError(
                        f"dictate could not take hold of the transcription "
                        f"process it just started (process {pid}, error "
                        f"{ctypes.get_last_error()}).",
                        "Stop dictate and start it again:\n  dictate stop\n"
                        "  dictate run",
                    )
                handle = opened
            else:
                handle = int(raw)
            if not self._kernel32.AssignProcessToJobObject(job, handle):
                raise DictateError(
                    "Windows would not let dictate tie whisper-server's life to "
                    f"its own (process {pid}, error {ctypes.get_last_error()}).",
                    "dictate still works. But if it is ever ended without being "
                    "allowed to shut down, whisper-server may be left holding "
                    "the transcription port - clear it with:\n  dictate stop",
                )
        finally:
            if opened:
                self._kernel32.CloseHandle(opened)
        if pid is not None:
            self.adopted.append(int(pid))
        log.info("whisper-server (pid %s) is in the job: it cannot outlive this "
                 "process", pid)

    def close(self) -> None:
        """Nothing. Deliberately.

        Closing the handle is what terminates the children, and the moment for
        that is when this process ends - which Windows handles. Closing it early
        would kill a whisper-server that is mid-transcription during an ordinary
        idle release.
        """

    @property
    def describe(self) -> str:
        return ("a Windows job object: whisper-server is ended by Windows if "
                f"dictate (process {os.getpid()}) ever dies without stopping it")
