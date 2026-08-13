"""The utterance buffer.

The whole utterance is kept in memory so the GPU batch pass can see it complete.
At 16 kHz mono 16-bit that is 32 kB per second - five minutes is under 10 MB, so
there is no reason to be clever about it beyond a hard ceiling that stops a stuck
hotkey from growing without bound.

Audio lives here as raw little-endian signed 16-bit PCM `bytes`, which is what
both the WAV writer and sherpa-onnx want, and what every Windows capture API can
produce. Nothing in this module imports numpy - it is stdlib-only so it can be
tested anywhere.
"""

from __future__ import annotations

import threading

BYTES_PER_SAMPLE = 2


class UtteranceBuffer:
    """Append-only PCM accumulator with a hard ceiling.

    Thread-safe: the audio callback thread appends while the pipeline thread
    reads. Appends are a list append under a lock, which is cheap enough to do
    in a real-time audio callback.
    """

    def __init__(self, sample_rate: int, max_seconds: float) -> None:
        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        if max_seconds <= 0:
            raise ValueError("max_seconds must be positive")
        self.sample_rate = sample_rate
        self.max_seconds = max_seconds
        self._max_bytes = int(sample_rate * max_seconds) * BYTES_PER_SAMPLE
        self._chunks: list[bytes] = []
        self._nbytes = 0
        self._overflowed = False
        self._lock = threading.Lock()

    def append(self, pcm: bytes) -> bool:
        """Append PCM16. Returns False once the ceiling is hit (audio is dropped
        from that point, and `overflowed` becomes True)."""
        with self._lock:
            if self._nbytes >= self._max_bytes:
                self._overflowed = True
                return False
            room = self._max_bytes - self._nbytes
            if len(pcm) > room:
                pcm = pcm[:room]
                self._overflowed = True
            self._chunks.append(pcm)
            self._nbytes += len(pcm)
            return not self._overflowed

    def pcm(self) -> bytes:
        with self._lock:
            if len(self._chunks) > 1:
                joined = b"".join(self._chunks)
                self._chunks = [joined]
                return joined
            return self._chunks[0] if self._chunks else b""

    def reset(self) -> None:
        with self._lock:
            self._chunks.clear()
            self._nbytes = 0
            self._overflowed = False

    @property
    def nbytes(self) -> int:
        with self._lock:
            return self._nbytes

    @property
    def duration_s(self) -> float:
        with self._lock:
            return self._nbytes / BYTES_PER_SAMPLE / self.sample_rate

    @property
    def overflowed(self) -> bool:
        with self._lock:
            return self._overflowed
