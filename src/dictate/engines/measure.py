"""Measuring a live-caption model on a recording, with no screen involved.

This is what `dictate captions <clip.wav>` runs, and it exists for the same
reason `dictate transcribe` does: the numbers that matter are the ones from HIS
machine and HIS voice, and nobody who built this has either. The two questions
it answers are the two he reported:

* **when do the first words appear** - how far into the recording, not how far
  into the wall clock, so the answer does not move when the machine is busy;
* **how often do they change afterwards** - the update interval, which is a
  property of the model and is what "it takes forever to show up" is about.

It feeds the session exactly as `pipeline.pump_captions` feeds it: whole blocks
of `[audio] block_ms`, in order, one at a time. Nothing here knows what a
Zipformer is - it takes anything with `accept`/`text`/`close`, which is what
lets `tests/test_measure.py` run the whole thing with a fake session on a
machine that has no model at all.

Real time is deliberately NOT simulated. Playing a clip back at 1x would make
every number depend on what else the machine was doing, and the RTF below
already says whether the model can keep up.
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class CaptionMeasurement:
    """What one clip through one caption model looked like."""

    audio_s: float
    blocks: int
    #: Seconds INTO THE RECORDING at which the panel first had words on it, or
    #: None if the model never produced any.
    first_words_s: float | None
    #: Every moment the words changed: (seconds into the recording, the text).
    updates: list[tuple[float, str]] = field(default_factory=list)
    #: Seconds of CPU spent decoding, over seconds of audio. Under 1.0 means the
    #: model keeps up with speech; the caption thread has two threads to do it in.
    rtf: float = 0.0
    decode_s: float = 0.0
    text: str = ""

    @property
    def gaps(self) -> list[float]:
        """How long the panel went without changing, between updates."""
        return [b[0] - a[0] for a, b in zip(self.updates, self.updates[1:])]

    @property
    def median_gap_s(self) -> float | None:
        gaps = self.gaps
        return statistics.median(gaps) if gaps else None

    @property
    def worst_gap_s(self) -> float | None:
        gaps = self.gaps
        return max(gaps) if gaps else None

    def report(self) -> list[str]:
        """The measurement as lines, for `dictate captions` to print."""
        when = ("never - the model produced no words at all"
                if self.first_words_s is None
                else f"{self.first_words_s:.2f}s into the recording")
        keeps_up = ("YES" if self.rtf < 1.0 else
                    "NO - it cannot produce captions as fast as you speak")
        lines = [
            f"audio length      {self.audio_s:.1f}s in {self.blocks} blocks",
            f"first words       {when}",
        ]
        if self.median_gap_s is not None:
            lines.append(f"updates every     {self.median_gap_s:.2f}s "
                         f"(worst gap {self.worst_gap_s:.2f}s, "
                         f"{len(self.updates)} in total)")
        lines += [
            f"decoding cost     {self.decode_s:.2f}s of CPU for "
            f"{self.audio_s:.1f}s of audio (RTF {self.rtf:.3f})",
            f"keeps up          {keeps_up}",
        ]
        return lines


def measure(start_session: Callable[[], object], pcm: bytes, *,
            sample_rate: int = 16000, block_ms: int = 32,
            clock: Callable[[], float] = time.perf_counter) -> CaptionMeasurement:
    """Put `pcm` through one caption session, block by block, and time it.

    `start_session` is `StreamingTranscriber.start_session`; the session is
    closed before returning, whatever happens, because a session holds the
    caption text and closing it is what destroys it.
    """
    block_bytes = max(2, int(sample_rate * block_ms / 1000) * 2)
    audio_s = len(pcm) / 2 / sample_rate
    session = start_session()
    updates: list[tuple[float, str]] = []
    decode_s = 0.0
    last = ""
    blocks = 0
    try:
        for offset in range(0, len(pcm), block_bytes):
            block = pcm[offset:offset + block_bytes]
            blocks += 1
            # The audio this block ENDS at: the words it produced could not have
            # been on screen any earlier than that, whatever the CPU did.
            at = min(audio_s, (offset + len(block)) / 2 / sample_rate)
            t0 = clock()
            session.accept(block)
            text = session.text()
            decode_s += clock() - t0
            if text != last:
                updates.append((round(at, 3), text))
                last = text
        final = session.text()
    finally:
        try:
            session.close()
        except Exception:  # noqa: BLE001 - a measurement may not raise on the way out
            pass
    return CaptionMeasurement(
        audio_s=audio_s,
        blocks=blocks,
        first_words_s=updates[0][0] if updates else None,
        updates=updates,
        rtf=decode_s / audio_s if audio_s else 0.0,
        decode_s=decode_s,
        text=final,
    )
