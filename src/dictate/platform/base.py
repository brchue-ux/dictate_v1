"""Platform seams.

Some things in this app can only work on Windows: capturing the microphone,
hearing a global hotkey held down, drawing a caption overlay that never takes
focus, delivering text to another application's window, containing a child
process so it cannot outlive us, clearing one that already has, and showing an
icon in the notification area.

They live behind these interfaces so that the parts that carry the actual
product logic - the state machine, the cleanup pass, the backend lifecycle, what
the tray says, what a rescue decides to end - are plain Python with no platform
in them, and can be tested anywhere. Nobody on this build had Windows or an AMD
GPU; that constraint is the reason for this file.

There is no fallback implementation. Asking for one of these on a platform that
does not have it raises `PlatformUnsupportedError` immediately and says so. It
never silently substitutes something that pretends to work.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class TargetWindow:
    """The window that had focus when the hotkey went down.

    Captured at press, not at paste. Between those two moments the caption
    overlay appears, the user may alt-tab, and a notification may steal focus -
    so "whatever is focused when we finish" is the wrong answer and this is the
    right one.
    """

    handle: int
    title: str = ""
    process: str = ""

    def __str__(self) -> str:
        label = self.title or self.process or "unknown window"
        return f"{label} (hwnd {self.handle})"


class OverlayState(Enum):
    HIDDEN = "hidden"
    LISTENING = "listening"      # captions streaming in
    THINKING = "thinking"        # hotkey released, GPU pass running
    DONE = "done"                # text delivered
    ERROR = "error"              # something the user needs to read


#: `set_state(state, KEEP)` changes the state and leaves the words that are
#: already on screen exactly where they are. It is what the hotkey release does,
#: so the caption he was reading stays up while the GPU works instead of the
#: panel emptying at the moment he most wants to see something.
#:
#: It is spelled as "no text supplied" rather than as a string on purpose. The
#: caller cannot put caption text back on screen this way, because the caller
#: never had the caption text: only the streaming session and the overlay's own
#: label ever hold it (`pipeline.pump_captions`). That is what keeps constraint 4
#: - caption text is display-only and never reaches the document - a property of
#: the shape of the code rather than of when the text happens to be cleared.
KEEP = None


@runtime_checkable
class WindowTracker(Protocol):
    def foreground(self) -> TargetWindow | None:
        """The window with keyboard focus right now, or None if unknowable."""

    def focus(self, target: TargetWindow) -> bool:
        """Put focus back on `target`. Returns True if it now has focus."""


@runtime_checkable
class TextInjector(Protocol):
    def send(self, text: str, target: TargetWindow | None) -> None:
        """Deliver `text` to `target`. Raises `InjectionError` on failure."""

    @property
    def describe(self) -> str: ...


@runtime_checkable
class CaptionOverlay(Protocol):
    """An always-on-top, non-activating window.

    The hard requirement is in the name of the second adjective: if this window
    ever takes focus, the window the user was typing into stops being the
    foreground window and the paste lands in the wrong place. Every
    implementation must be created with WS_EX_NOACTIVATE (or the local
    equivalent) and must never call a focus or activate API.
    """

    def set_state(self, state: OverlayState, text: str | None = KEEP,
                  target: TargetWindow | None = None) -> None:
        """Update what is on screen. Safe to call from any thread.

        `text` is what the big line should say; `""` empties it. `KEEP` (the
        default, which is `None`) means "leave what is on screen alone" - see
        the note on `KEEP` above for why that is spelled as an absence.

        An implementation must not carry text across appearances: when the
        panel is not up there is nothing to keep, and a caption from a previous
        utterance must never come back with it.

        `target` is the window captured at hotkey press, passed so the overlay
        can appear on the display that window is on - which is the display the
        user is working on, and where the text is about to be pasted. It is
        advisory: an implementation that only has one screen ignores it.
        """

    def close(self) -> None: ...


AudioCallback = Callable[[bytes], None]


@runtime_checkable
class AudioCapture(Protocol):
    def start(self, callback: AudioCallback) -> None:
        """Begin delivering PCM16 mono blocks to `callback`."""

    def stop(self) -> None:
        """Stop delivering blocks. Safe to call when not started."""

    def close(self) -> None: ...

    @property
    def describe(self) -> str: ...


@runtime_checkable
class HotkeyListener(Protocol):
    def register(self, on_press: Callable[[], None], on_release: Callable[[], None]) -> None:
        """Bind the configured combination. `on_release` fires on key-up."""

    def start(self) -> None: ...

    def stop(self) -> None: ...

    @property
    def describe(self) -> str: ...


@runtime_checkable
class ChildGuard(Protocol):
    """Makes it impossible for a child process to outlive this one.

    Not a tidy-up afterwards and not a shutdown handler: the case that has to be
    covered is the one where this process does not get to run any code at all -
    Ctrl+C answered at the "Terminate batch job" prompt, Task Manager's End
    Task, a crash. Something outside this process has to end the child, which
    means the operating system. On Windows that is a job object with
    `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`; see `windows/job.py`.

    `adopt` is called once per spawned child, immediately after it is created.
    """

    def adopt(self, process) -> None:
        """Tie `process` (a `subprocess.Popen`) to this process's lifetime.

        Raises `DictateError` if it could not, so the caller can say so out
        loud. It must never silently do nothing: an unguarded child is the whole
        bug this exists to prevent.
        """

    def close(self) -> None: ...

    @property
    def describe(self) -> str: ...


@runtime_checkable
class ProcessTools(Protocol):
    """The three things a rescue needs to ask the operating system.

    Kept this small on purpose: `recovery.py` decides everything, and this is
    only the part of it that cannot be answered in plain Python.
    """

    def listeners(self, port: int) -> list:
        """Who holds `port`, as `recovery.Listener` rows. May be empty."""

    def name_of(self, pid: int) -> str:
        """The image name of `pid` ("whisper-server.exe"), or "" if it is gone."""

    def end(self, pid: int) -> None:
        """End `pid` AND its children. Never the parent on its own - that is
        what strands a whisper-server holding the port and 1.6 GB of VRAM."""


@runtime_checkable
class TrayIcon(Protocol):
    """The notification-area icon: dictate's only visible surface at logon.

    With `dictate autostart enable` there is no console window and no way to
    type anything at the running copy, so this is what carries the status and
    the way out. It runs its own message loop on its own thread and must never
    block the app.
    """

    def start(self) -> None:
        """Show the icon. Returns once it is on screen or has failed."""

    def update(self, state) -> None:
        """Re-draw from a `tray.TrayState`. Safe to call from any thread."""

    def close(self) -> None: ...

    @property
    def describe(self) -> str: ...
