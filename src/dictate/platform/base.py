"""Platform seams.

Four things in this app can only work on Windows: capturing the microphone,
hearing a global hotkey held down, drawing a caption overlay that never takes
focus, and delivering text to another application's window.

They live behind these four interfaces so that the parts that carry the actual
product logic - the state machine, the cleanup pass, the backend lifecycle - are
plain Python with no platform in them, and can be tested anywhere. Nobody on this
build had Windows or an AMD GPU; that constraint is the reason for this file.

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

    def set_state(self, state: OverlayState, text: str = "") -> None:
        """Update what is on screen. Safe to call from any thread."""

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
