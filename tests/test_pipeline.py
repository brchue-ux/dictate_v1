"""The state machine, end to end, with no microphone and no GPU.

This is the test that matters most: it exercises the whole flow - press, audio,
captions, release, transcribe, clean, paste - and every error path through it,
on a machine with none of the hardware the product needs.
"""

from __future__ import annotations

import threading
import unittest
from pathlib import Path

from dictate import pipeline as pipeline_mod
from dictate.cleanup.engine import CleanResult
from dictate.errors import InjectionError, TranscriptionError
from dictate.pipeline import Pipeline, PipelineState, Utterance, caption_tail
from dictate.platform.base import OverlayState, TargetWindow

from .fakes import (
    DeferredSubmit,
    FakeBatch,
    FakeInjector,
    FakeOverlay,
    FakeStreaming,
    FakeWindows,
    InlineSubmit,
)

SR = 16000


def audio(ms: int) -> bytes:
    """`ms` milliseconds of PCM16 silence."""
    return b"\x00\x00" * int(SR * ms / 1000)


def passthrough(text: str) -> CleanResult:
    return CleanResult(text=text.strip(), original=text)


class PipelineTestCase(unittest.TestCase):
    def build(self, **kwargs) -> Pipeline:
        self.batch = kwargs.pop("batch", FakeBatch())
        self.injector = kwargs.pop("injector", FakeInjector())
        self.windows = kwargs.pop("windows", FakeWindows())
        self.overlay = kwargs.pop("overlay", FakeOverlay())
        self.streaming = kwargs.pop("streaming", FakeStreaming())
        self.submit = kwargs.pop("submit", InlineSubmit())
        self.notices: list[tuple[str, str]] = []
        return Pipeline(
            batch=self.batch,
            cleaner=kwargs.pop("cleaner", passthrough),
            injector=self.injector,
            windows=self.windows,
            overlay=self.overlay,
            streaming=self.streaming,
            submit=self.submit,
            notify=lambda lvl, msg: self.notices.append((lvl, msg)),
            sample_rate=SR,
            **kwargs,
        )

    def drain(self, pipeline: Pipeline, limit: int = 500) -> None:
        for _ in range(limit):
            if not pipeline.pump_captions(timeout=0):
                return


class HappyPath(PipelineTestCase):
    def test_full_utterance_is_transcribed_cleaned_and_pasted(self):
        p = self.build()
        self.assertEqual(p.state, PipelineState.IDLE)

        self.assertTrue(p.start_utterance())
        self.assertEqual(p.state, PipelineState.RECORDING)
        p.push_audio(audio(500))
        p.push_audio(audio(500))
        self.drain(p)
        self.assertTrue(p.finish_utterance())

        self.assertEqual(self.injector.sent, [("Hello world.", self.windows.window)])
        self.assertEqual(p.state, PipelineState.IDLE)
        self.assertEqual(p.completed, 1)
        # A full second of audio at 16 kHz mono 16-bit is 32000 bytes.
        self.assertEqual(self.batch.calls, [(32000, SR)])

    def test_target_window_is_captured_at_press_not_at_paste(self):
        p = self.build()
        p.start_utterance()
        pressed_window = self.windows.window
        # Focus moves while the user is still speaking - a notification, or the
        # overlay itself if it were ever to misbehave.
        self.windows.window = TargetWindow(handle=999, title="Something Else")
        p.push_audio(audio(600))
        p.finish_utterance()

        text, target = self.injector.sent[0]
        self.assertEqual(target, pressed_window)
        self.assertNotEqual(target.handle, 999)

    def test_the_overlay_is_told_which_window_the_text_is_going_to(self):
        """It is the same handle the paste will use, and it is what decides which
        monitor the captions appear on - so the words come up on the screen he is
        working on rather than always on the primary one."""
        p = self.build()
        pressed_window = self.windows.window
        p.start_utterance()
        self.windows.window = TargetWindow(handle=999, title="Something Else")
        p.push_audio(audio(600))
        p.finish_utterance()

        listening = [t for (state, _), t in zip(self.overlay.history,
                                                self.overlay.targets)
                     if state is OverlayState.LISTENING]
        self.assertEqual(listening[0], pressed_window)
        _, pasted_into = self.injector.sent[0]
        self.assertEqual(listening[0], pasted_into)

    def test_captions_appear_while_recording(self):
        p = self.build()
        p.start_utterance()
        p.push_audio(audio(320))
        p.push_audio(audio(320))
        self.drain(p)
        self.assertEqual(self.overlay.captions, ["CHUNK1", "CHUNK1 CHUNK2"])

    def test_overlay_sequence_is_listen_think_done(self):
        p = self.build()
        p.start_utterance()
        p.push_audio(audio(600))
        self.drain(p)
        p.finish_utterance()
        states = self.overlay.states
        self.assertEqual(states[0], OverlayState.LISTENING)
        self.assertIn(OverlayState.THINKING, states)
        self.assertEqual(states[-1], OverlayState.DONE)


class TheCaptionStaysUpUntilTheTextLands(PipelineTestCase):
    """What he asked for: "captions should stay up while I'm talking, and it
    thinks, until it's inserted"."""

    def test_the_words_are_still_on_screen_while_the_gpu_works(self):
        p = self.build(submit=DeferredSubmit())
        p.start_utterance()
        p.push_audio(audio(600))
        self.drain(p)
        spoken = self.overlay.showing
        self.assertTrue(spoken)

        p.finish_utterance()               # he lets go; transcription is pending
        state, showing = self.overlay.screen[-1]
        self.assertIs(state, OverlayState.THINKING)
        self.assertEqual(showing, spoken)  # the same words, still there

    def test_release_asks_for_no_text_at_all_rather_than_re_sending_it(self):
        """The mechanism, not just the effect. The pipeline holds no caption
        text, so the only thing it CAN do here is leave the screen alone."""
        p = self.build(submit=DeferredSubmit())
        p.start_utterance()
        p.push_audio(audio(600))
        self.drain(p)
        p.finish_utterance()
        state, asked_for = self.overlay.history[-1]
        self.assertIs(state, OverlayState.THINKING)
        self.assertIsNone(asked_for)

    def test_the_words_go_when_the_text_lands_and_not_before(self):
        submit = DeferredSubmit()
        p = self.build(submit=submit)
        p.start_utterance()
        p.push_audio(audio(600))
        self.drain(p)
        p.finish_utterance()
        self.assertTrue(self.overlay.showing)   # still up, still nothing pasted
        self.assertEqual(self.injector.sent, [])

        submit.run_all()
        state, showing = self.overlay.screen[-1]
        self.assertIs(state, OverlayState.DONE)
        self.assertEqual(showing, "")
        self.assertEqual(len(self.injector.sent), 1)

    def test_a_failure_replaces_the_words_with_what_went_wrong(self):
        """The one thing worse than an empty panel is one that goes on showing
        him a sentence that is never going to be pasted."""
        p = self.build(batch=FakeBatch(error=TranscriptionError("gone", "restart")))
        p.start_utterance()
        p.push_audio(audio(600))
        self.drain(p)
        p.finish_utterance()
        state, showing = self.overlay.screen[-1]
        self.assertIs(state, OverlayState.ERROR)
        self.assertNotIn("CHUNK", showing)

    def test_a_mis_press_takes_the_panel_away_rather_than_leaving_it_thinking(self):
        p = self.build(min_utterance_ms=350)
        p.start_utterance()
        p.push_audio(audio(100))
        p.finish_utterance()
        state, showing = self.overlay.screen[-1]
        self.assertIs(state, OverlayState.HIDDEN)
        self.assertEqual(showing, "")

    def test_nothing_carries_over_from_one_dictation_to_the_next(self):
        p = self.build()
        p.start_utterance()
        p.push_audio(audio(600))
        self.drain(p)
        p.finish_utterance()

        p.start_utterance()
        state, showing = self.overlay.screen[-1]
        self.assertIs(state, OverlayState.LISTENING)
        self.assertEqual(showing, "")


class CaptionsCanNeverBePasted(PipelineTestCase):
    """Constraint 4: caption text is display-only.

    It used to be held by clearing the screen at release, and four tests here
    asserted that timing. The timing has changed - the words now stay up until
    the real text lands - so these assert the guarantee itself: whatever is on
    screen, and whenever, the caption cannot reach his document. The enforcement
    is structural, and each test names the part of the structure it holds.
    """

    def test_caption_text_never_reaches_the_injector(self):
        p = self.build()
        p.start_utterance()
        p.push_audio(audio(600))
        self.drain(p)
        self.assertTrue(self.overlay.captions)          # captions were shown
        p.finish_utterance()
        pasted = self.injector.sent[0][0]
        self.assertEqual(pasted, "Hello world.")         # from the batch engine
        self.assertNotIn("CHUNK", pasted)                # never the caption text

    def test_the_words_on_screen_at_the_moment_of_the_paste_are_not_the_paste(self):
        """The new timing's own case: the caption is still up, in front of him,
        while the text is being delivered. It is still not what is delivered."""
        submit = DeferredSubmit()
        p = self.build(submit=submit)
        p.start_utterance()
        p.push_audio(audio(600))
        self.drain(p)
        p.finish_utterance()
        on_screen = self.overlay.showing
        self.assertIn("CHUNK", on_screen)

        submit.run_all()
        pasted, _ = self.injector.sent[0]
        self.assertEqual(pasted, "Hello world.")
        self.assertNotEqual(pasted, on_screen)

    def test_the_pipeline_never_holds_the_caption_text(self):
        """Why the two tests above cannot start failing quietly: there is no
        copy of the caption text in here to paste by accident. It goes from the
        decoder to the screen and is not kept."""
        p = self.build(submit=DeferredSubmit())
        p.start_utterance()
        p.push_audio(audio(600))
        self.drain(p)
        p.finish_utterance()
        self.assertIn("CHUNK", self.overlay.showing)     # it IS on screen

        held = [f"{name} = {value!r}" for name, value in vars(p).items()
                if "CHUNK" in repr(value)]
        self.assertEqual(held, [])

    def test_the_finalise_worker_is_handed_audio_and_no_text_at_all(self):
        """The value that crosses into the paste path carries PCM, a sample
        rate, a window and two numbers. There is no field for a string to
        travel in, which is why no string can."""
        import dataclasses

        text_fields = [f.name for f in dataclasses.fields(Utterance)
                       if f.type in ("str", str)]
        self.assertEqual(text_fields, [])

    def test_the_injector_is_called_from_exactly_one_place(self):
        """A second call site is how a rule like this rots. The one that exists
        is in `_finalize`, and what it sends comes out of `batch.transcribe`."""
        source = (Path(pipeline_mod.__file__)).read_text(encoding="utf-8")
        body = source.split("def _finalize(", 1)[1]
        self.assertEqual(source.count("injector.send("), 1)
        self.assertIn("self.injector.send(final, utt.target)", body)

    def test_the_streaming_session_is_closed_and_emptied_on_release(self):
        """The decoder that made the words on screen is shut on release, so the
        text that is still visible cannot be asked for again by anything."""
        p = self.build()
        p.start_utterance()
        p.push_audio(audio(600))
        self.drain(p)
        p.finish_utterance()
        self.drain(p)  # the close sentinel is handled by the pump thread
        session = self.streaming.sessions[0]
        self.assertTrue(session.closed)
        self.assertEqual(session.text(), "")
        self.assertEqual(session.chunks, [])

    def test_audio_queued_before_release_never_updates_the_screen_after_it(self):
        p = self.build()
        p.start_utterance()
        p.push_audio(audio(320))       # queued, not yet decoded
        p.finish_utterance()           # released before the pump ran
        before = len(self.overlay.history)
        self.drain(p)
        new_captions = [t for s, t in self.overlay.history[before:]
                        if s is OverlayState.LISTENING]
        self.assertEqual(new_captions, [])

    def test_the_caption_is_gone_before_the_panel_says_the_text_landed(self):
        """"pasted" and a sentence on screen together is the one arrangement in
        which the caption could be taken for what was pasted."""
        p = self.build()
        p.start_utterance()
        p.push_audio(audio(600))
        self.drain(p)
        p.finish_utterance()
        shown_when_done = [t for s, t in self.overlay.screen
                           if s is OverlayState.DONE]
        self.assertTrue(shown_when_done)
        self.assertTrue(all(t == "" for t in shown_when_done))


class WhatGoesIntoTheHistory(PipelineTestCase):
    """The pipeline's whole share of the dictation history: it hands over what
    landed, once, and only when something did."""

    def build(self, **kwargs) -> Pipeline:
        self.recorded: list[dict] = []
        kwargs.setdefault("record", lambda text, **kw: self.recorded.append(
            dict(text=text, **kw)))
        return super().build(**kwargs)

    def test_a_delivered_dictation_is_recorded_with_the_text_that_landed(self):
        p = self.build(cleaner=lambda text: CleanResult(text="Hello world.",
                                                        original=text))
        p.start_utterance()
        p.push_audio(audio(600))
        p.finish_utterance()
        self.assertEqual(len(self.recorded), 1)
        self.assertEqual(self.recorded[0]["text"], "Hello world.")
        self.assertEqual(self.recorded[0]["text"], self.injector.sent[0][0])

    def test_it_carries_what_whisper_said_before_the_rules_ran(self):
        """The raw/cleaned pair is the evidence for "it ate a word", which is
        the one argument a history has to be able to settle."""
        p = self.build(batch=FakeBatch("Um, hello world."),
                       cleaner=lambda text: CleanResult(text="Hello world.",
                                                        original=text))
        p.start_utterance()
        p.push_audio(audio(600))
        p.finish_utterance()
        self.assertEqual(self.recorded[0]["raw"], "Um, hello world.")

    def test_it_carries_how_long_he_spoke_for(self):
        p = self.build()
        p.start_utterance()
        p.push_audio(audio(1000))
        p.finish_utterance()
        self.assertAlmostEqual(self.recorded[0]["spoke_s"], 1.0, places=2)

    def test_nothing_is_recorded_when_nothing_was_pasted(self):
        for case, kwargs in (
            ("transcription failed",
             dict(batch=FakeBatch(error=TranscriptionError("gone", "restart")))),
            ("the paste failed",
             dict(injector=FakeInjector(error=InjectionError("no", "try again")))),
            ("nothing was heard", dict(batch=FakeBatch(""))),
        ):
            with self.subTest(case=case):
                p = self.build(**kwargs)
                p.start_utterance()
                p.push_audio(audio(600))
                p.finish_utterance()
                self.assertEqual(self.recorded, [])

    def test_a_mis_press_is_not_a_dictation(self):
        p = self.build(min_utterance_ms=350)
        p.start_utterance()
        p.push_audio(audio(100))
        p.finish_utterance()
        self.assertEqual(self.recorded, [])

    def test_the_caption_text_is_not_offered_to_it_either(self):
        """It is a record of what he dictated. What was on screen while he
        dictated is not that, and constraint 4 applies to a file as much as to
        a document."""
        p = self.build()
        p.start_utterance()
        p.push_audio(audio(600))
        self.drain(p)
        p.finish_utterance()
        self.assertNotIn("CHUNK", repr(self.recorded))

    def test_a_history_that_cannot_be_written_does_not_cost_him_the_dictation(self):
        def explode(text, **kwargs):
            raise OSError("the disk is full")

        p = self.build(record=explode)
        p.start_utterance()
        p.push_audio(audio(600))
        p.finish_utterance()
        self.assertEqual(self.injector.sent[0][0], "Hello world.")
        self.assertEqual(p.completed, 1)
        self.assertEqual([lvl for lvl, _ in self.notices], [])
        self.assertIs(self.overlay.states[-1], OverlayState.DONE)


class ShortAndEmpty(PipelineTestCase):
    def test_a_tap_of_the_hotkey_transcribes_nothing(self):
        p = self.build(min_utterance_ms=350)
        p.start_utterance()
        p.push_audio(audio(100))
        p.finish_utterance()
        self.assertEqual(self.batch.calls, [])
        self.assertEqual(self.injector.sent, [])
        self.assertEqual(p.state, PipelineState.IDLE)
        self.assertEqual(self.overlay.states[-1], OverlayState.HIDDEN)

    def test_press_with_no_audio_at_all(self):
        p = self.build()
        p.start_utterance()
        p.finish_utterance()
        self.assertEqual(self.injector.sent, [])
        self.assertEqual(p.state, PipelineState.IDLE)

    def test_silence_transcribed_as_nothing_pastes_nothing(self):
        p = self.build(batch=FakeBatch(text="   "))
        p.start_utterance()
        p.push_audio(audio(600))
        p.finish_utterance()
        self.assertEqual(self.injector.sent, [])
        self.assertTrue(any("did not hear" in m for _, m in self.notices))

    def test_the_utterance_ceiling_stops_recording_and_still_transcribes(self):
        p = self.build(max_utterance_s=0.5)
        p.start_utterance()
        p.push_audio(audio(1000))     # twice the ceiling
        self.assertFalse(p.is_recording)
        self.assertEqual(len(self.batch.calls), 1)
        self.assertEqual(self.batch.calls[0][0], int(SR * 0.5) * 2)
        self.assertTrue(any("limit" in m for _, m in self.notices))


class ErrorPaths(PipelineTestCase):
    def test_a_transcription_failure_is_reported_and_nothing_is_pasted(self):
        p = self.build(batch=FakeBatch(error=TranscriptionError(
            "server said no", "try again")))
        p.start_utterance()
        p.push_audio(audio(600))
        p.finish_utterance()
        self.assertEqual(self.injector.sent, [])
        self.assertEqual(p.state, PipelineState.IDLE)      # recovered
        self.assertEqual(self.overlay.states[-1], OverlayState.ERROR)
        self.assertTrue(any(lvl == "error" and "server said no" in m
                            for lvl, m in self.notices))

    def test_a_paste_failure_is_reported_and_the_app_keeps_working(self):
        p = self.build(injector=FakeInjector(error=InjectionError("no focus", "click it")))
        p.start_utterance()
        p.push_audio(audio(600))
        p.finish_utterance()
        self.assertEqual(p.state, PipelineState.IDLE)
        self.assertTrue(any(lvl == "error" for lvl, _ in self.notices))
        # and the next utterance still works
        self.injector.error = None
        p.start_utterance()
        p.push_audio(audio(600))
        p.finish_utterance()
        self.assertEqual(len(self.injector.sent), 1)

    def test_an_unexpected_exception_does_not_leave_the_pipeline_stuck(self):
        p = self.build(batch=FakeBatch(error=RuntimeError("boom")))
        p.start_utterance()
        p.push_audio(audio(600))
        p.finish_utterance()
        self.assertEqual(p.state, PipelineState.IDLE)
        self.assertTrue(any(lvl == "error" for lvl, _ in self.notices))

    def test_captions_failing_to_start_does_not_stop_the_dictation(self):
        streaming = FakeStreaming()
        streaming.raise_on_start = RuntimeError("model file vanished")
        p = self.build(streaming=streaming)
        p.start_utterance()
        p.push_audio(audio(600))
        p.finish_utterance()
        self.assertEqual(self.injector.sent, [("Hello world.", self.windows.window)])

    def test_captions_crashing_mid_utterance_does_not_stop_the_dictation(self):
        streaming = FakeStreaming()
        p = self.build(streaming=streaming)
        p.start_utterance()
        p.push_audio(audio(320))
        streaming.raise_on_accept = RuntimeError("onnx exploded")
        p.push_audio(audio(320))
        self.drain(p)
        self.assertTrue(p.is_recording)          # still recording
        p.push_audio(audio(320))
        p.finish_utterance()
        self.assertEqual(self.injector.sent, [("Hello world.", self.windows.window)])
        self.assertTrue(any("captions stopped" in t for _, t in self.overlay.history))

    def test_an_unreadable_focused_window_still_dictates(self):
        windows = FakeWindows()
        windows.raise_on_foreground = OSError("no window")
        p = self.build(windows=windows)
        p.start_utterance()
        p.push_audio(audio(600))
        p.finish_utterance()
        self.assertEqual(self.injector.sent, [("Hello world.", None)])

    def test_cleanup_rejection_is_surfaced_but_the_text_still_lands(self):
        def rejecting(text: str) -> CleanResult:
            return CleanResult(text=text, original=text,
                               rejected_reason="a rule tried to invent a word")

        p = self.build(cleaner=rejecting)
        p.start_utterance()
        p.push_audio(audio(600))
        p.finish_utterance()
        self.assertEqual(self.injector.sent, [("Hello world.", self.windows.window)])
        self.assertTrue(any("invent" in m for _, m in self.notices))


class Sequencing(PipelineTestCase):
    def test_a_second_press_while_recording_is_ignored(self):
        p = self.build()
        self.assertTrue(p.start_utterance())
        self.assertFalse(p.start_utterance())
        self.assertEqual(len(self.streaming.sessions), 1)

    def test_release_without_press_is_ignored(self):
        p = self.build()
        self.assertFalse(p.finish_utterance())
        self.assertEqual(self.batch.calls, [])

    def test_audio_arriving_after_release_is_dropped(self):
        p = self.build()
        p.start_utterance()
        p.push_audio(audio(600))
        p.finish_utterance()
        p.push_audio(audio(600))            # a late block from the audio thread
        self.assertEqual(len(self.batch.calls), 1)

    def test_a_new_utterance_can_start_while_the_previous_one_is_transcribing(self):
        deferred = DeferredSubmit()
        p = self.build(submit=deferred)
        p.start_utterance()
        p.push_audio(audio(600))
        p.finish_utterance()
        self.assertEqual(p.state, PipelineState.FINALIZING)

        self.assertTrue(p.start_utterance())   # user starts talking again
        p.push_audio(audio(600))
        p.finish_utterance()

        deferred.run_all()
        self.assertEqual(len(self.injector.sent), 2)
        self.assertEqual(p.state, PipelineState.IDLE)

    def test_cancel_discards_everything(self):
        p = self.build()
        p.start_utterance()
        p.push_audio(audio(600))
        self.assertTrue(p.cancel_utterance("test"))
        self.assertEqual(self.batch.calls, [])
        self.assertEqual(self.injector.sent, [])
        self.assertEqual(p.state, PipelineState.IDLE)
        self.assertEqual(self.overlay.states[-1], OverlayState.HIDDEN)

    def test_close_is_safe_and_stops_further_recording(self):
        p = self.build()
        p.start_utterance()
        p.close()
        self.assertFalse(p.start_utterance())


class Concurrency(PipelineTestCase):
    def test_audio_thread_and_caption_thread_do_not_corrupt_each_other(self):
        """The real layout: one thread pushing audio, one pumping captions,
        while the main thread starts and stops utterances."""
        p = self.build()
        stop = threading.Event()
        errors: list[BaseException] = []

        def pump():
            try:
                while not stop.is_set():
                    p.pump_captions(timeout=0.01)
            except BaseException as exc:      # noqa: BLE001 - reported below
                errors.append(exc)

        def feed():
            try:
                while not stop.is_set():
                    p.push_audio(audio(32))
            except BaseException as exc:      # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=pump), threading.Thread(target=feed)]
        for t in threads:
            t.start()
        try:
            for _ in range(25):
                p.start_utterance()
                p.finish_utterance()
        finally:
            stop.set()
            for t in threads:
                t.join(timeout=5)
        self.assertEqual(errors, [])
        self.assertEqual(p.state, PipelineState.IDLE)


class CaptionTail(unittest.TestCase):
    def test_short_text_is_untouched(self):
        self.assertEqual(caption_tail("hello", 20), "hello")

    def test_long_text_keeps_the_end(self):
        text = "one two three four five six seven eight nine ten"
        out = caption_tail(text, 20)
        self.assertTrue(out.startswith("… "))
        self.assertTrue(text.endswith(out.removeprefix("… ")))
        self.assertLessEqual(len(out), 24)

    def test_exactly_at_the_limit(self):
        self.assertEqual(caption_tail("a" * 20, 20), "a" * 20)


if __name__ == "__main__":
    unittest.main()
