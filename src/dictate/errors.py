"""Error types.

Every error that can reach the product owner carries a `remedy`: a sentence a
non-developer can act on. `DictateError.report()` is what gets printed or shown
in the overlay, and it is the reason there are no bare tracebacks on the user
paths.
"""

from __future__ import annotations


class DictateError(Exception):
    """Base class. `message` says what happened, `remedy` says what to do."""

    def __init__(self, message: str, remedy: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.remedy = remedy

    def report(self) -> str:
        if self.remedy:
            return f"{self.message}\n  -> {self.remedy}"
        return self.message


class ConfigError(DictateError):
    """The config file is missing, malformed, or says something impossible."""


class MissingDependencyError(DictateError):
    """A Python package or a downloaded artefact the user must install is absent."""


class BackendUnavailableError(DictateError):
    """A transcription backend could not be started or has died and stayed dead."""


class TranscriptionError(DictateError):
    """A single transcription request failed. Recoverable - the app keeps running."""


class AlreadyRunningError(DictateError):
    """A second copy of dictate was asked to start while one is already running.

    Two copies would fight over the global hotkey and over whisper-server's port,
    and the resulting failure is baffling rather than legible. `holder` describes
    the copy that got there first, so the message can name it.
    """

    def __init__(self, message: str, remedy: str = "", holder=None) -> None:
        super().__init__(message, remedy)
        self.holder = holder


class MouseHookError(DictateError):
    """The low-level mouse hook behind a mouse trigger could not be installed,
    or has been lost.

    Its own class because it is the one failure that is NOT the end of
    dictation: the keyboard chord in `[hotkey] keyboard_fallback` is registered
    alongside it and still works, so this is reported loudly and carried on
    from rather than raised at the user as a dead product
    (`platform/trigger_pair.py`).
    """


class PlatformUnsupportedError(DictateError):
    """A platform-specific component was asked for on a platform that has no
    implementation. Raised loudly and early - never silently substituted."""


class InjectionError(DictateError):
    """Text could not be delivered to the target window.

    `partial` is the one thing the caller cannot work out for itself: whether
    any of the text reached the window before this was raised. It decides
    whether "your words are on the clipboard, press Ctrl+V" is the whole truth
    or whether he has to look at the window first - a paste that stopped half
    way and a clipboard holding the whole sentence is how a dictation gets
    pasted twice. Delivering nothing is the ordinary case and the default.
    """

    def __init__(self, message: str, remedy: str = "", partial: bool = False) -> None:
        super().__init__(message, remedy)
        self.partial = partial
