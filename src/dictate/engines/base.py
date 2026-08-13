"""Transcription backend interfaces.

Two shapes, because the product uses two genuinely different kinds of model:

* `BatchTranscriber` sees a whole utterance and returns the text that gets
  pasted. This is Whisper large-v3-turbo on the GPU.
* `StreamingTranscriber` sees audio as it arrives and returns a running guess.
  This is the CPU Zipformer, and its output is DISPLAY ONLY - see the note on
  `StreamingSession`.

Both are protocols rather than base classes so a test double is just an object
with the right methods, and so nothing fake ever needs to live in the shipped
package to make the pipeline runnable.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class BatchTranscriber(Protocol):
    """Transcribes a complete utterance. The text this returns is the text that
    ends up in the product owner's document."""

    def start(self) -> None:
        """Bring the backend up and leave it up. Must be idempotent."""

    def stop(self) -> None:
        """Shut the backend down cleanly. Must be safe to call twice."""

    def is_healthy(self) -> bool:
        """True if a transcribe request would be served right now."""

    def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        """Transcribe PCM16 mono audio. Raises `TranscriptionError` on failure."""

    @property
    def describe(self) -> str:
        """One line naming the engine and model, for logs and `dictate doctor`."""


@runtime_checkable
class StreamingSession(Protocol):
    """One utterance's worth of streaming decode.

    The text from a session NEVER reaches the clipboard, the keyboard, or the
    document. It is drawn on the caption overlay and discarded when the hotkey
    is released. That is the whole reason a fast-but-sloppy model is acceptable
    here (see docs/DESIGN.md, decision 4).
    """

    def accept(self, pcm: bytes) -> None:
        """Feed PCM16 mono audio at the session's sample rate."""

    def text(self) -> str:
        """The current best guess. Caption use only."""

    def close(self) -> None:
        """Release the decoder. Safe to call twice."""


@runtime_checkable
class StreamingTranscriber(Protocol):
    def start_session(self) -> StreamingSession: ...

    @property
    def describe(self) -> str: ...

    def close(self) -> None: ...
