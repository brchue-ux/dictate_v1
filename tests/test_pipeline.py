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
        """Focus slipping and coming back is not a focus change.

        The window is read at press, so a notification that steals focus for a
        moment cannot move the paste - and by the time the words are ready he is
        back in the window he started in, which is the only thing the delivery
        decision looks at. What moves the paste is being somewhere ELSE at that
        moment, which is `FocusMovedWhileHeWasSpeaking` below.
        """
        p = self.build()
        pressed_window = self.windows.window
        p.start_utterance()
        self.windows.window = TargetWindow(handle=999, title="A Notification")
        p.push_audio(audio(600))
        self.windows.window = pressed_window          # it went away again
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
        self.windows.window = pressed_window
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
        rate, a window and four numbers. There is no field for a string to
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

    def test_it_carries_whether_pasting_it_pressed_return(self):
        """The stray-Enter defect's own line in the history. Ordinarily it is
        zero, because `[paste] line_breaks` will not let it be anything else -
        and what the injector reports is what goes down, so if it ever is not
        zero he can find out which dictation it was."""
        class Submitting(FakeInjector):
            def send(self, text, target):
                super().send(text, target)
                return 2

        p = self.build(injector=Submitting())
        p.start_utterance()
        p.push_audio(audio(600))
        p.finish_utterance()
        self.assertEqual(self.recorded[0]["returns"], 2)

    def test_an_injector_that_reports_nothing_is_not_an_error(self):
        p = self.build()          # FakeInjector.send returns None
        p.start_utterance()
        p.push_audio(audio(600))
        p.finish_utterance()
        self.assertEqual(self.recorded[0]["returns"], 0)

    def test_nothing_is_recorded_when_there_were_no_words(self):
        """A dictation with no text is not a dictation. Note what is NOT in this
        list any more: a paste that failed. There ARE words in that case, and
        losing them silently is the defect this file is now half the answer
        to - see `test_text_that_could_not_be_pasted_is_kept_and_marked`."""
        for case, kwargs in (
            ("transcription failed",
             dict(batch=FakeBatch(error=TranscriptionError("gone", "restart")))),
            ("nothing was heard", dict(batch=FakeBatch(""))),
        ):
            with self.subTest(case=case):
                p = self.build(**kwargs)
                p.start_utterance()
                p.push_audio(audio(600))
                p.finish_utterance()
                self.assertEqual(self.recorded, [])

    def test_text_that_could_not_be_pasted_is_kept_and_marked(self):
        """The history is the durable half of "never silently lost".

        The clipboard is the immediate half and it lasts until he copies
        anything else; this is what is still there an hour later. It has to be
        marked, or a line he never saw arrive reads as one that did.
        """
        p = self.build(injector=FakeInjector(
            error=InjectionError("Windows said no", "try again")))
        p.start_utterance()
        p.push_audio(audio(600))
        p.finish_utterance()
        self.assertEqual(len(self.recorded), 1)
        self.assertEqual(self.recorded[0]["text"], "Hello world.")
        self.assertIs(self.recorded[0]["delivered"], False)

    def test_a_delivered_dictation_says_it_was_delivered(self):
        p = self.build()
        p.start_utterance()
        p.push_audio(audio(600))
        p.finish_utterance()
        self.assertIs(self.recorded[0]["delivered"], True)

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


class FocusMovedWhileHeWasSpeaking(PipelineTestCase):
    """The reported bug, end to end.

    "If I'm currently dictating and I click away from the original focus screen
    that it breaks where it ends up pasting. Even if I type or if I click back
    into the terminal pane it won't paste it there any longer."

    What used to happen: the paste went back to the window he had left - raising
    it over whatever he had moved to - and if Windows would not raise it, the
    text was discarded. It was not on the clipboard, it was not in the history,
    and the audio had already been thrown away at the release, so there was
    nothing left anywhere and the only remedy was to say it all again.
    """

    def build(self, **kwargs) -> Pipeline:
        self.recorded: list[dict] = []
        kwargs.setdefault("record", lambda text, **kw: (
            self.recorded.append(dict(text=text, **kw)), True)[1])
        return super().build(**kwargs)

    def speak_then_move(self, p: Pipeline, to=None):
        p.start_utterance()
        p.push_audio(audio(600))
        self.windows.window = to or TargetWindow(handle=999, title="Firefox",
                                                 process="firefox.exe")
        p.finish_utterance()

    def test_nothing_is_pasted_anywhere(self):
        p = self.build()
        self.speak_then_move(p)
        self.assertEqual(self.injector.sent, [])

    def test_the_words_are_on_the_clipboard(self):
        p = self.build()
        self.speak_then_move(p)
        self.assertEqual(self.injector.kept, ["Hello world."])

    def test_the_words_are_in_the_history_marked_as_not_pasted(self):
        p = self.build()
        self.speak_then_move(p)
        self.assertEqual(self.recorded[0]["text"], "Hello world.")
        self.assertIs(self.recorded[0]["delivered"], False)

    def test_he_is_told_and_the_message_names_both_windows(self):
        p = self.build()
        self.speak_then_move(p)
        said = "\n".join(m for _, m in self.notices)
        self.assertIn("Not pasted", said)
        self.assertIn("Notepad", said)        # where he pressed the hotkey
        self.assertIn("Firefox", said)        # where he is now
        self.assertIn("Ctrl+V", said)

    def test_the_panel_says_it_rather_than_saying_pasted(self):
        p = self.build()
        self.speak_then_move(p)
        state, shown = self.overlay.screen[-1]
        self.assertIs(state, OverlayState.ERROR)
        self.assertIn("Not pasted", shown)

    def test_the_next_dictation_is_untouched(self):
        """The half of the report that says it does not recover. It does: the
        window is captured afresh at every press, and there is no state here
        that a held dictation leaves behind."""
        p = self.build()
        self.speak_then_move(p)
        p.start_utterance()                    # he is in Firefox now, and stays
        p.push_audio(audio(600))
        p.finish_utterance()
        self.assertEqual(len(self.injector.sent), 1)
        self.assertEqual(self.injector.sent[0][1].title, "Firefox")

    def test_clicking_back_before_the_words_arrive_pastes_as_usual(self):
        """The commonest accidental case, and it must not be a refusal: he
        clicked something, came back, and the text is ready while he is home."""
        p = self.build(submit=DeferredSubmit())
        started_in = self.windows.window
        p.start_utterance()
        p.push_audio(audio(600))
        self.windows.window = TargetWindow(handle=999, title="Firefox")
        p.finish_utterance()
        self.windows.window = started_in       # back before the GPU came back
        self.submit.run_all()
        self.assertEqual(self.injector.sent, [("Hello world.", started_in)])
        self.assertEqual(self.injector.kept, [])

    def test_the_window_he_was_dictating_into_closed(self):
        p = self.build()
        p.start_utterance()
        p.push_audio(audio(600))
        self.windows.closed.add(self.windows.window.handle)
        self.windows.window = TargetWindow(handle=999, title="Firefox")
        p.finish_utterance()
        self.assertEqual(self.injector.sent, [])
        said = "\n".join(m for _, m in self.notices)
        self.assertIn("closed", said)
        self.assertEqual(self.injector.kept, ["Hello world."])

    def test_restore_mode_pastes_into_the_window_he_started_in(self):
        p = self.build(on_focus_change="restore")
        started_in = self.windows.window
        self.speak_then_move(p)
        self.assertEqual(self.injector.sent, [("Hello world.", started_in)])
        self.assertEqual(self.injector.kept, [])
        self.assertIs(self.recorded[0]["delivered"], True)

    def test_restore_mode_says_out_loud_that_it_moved_a_window(self):
        """A window jumping in front of him is not a thing to do silently, even
        when it is the thing he asked for."""
        p = self.build(on_focus_change="restore")
        self.speak_then_move(p)
        self.assertTrue(any("brought" in m and "front" in m
                            for _, m in self.notices))

    def test_a_clipboard_that_refuses_leaves_the_history_and_says_so(self):
        injector = FakeInjector()
        injector.clipboard_fails = True
        p = self.build(injector=injector)
        self.speak_then_move(p)
        said = "\n".join(m for _, m in self.notices)
        self.assertNotIn("Ctrl+V", said)
        self.assertIn("dictation history", said)
        self.assertIs(self.recorded[0]["delivered"], False)

    def test_with_hold_to_clipboard_off_the_clipboard_is_not_touched(self):
        p = self.build(hold_to_clipboard=False)
        self.speak_then_move(p)
        self.assertEqual(self.injector.kept, [])
        self.assertIn("dictation history", "\n".join(m for _, m in self.notices))

    def test_a_clipboard_that_explodes_does_not_cost_him_the_history(self):
        class Exploding(FakeInjector):
            def to_clipboard(self, text):
                raise OSError("the clipboard is on fire")

        p = self.build(injector=Exploding())
        self.speak_then_move(p)
        self.assertIs(self.recorded[0]["delivered"], False)
        self.assertIn("dictation history", "\n".join(m for _, m in self.notices))


class APasteThatFailedUsedToLoseTheText(PipelineTestCase):
    """The other way the words used to disappear: the paste itself was refused.

    `restore_focus` could not raise the window, or SendInput was blocked by an
    elevated application. Either way `injector.send` raised, `_finalize` reported
    it, and the text went with the exception.
    """

    def build(self, **kwargs) -> Pipeline:
        self.recorded: list[dict] = []
        kwargs.setdefault("record", lambda text, **kw: (
            self.recorded.append(dict(text=text, **kw)), True)[1])
        return super().build(**kwargs)

    def run_one(self, error):
        p = self.build(injector=FakeInjector(error=error))
        p.start_utterance()
        p.push_audio(audio(600))
        p.finish_utterance()
        return p

    def test_the_text_is_kept_and_he_is_told_where(self):
        self.run_one(InjectionError("Windows accepted only 0 of 400 keystrokes",
                                    "run dictate as administrator"))
        self.assertEqual(self.injector.kept, ["Hello world."])
        self.assertIs(self.recorded[0]["delivered"], False)
        self.assertIn("Ctrl+V", "\n".join(m for _, m in self.notices))

    def test_what_windows_said_is_still_reported(self):
        """Holding the text must not swallow the reason. He needs the remedy -
        this one is fixable, and the message that fixes it is the injector's."""
        self.run_one(InjectionError("Windows accepted only 0 of 400 keystrokes",
                                    "run dictate as administrator"))
        said = "\n".join(m for _, m in self.notices)
        self.assertIn("Windows accepted only 0 of 400", said)
        self.assertIn("administrator", said)

    def test_a_half_finished_paste_warns_before_he_pastes_it_again(self):
        error = InjectionError("Windows accepted only 12 of 400 keystrokes", "")
        error.partial = True
        self.run_one(error)
        self.assertIn("already have been typed",
                      "\n".join(m for _, m in self.notices))

    def test_the_pipeline_keeps_working(self):
        p = self.run_one(InjectionError("no", "try again"))
        self.assertEqual(p.state, PipelineState.IDLE)
        self.injector.error = None
        p.start_utterance()
        p.push_audio(audio(600))
        p.finish_utterance()
        self.assertEqual(len(self.injector.sent), 1)


class TheHeldTextIsNotKeptHere(PipelineTestCase):
    """Constraint 4's shape, applied to the new path.

    A "last dictation" field on the pipeline would be the obvious way to offer
    "paste it now", and it is the first exception to "this module holds no
    text between utterances" - which is the property `CaptionsCanNeverBePasted`
    is enforced by. The held text goes straight out to the clipboard and the
    history and is not kept.
    """

    def test_nothing_of_the_held_dictation_stays_on_the_pipeline(self):
        p = self.build()
        p.start_utterance()
        p.push_audio(audio(600))
        self.windows.window = TargetWindow(handle=999, title="Firefox")
        p.finish_utterance()
        self.assertEqual(self.injector.kept, ["Hello world."])   # it WAS held
        held = [f"{name} = {value!r}" for name, value in vars(p).items()
                if "Hello world." in repr(value)]
        self.assertEqual(held, [])

    def test_the_clipboard_is_offered_the_text_from_exactly_one_place(self):
        """The same rule as `injector.send`: a second call site is how a
        guarantee about where text goes rots."""
        source = (Path(pipeline_mod.__file__)).read_text(encoding="utf-8")
        self.assertEqual(source.count("injector.to_clipboard("), 1)
        body = source.split("def _hold(", 1)[1]
        self.assertIn("self.injector.to_clipboard(final)", body)


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


class AudioThatWasThrownAway(PipelineTestCase):
    """Two ways audio goes missing, two consequences, two messages.

    Both used to be silent. The OS one was counted process-wide and logged on
    the 1st, 10th and 100th occurrence and then never again; the caption one was
    a `log.debug` nobody has ever read. So "some of what I said is missing" and
    "the captions were nonsense on that one" were both unanswerable after the
    fact. They are answered per utterance now, which is the only unit he can
    act on.
    """

    def test_input_the_os_discarded_is_reported_in_milliseconds(self):
        p = self.build()
        p.start_utterance()
        p.push_audio(audio(600))
        p.note_input_loss()
        p.note_input_loss()
        p.finish_utterance()

        warnings = [msg for level, msg in self.notices if level == "warning"]
        self.assertEqual(len(warnings), 1)
        # Two 32 ms blocks. The number he is given is time, not a block count.
        self.assertIn("64 ms", warnings[0])
        self.assertIn("pasted text may be missing a word", warnings[0])

    def test_the_loss_lands_on_the_utterance_the_worker_is_given(self):
        """So that anything downstream of the release - the history, a future
        confidence check - can see what this recording was made from, rather
        than having to ask a counter that has already moved on."""
        seen: list[Utterance] = []
        p = self.build()
        p._finalize = seen.append
        p.start_utterance()
        p.push_audio(audio(600))
        p.note_input_loss(3)
        p.finish_utterance()

        self.assertEqual(seen[0].input_lost, 3)
        self.assertEqual(seen[0].captions_dropped, 0)
        self.assertEqual(p.input_lost, 3)

    def test_dropped_caption_blocks_say_the_pasted_text_is_unaffected(self):
        p = self.build()
        p.start_utterance()
        # More blocks than the queue holds, with nothing pumping them out.
        for _ in range(pipeline_mod.CAPTION_QUEUE_BLOCKS + 5):
            p.push_audio(audio(32))
        p.finish_utterance()

        infos = [msg for level, msg in self.notices if level == "info"]
        self.assertEqual(len(infos), 1)
        self.assertIn("Live captions fell behind", infos[0])
        self.assertIn("What was pasted is unaffected", infos[0])
        self.assertGreater(p.captions_dropped, 0)

    def test_the_whole_recording_still_reaches_the_transcriber(self):
        """The point of the message above. Caption blocks are droppable; the
        buffer the GPU pass reads is not, and never was."""
        p = self.build()
        p.start_utterance()
        for _ in range(pipeline_mod.CAPTION_QUEUE_BLOCKS + 5):
            p.push_audio(audio(32))
        p.finish_utterance()
        sent_bytes, _ = self.batch.calls[0]
        self.assertEqual(sent_bytes,
                         len(audio(32)) * (pipeline_mod.CAPTION_QUEUE_BLOCKS + 5))

    def test_a_healthy_utterance_says_nothing_at_all(self):
        p = self.build()
        p.start_utterance()
        p.push_audio(audio(600))
        self.drain(p)
        p.finish_utterance()
        self.assertEqual(self.notices, [])

    def test_the_count_does_not_carry_into_the_next_utterance(self):
        p = self.build()
        p.start_utterance()
        p.push_audio(audio(600))
        p.note_input_loss(4)
        p.finish_utterance()
        self.notices.clear()

        p.start_utterance()
        p.push_audio(audio(600))
        p.finish_utterance()
        self.assertEqual(self.notices, [])

    def test_loss_while_he_is_not_dictating_is_counted_but_not_blamed_on_him(self):
        """The microphone stream is open whenever dictate is running, so an
        overflow at three in the afternoon may belong to nothing he said. It is
        still counted - that is the evidence that it happens while idle - but it
        is not attached to the next thing he says."""
        p = self.build()
        p.note_input_loss(9)
        p.start_utterance()
        p.push_audio(audio(600))
        p.finish_utterance()

        self.assertEqual(p.input_lost, 9)
        self.assertEqual(self.notices, [])

    def test_a_mispress_reports_nothing(self):
        """Below the floor nothing is transcribed and nothing is pasted, so
        there is nothing for a warning about missing words to be about."""
        p = self.build()
        p.start_utterance()
        p.push_audio(audio(50))
        p.note_input_loss(2)
        p.finish_utterance()
        self.assertEqual(self.notices, [])

    def test_the_running_app_actually_wires_the_listener_up(self):
        """Everything above passes whether or not anything ever calls
        `note_input_loss` on the real machine, and the one thing that does is a
        single argument in `app.py` on a line no test on this platform can
        execute. So it is read instead. Without it the counting is dead code and
        he is told nothing, exactly as before."""
        source = (Path(pipeline_mod.__file__).parent / "app.py").read_text(
            encoding="utf-8")
        self.assertIn(
            "self.audio.start(self.pipeline.push_audio, self.pipeline.note_input_loss)",
            source)

    def test_the_millisecond_figure_follows_the_configured_block_size(self):
        p = self.build(block_ms=64)
        p.start_utterance()
        p.push_audio(audio(600))
        p.note_input_loss(2)
        p.finish_utterance()
        warnings = [msg for level, msg in self.notices if level == "warning"]
        self.assertIn("128 ms", warnings[0])


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
