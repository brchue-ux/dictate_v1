"""Test doubles.

These live in `tests/`, never in `src/`. The build brief forbids stubbing
anything into the shipped product to make it look like it works, so the package
contains no fallback overlay, no no-op paste and no fake transcriber - if a
platform piece is missing the app says so and stops. The doubles that let the
pipeline be exercised without a microphone or a GPU are all here.
"""

from __future__ import annotations

import threading

from dictate.errors import TargetNotForegroundError
from dictate.platform.base import OverlayState, TargetWindow


class FakeBatch:
    """Stands in for whisper-server.

    The gates exist for the residency tests: loading the real model takes
    seconds, and the interesting cases are all about what happens *during* that
    load or that shutdown. Holding a gate closed puts the wrapper in exactly
    that state for as long as the test needs, with no sleeping and no guessing.
    """

    def __init__(self, text: str = "Hello world.", error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.calls: list[tuple[int, int]] = []
        self.healthy = True
        self.running = False
        #: Counts, so a test can prove nothing was started twice.
        self.starts = 0
        self.stops = 0
        #: Set an Event here and start()/stop()/transcribe() block until it is set.
        self.start_gate: threading.Event | None = None
        self.stop_gate: threading.Event | None = None
        self.transcribe_gate: threading.Event | None = None
        #: Raised by start(), to stand in for a model that will not come back.
        self.start_error: Exception | None = None
        #: Signalled as soon as start()/transcribe() is entered, before the gate.
        self.start_entered = threading.Event()
        self.transcribe_entered = threading.Event()

    def start(self) -> None:
        self.start_entered.set()
        if self.start_gate is not None:
            self.start_gate.wait(timeout=30)
        if self.start_error is not None:
            raise self.start_error
        if self.running:
            # The real backend would no-op here; this is louder on purpose,
            # because two live whisper-servers on one port is the failure the
            # residency tests are looking for.
            raise AssertionError("start() while already running: two servers")
        self.starts += 1
        self.running = True
        self.healthy = True

    def stop(self) -> None:
        # ManagedProcess.stop() signals a start that is still waiting for the
        # server to become healthy, and that is what keeps shutdown bounded
        # while a load is in flight. Model that here rather than deadlocking.
        if self.start_gate is not None:
            self.start_gate.set()
        if self.stop_gate is not None:
            self.stop_gate.wait(timeout=30)
        if self.running:
            self.stops += 1
        self.running = False
        self.healthy = False

    def is_healthy(self) -> bool:
        return self.healthy

    @property
    def describe(self) -> str:
        return "fake batch transcriber"

    def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        self.transcribe_entered.set()
        if self.transcribe_gate is not None:
            self.transcribe_gate.wait(timeout=30)
        self.calls.append((len(pcm), sample_rate))
        if self.error:
            raise self.error
        return self.text


class FakeSession:
    def __init__(self, owner: "FakeStreaming") -> None:
        self.owner = owner
        self.chunks: list[bytes] = []
        self.closed = False

    def accept(self, pcm: bytes) -> None:
        if self.owner.raise_on_accept:
            raise self.owner.raise_on_accept
        self.chunks.append(pcm)

    def text(self) -> str:
        return " ".join(f"CHUNK{i}" for i in range(1, len(self.chunks) + 1))

    def close(self) -> None:
        self.closed = True
        self.chunks.clear()


class FakeStreaming:
    def __init__(self) -> None:
        self.sessions: list[FakeSession] = []
        self.raise_on_accept: Exception | None = None
        self.raise_on_start: Exception | None = None

    def start_session(self) -> FakeSession:
        if self.raise_on_start:
            raise self.raise_on_start
        session = FakeSession(self)
        self.sessions.append(session)
        return session

    @property
    def describe(self) -> str:
        return "fake streaming transcriber"

    def close(self) -> None:
        pass


class FakeInjector:
    def __init__(self, error: Exception | None = None) -> None:
        self.sent: list[tuple[str, TargetWindow | None]] = []
        self.error = error
        #: Every text handed over because it could NOT be pasted. The clipboard,
        #: on the real thing.
        self.kept: list[str] = []
        #: Set to make the clipboard refuse, which is the case where dictate has
        #: to say the words are only in the history.
        self.clipboard_fails = False
        #: Calls which required the captured target to remain foreground. The
        #: deferred route always does; the ordinary route never needs to.
        self.required_foreground: list[bool] = []

    def send(self, text: str, target: TargetWindow | None, *,
             require_target_foreground: bool = False,
             still_allowed=None) -> int:
        if still_allowed is not None and not still_allowed():
            raise TargetNotForegroundError("automatic wait ended")
        if self.error:
            raise self.error
        self.sent.append((text, target))
        self.required_foreground.append(require_target_foreground)
        return 0

    def to_clipboard(self, text: str) -> bool:
        if self.clipboard_fails:
            return False
        self.kept.append(text)
        return True

    @property
    def describe(self) -> str:
        return "fake injector"


class FakeWindows:
    """A focused window that can move underneath us, like the real thing.

    `window` is what `foreground()` answers, so a test moves focus mid-utterance
    by assigning to it between the press and the release - which is exactly what
    happens when he clicks on something else while he is speaking.
    """

    def __init__(self, window: TargetWindow | None = None) -> None:
        self.window = window or TargetWindow(handle=4242, title="Notepad", process="notepad.exe")
        self.focused: list[TargetWindow] = []
        self.raise_on_foreground: Exception | None = None
        #: Handles that no longer name a window - a window closed while he was
        #: still talking.
        self.closed: set[int] = set()
        self.raise_on_exists: Exception | None = None

    def foreground(self) -> TargetWindow | None:
        if self.raise_on_foreground:
            raise self.raise_on_foreground
        return self.window

    def focus(self, target: TargetWindow) -> bool:
        self.focused.append(target)
        return True

    def exists(self, target: TargetWindow) -> bool:
        if self.raise_on_exists:
            raise self.raise_on_exists
        if target.handle in self.closed:
            return False
        current = self.window
        if current is not None and current.handle == target.handle \
                and target.process_id and current.process_id:
            return target.process_id == current.process_id
        return True


class FakeOverlay:
    """A screen that remembers everything it was ever asked to show.

    It implements `KEEP` the way the real overlay does - a state change with no
    text leaves the words alone, and nothing survives the panel being hidden -
    so a test can ask what he would actually be looking at at any point, not
    just what the pipeline said.
    """

    def __init__(self) -> None:
        #: Exactly what was passed, `KEEP` (None) included.
        self.history: list[tuple[OverlayState, str | None]] = []
        #: What was on screen after each call, with `KEEP` resolved.
        self.screen: list[tuple[OverlayState, str]] = []
        #: Every target handed to set_state, so tests can hold in place that the
        #: window captured at press is what decides which monitor is used.
        self.targets: list[TargetWindow | None] = []
        self._lock = threading.Lock()
        #: The real overlay's close() is what ends run_forever and so ends the
        #: app. An Event rather than a flag so a test can wait for it.
        self.closed = threading.Event()

    def set_state(self, state: OverlayState, text: str | None = None,
                  target: TargetWindow | None = None) -> None:
        with self._lock:
            showing = self.showing
            if state is OverlayState.HIDDEN:
                showing = ""
            elif text is not None:
                showing = text
            self.history.append((state, text))
            self.screen.append((state, showing))
            self.targets.append(target)

    def close(self) -> None:
        self.closed.set()

    @property
    def showing(self) -> str:
        """The words on screen right now."""
        return self.screen[-1][1] if self.screen else ""

    @property
    def states(self) -> list[OverlayState]:
        return [s for s, _ in self.history]

    @property
    def captions(self) -> list[str]:
        return [t for s, t in self.screen if s is OverlayState.LISTENING and t]


class InlineSubmit:
    """Runs the finalize job on the calling thread, so tests are deterministic."""

    def __init__(self) -> None:
        self.jobs = 0

    def __call__(self, fn) -> None:
        self.jobs += 1
        fn()


class DeferredSubmit:
    """Holds jobs so a test can inspect the state between release and paste."""

    def __init__(self) -> None:
        self.pending: list = []

    def __call__(self, fn) -> None:
        self.pending.append(fn)

    def run_all(self) -> None:
        while self.pending:
            self.pending.pop(0)()


class RecordingGuard:
    """Stands in for the Windows job object.

    What it cannot do is contain anything - that needs the operating system, and
    that half is proved on CI's Windows runners by killing a parent. What it
    does prove here is the half that IS ours and that a Windows-only test could
    never cover from Linux: that every single spawn is handed over, including
    the ones nobody thinks about - a restart after a crash, and a reload after
    an idle release.
    """

    def __init__(self, error: Exception | None = None) -> None:
        self.adopted: list[int] = []
        self.error = error
        self.closed = False

    def adopt(self, process) -> None:
        if self.error is not None:
            raise self.error
        self.adopted.append(process.pid)

    def close(self) -> None:
        self.closed = True

    @property
    def describe(self) -> str:
        return "a guard that only remembers"


class FakeProcessTools:
    """Stands in for netstat, tasklist and taskkill.

    `recovery.py` makes every decision about what to end; this is the three
    answers Windows would give, so those decisions can be driven through every
    branch - including the ones that must NOT end anything.
    """

    def __init__(self, listeners=None, names=None, command_lines=None, *,
                 refuse: Exception | None = None,
                 lingering: bool = False, on_end=None) -> None:
        self._listeners = listeners or {}
        self.names = names or {}
        #: pid -> command line, or `None`/absent for "could not be read".
        self.command_lines = command_lines or {}
        self.refuse = refuse
        #: True: ended processes keep holding the port, as a wedged one would.
        self.lingering = lingering
        #: Called when a process is ended, so a test can close the real socket
        #: that is standing in for the whisper-server being ended.
        self.on_end = on_end
        self.ended: list[int] = []
        self.asked: list[int] = []

    def listeners(self, port: int) -> list:
        return list(self._listeners.get(port, []))

    def name_of(self, pid: int) -> str:
        self.asked.append(pid)
        return self.names.get(pid, "")

    def command_line_of(self, pid: int) -> str | None:
        return self.command_lines.get(pid)

    def pids_named(self, image: str) -> list[int]:
        wanted = image.strip().lower()
        gone = () if self.lingering else tuple(self.ended)
        return sorted(pid for pid, name in self.names.items()
                      if name.strip().lower() == wanted and pid not in gone)

    def end(self, pid: int) -> None:
        if self.refuse is not None:
            raise self.refuse
        self.ended.append(pid)
        if self.lingering:
            return
        for port, rows in list(self._listeners.items()):
            self._listeners[port] = [r for r in rows if r.pid != pid]
        if self.on_end is not None:
            self.on_end(pid)
