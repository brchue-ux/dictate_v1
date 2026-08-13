"""Test doubles.

These live in `tests/`, never in `src/`. The build brief forbids stubbing
anything into the shipped product to make it look like it works, so the package
contains no fallback overlay, no no-op paste and no fake transcriber - if a
platform piece is missing the app says so and stops. The doubles that let the
pipeline be exercised without a microphone or a GPU are all here.
"""

from __future__ import annotations

import threading

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

    def send(self, text: str, target: TargetWindow | None) -> None:
        if self.error:
            raise self.error
        self.sent.append((text, target))

    @property
    def describe(self) -> str:
        return "fake injector"


class FakeWindows:
    """A focused window that can move underneath us, like the real thing."""

    def __init__(self, window: TargetWindow | None = None) -> None:
        self.window = window or TargetWindow(handle=4242, title="Notepad", process="notepad.exe")
        self.focused: list[TargetWindow] = []
        self.raise_on_foreground: Exception | None = None

    def foreground(self) -> TargetWindow | None:
        if self.raise_on_foreground:
            raise self.raise_on_foreground
        return self.window

    def focus(self, target: TargetWindow) -> bool:
        self.focused.append(target)
        return True


class FakeOverlay:
    def __init__(self) -> None:
        self.history: list[tuple[OverlayState, str]] = []
        #: Every target handed to set_state, so tests can hold in place that the
        #: window captured at press is what decides which monitor is used.
        self.targets: list[TargetWindow | None] = []
        self._lock = threading.Lock()

    def set_state(self, state: OverlayState, text: str = "",
                  target: TargetWindow | None = None) -> None:
        with self._lock:
            self.history.append((state, text))
            self.targets.append(target)

    def close(self) -> None:
        pass

    @property
    def states(self) -> list[OverlayState]:
        return [s for s, _ in self.history]

    @property
    def captions(self) -> list[str]:
        return [t for s, t in self.history if s is OverlayState.LISTENING and t]


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
