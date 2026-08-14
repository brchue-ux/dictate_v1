"""`dictate captions`, without a caption model.

The command exists so that the two numbers behind "the captions are wrong and
slow" can be measured on HIS machine and HIS voice. This holds the arithmetic
behind those numbers in place on any machine, with a session that produces
known words at known moments - because the thing that would make the command
useless is not a missing model, it is a number that is quietly wrong.
"""

from __future__ import annotations

import unittest

from dictate.engines.measure import CaptionMeasurement, measure

SR = 16000


def audio(ms: int) -> bytes:
    return b"\x00\x00" * int(SR * ms / 1000)


class Session:
    """Emits a new word after a set number of blocks, and counts closes."""

    def __init__(self, every: int = 10, words: int = 5, cost: float = 0.0) -> None:
        self.every = every
        self.words = words
        self.cost = cost
        self.blocks = 0
        self.closed = False
        self.clock = 0.0

    def accept(self, pcm: bytes) -> None:
        self.blocks += 1
        self.clock += self.cost

    def text(self) -> str:
        got = min(self.words, self.blocks // self.every)
        return " ".join(f"word{i}" for i in range(1, got + 1))

    def close(self) -> None:
        self.closed = True


class WhatItMeasures(unittest.TestCase):
    def run_one(self, session: Session, ms: int = 2000, **kwargs) -> CaptionMeasurement:
        return measure(lambda: session, audio(ms),
                       sample_rate=SR, clock=lambda: session.clock, **kwargs)

    def test_first_words_are_dated_by_the_recording_not_the_wall_clock(self):
        """A busy machine must not move this number. It is "how far into what
        you said", which is the thing he can compare against his own memory of
        holding the key."""
        session = Session(every=10, cost=0.5)   # half a second of CPU per block
        result = self.run_one(session)
        # Ten 32 ms blocks in: 0.32 s of audio, whatever the CPU took.
        self.assertAlmostEqual(result.first_words_s, 0.32, places=3)

    def test_the_update_interval_is_the_gap_between_changes(self):
        session = Session(every=10)
        result = self.run_one(session)
        self.assertAlmostEqual(result.median_gap_s, 0.32, places=3)
        self.assertAlmostEqual(result.worst_gap_s, 0.32, places=3)
        self.assertEqual(len(result.updates), 5)

    def test_a_model_that_never_speaks_says_so_instead_of_dividing_by_zero(self):
        result = self.run_one(Session(words=0))
        self.assertIsNone(result.first_words_s)
        self.assertIsNone(result.median_gap_s)
        self.assertEqual(result.text, "")
        self.assertIn("never", "\n".join(result.report()))

    def test_rtf_is_decode_time_over_audio_time(self):
        session = Session(cost=0.01)            # 10 ms per 32 ms block
        result = self.run_one(session, ms=1000)
        self.assertEqual(result.blocks, 32)     # 1000 ms in 32 ms blocks, rounded
        self.assertAlmostEqual(result.rtf, 0.32, places=2)
        self.assertIn("keeps up          YES", "\n".join(result.report()))

    def test_a_model_slower_than_speech_is_named_as_such(self):
        result = self.run_one(Session(cost=0.05), ms=1000)
        self.assertGreater(result.rtf, 1.0)
        self.assertIn("keeps up          NO", "\n".join(result.report()))

    def test_the_block_size_is_the_microphone_s(self):
        """It feeds the model the way the running app feeds it. A measurement
        taken in whole-file gulps would flatter every streaming model."""
        session = Session()
        self.run_one(session, ms=1000, block_ms=64)
        self.assertEqual(session.blocks, 16)

    def test_the_session_is_closed_even_when_the_model_throws(self):
        """A session holds the caption text and closing it is what destroys it.
        A measurement that leaked one would leave caption text alive after the
        thing that produced it was gone."""
        session = Session()

        def explode(_pcm):
            raise RuntimeError("onnxruntime said no")

        session.accept = explode
        with self.assertRaises(RuntimeError):
            self.run_one(session)
        self.assertTrue(session.closed)

    def test_the_session_is_closed_on_the_ordinary_path_too(self):
        session = Session()
        self.run_one(session)
        self.assertTrue(session.closed)

    def test_no_audio_at_all_is_not_a_crash(self):
        session = Session()
        result = measure(lambda: session, b"", sample_rate=SR)
        self.assertEqual(result.blocks, 0)
        self.assertEqual(result.rtf, 0.0)
        self.assertIsNone(result.first_words_s)


if __name__ == "__main__":
    unittest.main()
