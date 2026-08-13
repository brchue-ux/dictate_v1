"""Idle VRAM release, and every awkward moment around it.

That the graphics card genuinely gets its 1.6 GB back cannot be observed here -
there is no GPU on any machine this runs on. What CAN be observed, and is, is
everything that decides *when* to hand it back and *when* to take it again: the
timer, the warm-up that starts at hotkey press, and every collision between a
press and a transition that was already running. Those are where the bugs would
be, and none of them need hardware.

The clock is injected so the five-minute timer is tested by arithmetic rather
than by waiting, and the fake backend has gates on start/stop/transcribe so a
test can hold the model mid-load for as long as it likes and press the hotkey
while it is there.
"""

from __future__ import annotations

import threading
import time
import unittest

from dictate.cleanup.engine import CleanResult
from dictate.engines.residency import Residency, ResidentModel, watch_interval
from dictate.errors import BackendUnavailableError
from dictate.pipeline import Pipeline
from dictate.platform.base import OverlayState

from .fakes import FakeBatch, FakeInjector, FakeOverlay, FakeWindows, InlineSubmit

SR = 16000
PCM = b"\x00\x00" * SR          # one second of silence
FIVE_MINUTES = 300.0


def wait_for(predicate, timeout: float = 5.0, interval: float = 0.005) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class Clock:
    """A clock the test moves by hand, so "five minutes later" costs nothing."""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ResidencyTestCase(unittest.TestCase):
    def build(self, **kwargs) -> ResidentModel:
        self.batch = kwargs.pop("batch", FakeBatch())
        self.clock = kwargs.pop("clock", Clock())
        model = ResidentModel(
            self.batch,
            idle_release_s=kwargs.pop("idle_release_s", FIVE_MINUTES),
            clock=self.clock,
            poll_interval_s=kwargs.pop("poll_interval_s", 0.01),
            **kwargs,
        )
        # Reverse order: the gates are opened first, then the model is stopped,
        # so a test that leaves something held does not stall teardown.
        self.addCleanup(model.stop)
        self.addCleanup(self.open_gates)
        return model

    def open_gates(self) -> None:
        for gate in (self.batch.start_gate, self.batch.stop_gate,
                     self.batch.transcribe_gate):
            if gate is not None:
                gate.set()

    def assert_becomes(self, model: ResidentModel, state: Residency) -> None:
        self.assertTrue(
            wait_for(lambda: model.state is state),
            f"stayed {model.state.value}, expected {state.value}",
        )

    def assert_failed(self, model: ResidentModel) -> None:
        """Wait for a load to have been tried and failed - not merely for the
        state to be RELEASED, which it already was before the attempt."""
        self.assertTrue(wait_for(lambda: model.last_failure is not None),
                        "the load was never attempted, or it did not fail")
        self.assertIs(model.state, Residency.RELEASED)

    def release(self, model: ResidentModel) -> None:
        """Put the model through a real idle release and wait for it to land."""
        self.clock.advance(FIVE_MINUTES + 1)
        self.assertTrue(model.check_idle())
        self.assert_becomes(model, Residency.RELEASED)


# ---------------------------------------------------------------------------
# The timer
# ---------------------------------------------------------------------------


class TheIdleTimer(ResidencyTestCase):
    def test_the_model_is_loaded_at_start_and_held_until_the_idle_period_passes(self):
        model = self.build()
        model.start()
        self.assertIs(model.state, Residency.RESIDENT)
        self.assertEqual(self.batch.starts, 1)

        self.clock.advance(FIVE_MINUTES - 1)
        self.assertFalse(model.check_idle())
        self.assertIs(model.state, Residency.RESIDENT)

        self.clock.advance(2)
        self.assertTrue(model.check_idle())
        self.assert_becomes(model, Residency.RELEASED)
        self.assertEqual(self.batch.stops, 1)
        self.assertFalse(self.batch.running)

    def test_zero_minutes_means_the_model_is_never_released(self):
        model = self.build(idle_release_s=0.0)
        model.start()
        self.clock.advance(60 * 60 * 24)
        self.assertFalse(model.check_idle())
        self.assertIs(model.state, Residency.RESIDENT)
        self.assertTrue(self.batch.running)
        # ...and it does not tell him it will do something it will not do.
        self.assertNotIn("unloaded", model.describe)

    def test_dictating_resets_the_idle_clock(self):
        model = self.build()
        model.start()
        self.clock.advance(FIVE_MINUTES - 1)
        model.transcribe(PCM, SR)
        self.clock.advance(FIVE_MINUTES - 1)
        self.assertFalse(model.check_idle())
        self.clock.advance(2)
        self.assertTrue(model.check_idle())

    def test_pressing_the_hotkey_resets_the_idle_clock(self):
        model = self.build()
        model.start()
        self.clock.advance(FIVE_MINUTES - 1)
        model.note_press()
        self.clock.advance(FIVE_MINUTES - 1)
        self.assertFalse(model.check_idle())

    def test_the_model_is_never_released_while_a_transcription_is_running(self):
        model = self.build()
        model.start()
        gate = threading.Event()
        self.batch.transcribe_gate = gate

        done: list[str] = []
        worker = threading.Thread(target=lambda: done.append(model.transcribe(PCM, SR)))
        worker.start()
        self.addCleanup(worker.join, 10)
        self.assertTrue(self.batch.transcribe_entered.wait(timeout=5))

        # Five minutes pass while the GPU is still working on that utterance.
        self.clock.advance(FIVE_MINUTES + 1)
        self.assertFalse(model.check_idle())
        self.assertIs(model.state, Residency.RESIDENT)

        gate.set()
        worker.join(timeout=10)
        self.assertEqual(done, ["Hello world."])
        # Only once it is finished may the memory go back.
        self.clock.advance(FIVE_MINUTES + 1)
        self.assertTrue(model.check_idle())

    def test_the_watcher_thread_releases_without_anyone_asking_it_to(self):
        model = self.build(idle_release_s=0.05, clock=time.monotonic,
                           poll_interval_s=0.01)
        model.start()
        self.assert_becomes(model, Residency.RELEASED)

    def test_the_watch_interval_is_a_sensible_fraction_of_the_idle_period(self):
        self.assertEqual(watch_interval(FIVE_MINUTES), 10.0)
        self.assertEqual(watch_interval(60.0), 6.0)
        self.assertEqual(watch_interval(5.0), 1.0)      # never faster than 1 s
        self.assertEqual(watch_interval(60 * 60), 10.0)  # never slower than 10 s


# ---------------------------------------------------------------------------
# The point of the whole thing: the load runs while he is speaking
# ---------------------------------------------------------------------------


class WarmingStartsAtThePress(ResidencyTestCase):
    def test_the_load_begins_at_the_press_not_at_the_release(self):
        model = self.build()
        model.start()
        self.release(model)

        gate = threading.Event()
        self.batch.start_gate = gate
        self.batch.start_entered.clear()

        model.note_press()          # key DOWN. He is about to speak.

        # The load is already under way while the key is still held - which is
        # the whole reason he does not feel it.
        self.assertTrue(self.batch.start_entered.wait(timeout=5))
        self.assertIs(model.state, Residency.WARMING)

        gate.set()                  # the model finishes loading, still mid-sentence
        self.assert_becomes(model, Residency.RESIDENT)

        # ...so the utterance itself waits for nothing.
        self.assertEqual(model.transcribe(PCM, SR), "Hello world.")
        self.assertEqual(self.batch.starts, 2)

    def test_a_second_press_while_it_is_loading_does_not_start_a_second_server(self):
        model = self.build()
        model.start()
        self.release(model)

        gate = threading.Event()
        self.batch.start_gate = gate
        self.batch.start_entered.clear()
        model.note_press()
        self.assertTrue(self.batch.start_entered.wait(timeout=5))

        for _ in range(5):          # key repeat, or an impatient second press
            model.note_press()
        self.assertIs(model.state, Residency.WARMING)

        gate.set()
        self.assert_becomes(model, Residency.RESIDENT)
        self.assertEqual(self.batch.starts, 2)
        self.assertIsNone(model.last_failure)

    def test_an_utterance_that_ends_before_the_model_has_loaded_waits_for_it(self):
        model = self.build()
        model.start()
        self.release(model)

        gate = threading.Event()
        self.batch.start_gate = gate
        self.batch.start_entered.clear()
        model.note_press()
        self.assertTrue(self.batch.start_entered.wait(timeout=5))

        # He says two words and lets go while the model is still loading.
        done: list[str] = []
        worker = threading.Thread(target=lambda: done.append(model.transcribe(PCM, SR)))
        worker.start()
        self.addCleanup(worker.join, 10)

        # It waits rather than failing, and does not start a server of its own.
        self.assertFalse(wait_for(lambda: not worker.is_alive(), timeout=0.25),
                         "the transcription did not wait for the model")
        self.assertIs(model.state, Residency.WARMING)

        gate.set()
        worker.join(timeout=10)
        self.assertEqual(done, ["Hello world."])
        self.assertEqual(self.batch.starts, 2)

    def test_a_very_short_utterance_that_is_discarded_still_leaves_one_server(self):
        """Below the minimum length the pipeline never transcribes at all, so
        nothing else finishes the warm-up the press began. It must still land
        on a single, healthy, resident model - not a half-started one."""
        model = self.build()
        model.start()
        self.release(model)

        model.note_press()          # a tap: pressed and released in 100 ms
        self.assert_becomes(model, Residency.RESIDENT)
        self.assertEqual(self.batch.starts, 2)
        self.assertTrue(self.batch.running)

        # and it goes away again on the next idle period, as if nothing happened
        self.release(model)
        self.assertEqual(self.batch.stops, 2)

    def test_a_transcription_with_no_press_at_all_loads_the_model_itself(self):
        """`dictate transcribe`, or an utterance whose press-time warm-up could
        not be started. Slower, but it must never come back empty."""
        model = self.build()
        model.start()
        self.release(model)
        self.assertEqual(model.transcribe(PCM, SR), "Hello world.")
        self.assertIs(model.state, Residency.RESIDENT)
        self.assertEqual(self.batch.starts, 2)


# ---------------------------------------------------------------------------
# Collisions with a shutdown that is already running
# ---------------------------------------------------------------------------


class PressingDuringAShutdown(ResidencyTestCase):
    def begin_release(self, model: ResidentModel) -> threading.Event:
        gate = threading.Event()
        self.batch.stop_gate = gate
        self.clock.advance(FIVE_MINUTES + 1)
        self.assertTrue(model.check_idle())
        self.assertIs(model.state, Residency.RELEASING)
        return gate

    def test_a_press_during_the_shutdown_reloads_as_soon_as_it_finishes(self):
        model = self.build()
        model.start()
        gate = self.begin_release(model)

        model.note_press()          # he starts dictating mid-shutdown
        self.assertIs(model.state, Residency.RELEASING)

        gate.set()                  # the shutdown completes...
        self.assert_becomes(model, Residency.RESIDENT)   # ...and it comes straight back

        self.assertEqual(self.batch.stops, 1)
        self.assertEqual(self.batch.starts, 2)
        self.assertEqual(model.transcribe(PCM, SR), "Hello world.")

    def test_a_transcription_during_the_shutdown_waits_and_then_works(self):
        model = self.build()
        model.start()
        gate = self.begin_release(model)

        done: list[str] = []
        worker = threading.Thread(target=lambda: done.append(model.transcribe(PCM, SR)))
        worker.start()
        self.addCleanup(worker.join, 10)
        self.assertFalse(wait_for(lambda: not worker.is_alive(), timeout=0.25),
                         "the transcription did not wait for the shutdown")

        gate.set()
        worker.join(timeout=10)
        self.assertEqual(done, ["Hello world."])
        self.assertEqual(self.batch.starts, 2)
        self.assertEqual(self.batch.stops, 1)

    def test_two_utterances_that_overlap_share_one_load(self):
        model = self.build()
        model.start()
        self.release(model)

        gate = threading.Event()
        self.batch.start_gate = gate
        self.batch.start_entered.clear()

        done: list[str] = []
        lock = threading.Lock()

        def dictate():
            text = model.transcribe(PCM, SR)
            with lock:
                done.append(text)

        workers = [threading.Thread(target=dictate) for _ in range(2)]
        for worker in workers:
            worker.start()
        self.assertTrue(self.batch.start_entered.wait(timeout=5))
        gate.set()
        for worker in workers:
            worker.join(timeout=10)

        self.assertEqual(done, ["Hello world."] * 2)
        self.assertEqual(self.batch.starts, 2)   # one at app start, one reload
        self.assertIsNone(model.last_failure)

    def test_pressing_and_dictating_while_the_timer_fires_never_wedges(self):
        """The real thread layout, at speed: the hotkey thread, the finalize
        worker and the idle timer all reaching for the model at once, with the
        idle period short enough that they genuinely collide. The gaps between
        bursts are longer than the idle period, so the timer really does fire
        in the middle of all this rather than never getting a chance."""
        model = self.build(idle_release_s=0.02, clock=time.monotonic,
                           poll_interval_s=0.002)
        model.start()
        stop = threading.Event()
        errors: list[BaseException] = []

        def loop(action, gap):
            try:
                while not stop.is_set():
                    action()
                    stop.wait(gap)
            except BaseException as exc:      # noqa: BLE001 - reported below
                errors.append(exc)

        threads = [
            threading.Thread(target=loop, args=(model.note_press, 0.035)),
            threading.Thread(target=loop, args=(lambda: model.transcribe(PCM, SR), 0.055)),
            threading.Thread(target=loop, args=(lambda: model.transcribe(PCM, SR), 0.075)),
        ]
        for thread in threads:
            thread.start()
        time.sleep(2.0)
        stop.set()
        for thread in threads:
            thread.join(timeout=15)
            self.assertFalse(thread.is_alive(), "a caller is wedged")

        self.assertEqual(errors, [])
        # Two servers at once would have raised inside the fake, and been
        # recorded here as a load failure.
        self.assertIsNone(model.last_failure)
        self.assertGreater(model.releases, 0, "the timer never fired at all")
        self.assertGreater(model.loads, 1, "it never came back")
        self.assertTrue(wait_for(lambda: model.state is Residency.RELEASED, 10),
                        "it never settled")
        self.assertFalse(self.batch.running)


# ---------------------------------------------------------------------------
# A model that cannot come back
# ---------------------------------------------------------------------------


class WhenItCannotComeBack(ResidencyTestCase):
    def gone(self) -> BackendUnavailableError:
        return BackendUnavailableError(
            "The Whisper model was not found at C:\\dictate-gpu\\models\\turbo.bin",
            "Download it with scripts/fetch-models.ps1.",
        )

    def test_the_failure_is_reported_in_plain_language(self):
        model = self.build()
        model.start()
        self.release(model)
        self.batch.start_error = self.gone()

        with self.assertRaises(BackendUnavailableError) as ctx:
            model.transcribe(PCM, SR)

        report = ctx.exception.report()
        self.assertIn("could not load it again", report)
        self.assertIn("was not found", report)          # whisper's own reason
        self.assertIn("was not pasted", report)         # what it cost him
        self.assertIn("fetch-models.ps1", report)       # what to do
        self.assertIn("idle_release_minutes = 0", report)  # how to opt out
        self.assertNotIn("Traceback", report)

    def test_a_warm_up_that_fails_at_the_press_still_reports_when_he_lets_go(self):
        model = self.build()
        model.start()
        self.release(model)
        self.batch.start_error = self.gone()

        model.note_press()
        self.assert_failed(model)
        self.assertIs(model.state, Residency.RELEASED)

        # He does not find out from a silent nothing: the utterance says so.
        with self.assertRaises(BackendUnavailableError):
            model.transcribe(PCM, SR)

    def test_a_later_press_tries_again_rather_than_staying_broken(self):
        model = self.build()
        model.start()
        self.release(model)
        self.batch.start_error = self.gone()
        model.note_press()
        self.assert_failed(model)

        self.batch.start_error = None      # he puts the model file back
        model.note_press()
        self.assert_becomes(model, Residency.RESIDENT)
        self.assertEqual(model.transcribe(PCM, SR), "Hello world.")
        self.assertIsNone(model.last_failure)

    def test_it_is_not_reported_as_healthy_after_a_failed_load(self):
        model = self.build()
        model.start()
        self.release(model)
        self.assertTrue(model.is_healthy())     # released, but it will come back
        self.batch.start_error = self.gone()
        model.note_press()
        self.assert_failed(model)
        self.assertFalse(model.is_healthy())


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


class Shutdown(ResidencyTestCase):
    def test_stopping_while_the_model_is_loading_leaves_nothing_running(self):
        model = self.build()
        model.start()
        self.release(model)

        gate = threading.Event()
        self.batch.start_gate = gate
        self.batch.start_entered.clear()
        model.note_press()
        self.assertTrue(self.batch.start_entered.wait(timeout=5))

        model.stop()
        self.assertIs(model.state, Residency.RELEASED)
        self.assertFalse(self.batch.running)

    def test_stopping_while_the_model_is_being_released_leaves_one_owner(self):
        """Ctrl+C landing in the middle of an idle release. Both the release
        worker and the shutdown are inside stop(); neither may hang, and the
        supervisor must not be left half-owned."""
        model = self.build()
        model.start()
        gate = threading.Event()
        self.batch.stop_gate = gate
        self.clock.advance(FIVE_MINUTES + 1)
        self.assertTrue(model.check_idle())
        self.assertIs(model.state, Residency.RELEASING)

        stopper = threading.Thread(target=model.stop)
        stopper.start()
        gate.set()
        stopper.join(timeout=20)
        self.assertFalse(stopper.is_alive(), "shutdown hung behind the release")
        self.assertIs(model.state, Residency.RELEASED)
        self.assertFalse(self.batch.running)

    def test_stop_is_safe_before_start_and_twice_over(self):
        model = self.build()
        model.stop()
        model.start()
        model.stop()
        model.stop()
        self.assertFalse(self.batch.running)

    def test_transcribing_during_shutdown_refuses_rather_than_hanging(self):
        model = self.build()
        model.start()
        model.stop()
        with self.assertRaises(BackendUnavailableError) as ctx:
            model.transcribe(PCM, SR)
        self.assertIn("shutting down", ctx.exception.message)

    def test_empty_audio_does_not_wake_a_released_model(self):
        model = self.build()
        model.start()
        self.release(model)
        self.assertEqual(model.transcribe(b"", SR), "")
        self.assertIs(model.state, Residency.RELEASED)
        self.assertEqual(self.batch.starts, 1)


class WhatHeSees(ResidencyTestCase):
    def test_describe_says_what_it_will_do_and_when(self):
        model = self.build()
        self.assertIn("fake batch transcriber", model.describe)
        self.assertIn("5 minutes", model.describe)

    def test_describe_of_a_one_minute_setting_is_not_written_as_1_minutes(self):
        model = self.build(idle_release_s=60.0)
        self.assertIn("1 minute", model.describe)
        self.assertNotIn("1 minutes", model.describe)

    def test_the_state_is_readable_at_any_moment(self):
        model = self.build()
        self.assertIs(model.state, Residency.RELEASED)
        model.start()
        self.assertIs(model.state, Residency.RESIDENT)
        self.release(model)
        self.assertIs(model.state, Residency.RELEASED)


# ---------------------------------------------------------------------------
# Through the real pipeline
# ---------------------------------------------------------------------------


class ThroughTheWholePipeline(ResidencyTestCase):
    def pipeline(self, model: ResidentModel) -> Pipeline:
        self.injector = FakeInjector()
        self.overlay = FakeOverlay()
        self.windows = FakeWindows()
        self.notices: list[tuple[str, str]] = []
        return Pipeline(
            batch=model,
            cleaner=lambda text: CleanResult(text=text.strip(), original=text),
            injector=self.injector,
            windows=self.windows,
            overlay=self.overlay,
            streaming=None,
            submit=InlineSubmit(),
            notify=lambda level, msg: self.notices.append((level, msg)),
            sample_rate=SR,
        )

    def test_the_first_utterance_after_an_idle_spell_is_still_pasted(self):
        model = self.build()
        model.start()
        pipeline = self.pipeline(model)
        self.release(model)

        model.note_press()          # what app.py does on key DOWN
        pipeline.start_utterance()
        pipeline.push_audio(PCM)
        pipeline.finish_utterance()

        self.assertEqual(self.injector.sent, [("Hello world.", self.windows.window)])
        self.assertIs(model.state, Residency.RESIDENT)
        self.assertEqual(self.batch.starts, 2)
        # Routine residency is not worth interrupting him for.
        self.assertEqual(self.notices, [])

    def test_a_model_that_cannot_come_back_is_told_to_him_not_swallowed(self):
        model = self.build()
        model.start()
        pipeline = self.pipeline(model)
        self.release(model)
        self.batch.start_error = BackendUnavailableError(
            "whisper-server exited immediately (exit code 7).", "Run setup again.")

        model.note_press()
        pipeline.start_utterance()
        pipeline.push_audio(PCM)
        pipeline.finish_utterance()

        self.assertEqual(self.injector.sent, [])
        self.assertEqual(self.overlay.states[-1], OverlayState.ERROR)
        errors = [msg for level, msg in self.notices if level == "error"]
        self.assertTrue(errors, "nothing was pasted and nothing was said")
        self.assertIn("could not load it again", errors[0])
        self.assertIn("Run setup again.", errors[0])


if __name__ == "__main__":
    unittest.main()
