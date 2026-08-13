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


class PlatformUnsupportedError(DictateError):
    """A platform-specific component was asked for on a platform that has no
    implementation. Raised loudly and early - never silently substituted."""


class InjectionError(DictateError):
    """Text could not be delivered to the target window."""
