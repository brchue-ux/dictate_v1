"""Building the platform layer for the machine we are actually on.

There is exactly one implementation - Windows - and no fallbacks. On any other
platform every constructor here raises `PlatformUnsupportedError` explaining
which component is missing and why. That is deliberate: a no-op overlay or a
paste that silently does nothing would let the app look like it worked, and the
build brief rules that out.

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


def make_overlay(cfg: Config):
    _require_windows("caption overlay")
    from .windows.overlay import TkCaptionOverlay  # noqa: PLC0415

    return TkCaptionOverlay(cfg.overlay)


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
