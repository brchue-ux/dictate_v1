"""Building the platform layer for the machine we are actually on.

There is exactly one implementation - Windows - and no fallbacks. On any other
platform every constructor here raises `PlatformUnsupportedError` explaining
which component is missing and why. That is deliberate: a no-op overlay, a paste
that silently does nothing, or a child guard that guards nothing would let the
app look like it worked, and the build brief rules that out.

The tests do not call this. They construct `Pipeline` with their own doubles,
which is the whole point of the seam.
"""

from __future__ import annotations

import sys

from ..config import Config
from ..errors import PlatformUnsupportedError

WINDOWS = sys.platform == "win32"


def _require_windows(component: str) -> None:
    if not WINDOWS:
        raise PlatformUnsupportedError(
            f"dictate's {component} is implemented for Windows only, and this "
            f"is {sys.platform}.",
            "Run dictate on the Windows machine. The transcription pipeline, "
            "the cleanup pass and the tests all run anywhere; only the four "
            "platform pieces need Windows.",
        )


def make_window_tracker():
    _require_windows("focused-window tracking")
    from .windows.window import WindowsWindowTracker  # noqa: PLC0415

    return WindowsWindowTracker()


def make_injector(cfg: Config, tracker):
    _require_windows("text delivery")
    from .windows.inject import WindowsTextInjector  # noqa: PLC0415

    return WindowsTextInjector(
        tracker,
        method=cfg.paste.method,
        restore_focus=cfg.paste.restore_focus,
        per_char_delay_ms=cfg.paste.per_char_delay_ms,
        clipboard_restore_delay_ms=cfg.paste.clipboard_restore_delay_ms,
        trailing_space=cfg.paste.trailing_space,
    )


def make_overlay(cfg: Config, *, notify=None):
    _require_windows("caption overlay")
    from .windows.overlay import TkCaptionOverlay  # noqa: PLC0415

    # `notify` is how the overlay says out loud that the caption font it was
    # asked for is not installed. Tk substitutes silently, so without this the
    # only symptom is that the captions look wrong for no stated reason.
    return TkCaptionOverlay(cfg.overlay, notify=notify)


def make_audio_capture(cfg: Config):
    _require_windows("microphone capture")
    from .windows.audio import SoundDeviceCapture  # noqa: PLC0415

    return SoundDeviceCapture(
        sample_rate=cfg.audio.sample_rate,
        channels=cfg.audio.channels,
        block_frames=cfg.block_frames,
        device=cfg.audio.device,
    )


def make_hotkey_listener(cfg: Config):
    _require_windows("global hotkey")
    from .windows.hotkey import WindowsHotkeyListener  # noqa: PLC0415

    return WindowsHotkeyListener(cfg.hotkey.combination, cfg.hotkey.mode)


def make_child_guard():
    """The thing that makes an orphaned whisper-server impossible.

    A Windows job object; see `windows/job.py` for why nothing inside this
    process can do the same job. There is no equivalent here, so this raises
    like everything else in this file rather than returning something that would
    contain nothing while looking like it contained something.
    """
    _require_windows("child-process containment")
    from .windows.job import WindowsJobGuard  # noqa: PLC0415

    return WindowsJobGuard()


def make_process_tools():
    """Finding and ending a process that a previous run left behind."""
    _require_windows("finding and ending a stuck process")
    from .windows.processes import WindowsProcessTools  # noqa: PLC0415

    return WindowsProcessTools()


def make_tray_icon(actions, *, icon_dir, state=None):
    """The icon in the notification area."""
    _require_windows("the notification-area icon")
    from .windows.tray import WindowsTrayIcon  # noqa: PLC0415

    return WindowsTrayIcon(actions, icon_dir=icon_dir, state=state)
