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
    #: A handle can be recycled after its window closes. The process id makes
    #: a deferred target distinguishable from a later window which happens to
    #: receive the same handle. Zero means the platform could not read it.
    process_id: int = 0

    def __str__(self) -> str:
        label = self.title or self.process or "unknown window"
        return f"{label} (hwnd {self.handle})"


class OverlayState(Enum):
    HIDDEN = "hidden"
    LISTENING = "listening"      # captions streaming in
    THINKING = "thinking"        # hotkey released, GPU pass running
    WAITING = "waiting"          # finished text waits for its captured window
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

    def exists(self, target: TargetWindow) -> bool:
        """Is `target` still a window? Read-only, and never raises it.

        Asked at paste time to distinguish "he moved" from "the target closed",
        and on each deferred poll so a wait for a dead or recycled target ends
        safely. `delivery.decide` may still take `None` for "nobody could ask";
        an unknown is never a verdict.
        """


@runtime_checkable
class TextInjector(Protocol):
    def send(self, text: str, target: TargetWindow | None, *,
             require_target_foreground: bool = False,
             still_allowed: Callable[[], bool] | None = None) -> int:
        """Deliver `text` to `target`. Raises `InjectionError` on failure.

        `require_target_foreground` is the no-focus-stealing route. It must
        re-check that `target` is still in front immediately before delivery
        and refuse if that cannot be established; it must never focus or raise
        the target. This closes most of the race between a deferred delivery's
        foreground observation and its SendInput call.

        `still_allowed` belongs to that same route: it is checked beside each
        send so a newer utterance can retire a waiting one while the injector
        was settling modifiers. False means refuse without sending.

        Returns how many Return keypresses delivering it involved - 0 unless
        `[paste] line_breaks = "return"`, because dictated text is typed and
        does not press keys (`platform/line_breaks.py`). It is the one thing a
        paste can do rather than write, so it is reported rather than assumed,
        and it is what the dictation history records.
        """

    def to_clipboard(self, text: str) -> bool:
        """Put `text` on the clipboard so he can place it himself. Never raises.

        Used on one path only: the text could not be pasted (`delivery.HOLD`),
        and the alternative to putting it somewhere he can reach is losing a
        sentence he has just spoken. Constraint 3 says the clipboard is
        preserved by not touching it, and that still holds for every *paste* -
        this is the deliberate exception, it is announced in the same breath by
        the message that explains why nothing was pasted, and `[paste]
        hold_to_clipboard = false` turns it off for someone who would rather
        keep the clipboard and use the dictation history instead.

        Returns whether the text is now on the clipboard, so the message can
        say where the words are rather than guess.
        """

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

#: Called from the audio thread when the operating system reports that input
#: audio was thrown away before dictate ever saw it, with how many callbacks
#: were affected. Not a warning about quality: PortAudio's own header says of
#: `paInputOverflow` that "data prior to the first sample of the input buffer
#: was discarded due to an overflow", so a recording this happened in has a
#: hole in it, and the hole is in the audio BOTH the captions and the pasted
#: text are made from. Must be as cheap as `AudioCallback` - it runs on the
#: same thread, in the same callback.
AudioLossCallback = Callable[[int], None]


@runtime_checkable
class AudioCapture(Protocol):
    def start(self, callback: AudioCallback,
              on_loss: AudioLossCallback | None = None) -> None:
        """Begin delivering PCM16 mono blocks to `callback`.

        `on_loss` is told when the OS discarded input audio. It is optional so
        that a caller which does not care - `dictate devices`, a smoke test -
        does not have to supply one, and never so that the loss can go
        unreported in the running app: `app.Application.start` passes it.
        """

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
    """The few things a rescue - and the report that explains one - need to ask
    the operating system.

    Kept this small on purpose: `recovery.py` decides everything, and this is
    only the part of it that cannot be answered in plain Python.
    """

    def listeners(self, port: int) -> list:
        """Who holds `port`, as `recovery.Listener` rows. May be empty."""

    def name_of(self, pid: int) -> str:
        """The image name of `pid` ("whisper-server.exe"), or "" if it is gone."""

    def command_line_of(self, pid: int) -> str | None:
        """The full command line `pid` was started with, or `None` if it could
        not be read - the process may have gone between being listed and being
        asked about, or the query may have been refused.

        `tasklist` (`name_of`, `pids_named`) only ever gets an image name; this
        is what turns "something named pythonw.exe" into "the logon task's own
        command", or tells `dictate autostart status --why` plainly that it
        could not, rather than leaving that question to tasklist's limits.
        """

    def pids_named(self, image: str) -> list[int]:
        """Every process running `image` right now, by pid. May be empty.

        Nothing is ever *ended* on the strength of this - it is read by
        `dictate autostart status --why`, which reports and changes nothing.
        Two copies of dictate and a whisper-server with no parent look
        identical from inside one process; from here they do not.
        """

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
