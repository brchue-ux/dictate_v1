"""WAV encoding and PCM conversion.

whisper-server takes a WAV upload, so the utterance buffer has to become a RIFF
file in memory. `wave` from the stdlib does this correctly, including the odd-
length chunk padding rule, so this module is a thin, well-tested wrapper rather
than hand-rolled header packing.
"""

from __future__ import annotations

import array
import io
import sys
import wave

BYTES_PER_SAMPLE = 2


def pcm16_to_wav(pcm: bytes, sample_rate: int = 16000, channels: int = 1) -> bytes:
    """Wrap raw little-endian PCM16 in a RIFF/WAVE container."""
    if len(pcm) % (BYTES_PER_SAMPLE * channels):
        # Truncate a torn final frame rather than emitting a malformed file.
        usable = len(pcm) - (len(pcm) % (BYTES_PER_SAMPLE * channels))
        pcm = pcm[:usable]
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(BYTES_PER_SAMPLE)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def silence(seconds: float, sample_rate: int = 16000) -> bytes:
    """Digital silence, used to warm the GPU pipeline at startup."""
    return b"\x00\x00" * int(sample_rate * seconds)


def pcm16_to_float32(pcm: bytes) -> list[float]:
    """Convert PCM16 to the -1.0..1.0 floats sherpa-onnx wants.

    Returns a plain list so this module stays numpy-free; the sherpa adapter
    converts to numpy only if numpy is present.
    """
    samples = array.array("h")
    usable = len(pcm) - (len(pcm) % BYTES_PER_SAMPLE)
    samples.frombytes(pcm[:usable])
    if sys.byteorder == "big":
        samples.byteswap()
    return [s / 32768.0 for s in samples]


def float32_to_pcm16(samples: list[float]) -> bytes:
    """Inverse of `pcm16_to_float32`, with clipping. Used by tests and tooling."""
    out = array.array("h", (max(-32768, min(32767, int(round(s * 32768.0)))) for s in samples))
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


def wav_to_pcm16(data: bytes) -> tuple[bytes, int]:
    """Read a mono 16-bit WAV back to (pcm, sample_rate). Used by `dictate transcribe`."""
    with wave.open(io.BytesIO(data), "rb") as w:
        if w.getsampwidth() != BYTES_PER_SAMPLE:
            raise ValueError(f"expected 16-bit audio, got {w.getsampwidth() * 8}-bit")
        frames = w.readframes(w.getnframes())
        channels = w.getnchannels()
        rate = w.getframerate()
    if channels > 1:
        frames = _downmix(frames, channels)
    return frames, rate


def _downmix(pcm: bytes, channels: int) -> bytes:
    samples = array.array("h")
    usable = len(pcm) - (len(pcm) % (BYTES_PER_SAMPLE * channels))
    samples.frombytes(pcm[:usable])
    if sys.byteorder == "big":
        samples.byteswap()
    out = array.array("h")
    for i in range(0, len(samples), channels):
        out.append(int(sum(samples[i:i + channels]) / channels))
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()
