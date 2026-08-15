"""The no-focus-stealing route, including its permanent fallback."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dictate import deferred as deferred_mod
from dictate.deferred import DeferredDelivery, Request
from dictate.errors import InjectionError, TargetNotForegroundError
from dictate.history import HistoryStore
from dictate.platform.base import OverlayState, TargetWindow

from .fakes import FakeInjector, FakeOverlay, FakeWindows, InlineSubmit

TERMINAL = TargetWindow(
    11, "bchue@homeserver: ~/vector_db", "terminal.exe", process_id=80)
BROWSER = TargetWindow(22, "Firefox", "firefox.exe", process_id=81)


class DeferredTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.windows = FakeWindows(TERMINAL)
        self.injector = FakeInjector()
        self.overlay = FakeOverlay()
        self.history = HistoryStore(Path(self.tmp.name) / "history.txt")
        self.notices: list[tuple[str, str]] = []
        self.service = DeferredDelivery(
            windows=self.windows,
            injector=self.injector,
            overlay=self.overlay,
            history=self.history,
            submit=InlineSubmit(),
            notify=lambda level, message: self.notices.append((level, message)),
        )

    def request(self, generation: int, **kwargs) -> Request:
        values = dict(
            generation=generation,
            text="Hello from the terminal.",
            raw="Hello from the terminal.",
            spoke_s=3.0,
            target=TERMINAL,
            focused=BROWSER,
            on_clipboard=True,
        )
        values.update(kwargs)
        return Request(**values)

    def begin_and_wait(self) -> int:
        generation = self.service.begin_utterance()
        self.windows.window = BROWSER
        self.service.defer(self.request(generation))
        return generation


class ItWaitsForTheCapturedWindow(DeferredTestCase):
    def test_nothing_is_sent_while_another_window_is_in_front(self):
        self.begin_and_wait()
        self.service.poll()
        self.assertEqual(self.injector.sent, [])
        self.assertTrue(self.service.waiting)
        self.assertIs(self.overlay.states[-1], OverlayState.WAITING)
        self.assertIn("Waiting to paste", self.overlay.showing)

    def test_a_recycled_handle_is_not_treated_as_the_captured_window(self):
        self.begin_and_wait()
        self.windows.window = TargetWindow(
            TERMINAL.handle, "A different window", "other.exe", process_id=90)
        self.service.poll()
        self.assertEqual(self.injector.sent, [])
        self.assertFalse(self.service.waiting)
        self.assertIn("closed", "\n".join(m for _, m in self.notices))

    def test_returning_to_the_target_delivers_without_focusing_anything(self):
        self.begin_and_wait()
        self.windows.window = TERMINAL
        with mock.patch.object(deferred_mod.log, "info") as logged:
            self.service.poll()
        self.assertEqual(self.injector.sent,
                         [("Hello from the terminal.", TERMINAL)])
        self.assertEqual(self.injector.required_foreground, [True])
        self.assertEqual(self.windows.focused, [])
        self.assertFalse(self.service.waiting)
        self.assertIs(self.overlay.states[-1], OverlayState.DONE)
        self.assertTrue(any("succeeded" in str(call) for call in logged.call_args_list))

    def test_the_history_changes_from_held_to_delivered_after_success(self):
        self.begin_and_wait()
        self.assertIn("did not paste", self.history.path.read_text(encoding="utf-8"))
        self.windows.window = TERMINAL
        self.service.poll()
        saved = self.history.path.read_text(encoding="utf-8")
        self.assertNotIn("did not paste", saved)
        self.assertIn("Hello from the terminal.", saved)


class TheFallbackNeverLosesText(DeferredTestCase):
    def test_a_closed_target_ends_the_wait_and_keeps_the_history_copy(self):
        self.begin_and_wait()
        self.windows.closed.add(TERMINAL.handle)
        self.service.poll()
        self.assertFalse(self.service.waiting)
        self.assertEqual(self.injector.sent, [])
        self.assertIn("closed", "\n".join(m for _, m in self.notices))
        self.assertIn("did not paste", self.history.path.read_text(encoding="utf-8"))

    def test_a_real_injection_refusal_ends_the_wait_and_names_the_case(self):
        self.injector.error = InjectionError(
            "Windows accepted only 0 of 40 keystrokes", "run as administrator")
        self.begin_and_wait()
        self.windows.window = TERMINAL
        with mock.patch.object(deferred_mod.log, "warning") as logged:
            self.service.poll()
        self.assertFalse(self.service.waiting)
        said = "\n".join(m for _, m in self.notices)
        self.assertIn("Windows accepted only 0", said)
        self.assertIn("administrator", said)
        self.assertIn("Ctrl+V", said)
        self.assertTrue(any("failed" in str(call) for call in logged.call_args_list))

    def test_a_foreground_race_retries_instead_of_claiming_failure(self):
        class MovedOnce(FakeInjector):
            def __init__(self):
                super().__init__()
                self.moved = False

            def send(self, text, target, *, require_target_foreground=False,
                     still_allowed=None):
                if not self.moved:
                    self.moved = True
                    raise TargetNotForegroundError("moved again")
                return super().send(
                    text, target,
                    require_target_foreground=require_target_foreground,
                    still_allowed=still_allowed)

        self.injector = MovedOnce()
        self.service.injector = self.injector
        self.begin_and_wait()
        self.windows.window = TERMINAL
        self.service.poll()
        self.assertTrue(self.service.waiting)
        self.assertEqual(self.injector.sent, [])
        self.service.poll()
        self.assertFalse(self.service.waiting)
        self.assertEqual(len(self.injector.sent), 1)

    def test_a_foreground_change_after_a_partial_batch_does_not_retry(self):
        error = TargetNotForegroundError("moved during a long paste")
        error.partial = True
        self.injector.error = error
        self.begin_and_wait()
        self.windows.window = TERMINAL
        self.service.poll()
        self.assertFalse(self.service.waiting)
        said = "\n".join(m for _, m in self.notices)
        self.assertIn("already have been typed", said)

    def test_starting_another_dictation_retires_the_automatic_wait(self):
        self.begin_and_wait()
        self.service.begin_utterance()
        self.windows.window = TERMINAL
        self.service.poll()
        self.assertFalse(self.service.waiting)
        self.assertEqual(self.injector.sent, [])
        said = "\n".join(m for _, m in self.notices)
        self.assertIn("another dictation started", said)
        self.assertIn("Ctrl+V", said)

    def test_a_new_dictation_during_the_final_guard_prevents_the_send(self):
        service = self.service

        class NewPressAtTheGuard(FakeInjector):
            def send(self, text, target, *, require_target_foreground=False,
                     still_allowed=None):
                service.begin_utterance()
                return super().send(
                    text, target,
                    require_target_foreground=require_target_foreground,
                    still_allowed=still_allowed)

        self.injector = NewPressAtTheGuard()
        self.service.injector = self.injector
        self.begin_and_wait()
        self.windows.window = TERMINAL
        self.service.poll()
        self.assertEqual(self.injector.sent, [])
        self.assertFalse(self.service.waiting)

    def test_an_older_transcription_cannot_join_after_a_new_press(self):
        old = self.service.begin_utterance()
        self.service.begin_utterance()
        self.service.defer(self.request(old))
        self.windows.window = TERMINAL
        self.service.poll()
        self.assertFalse(self.service.waiting)
        self.assertEqual(self.injector.sent, [])
        self.assertIn("before these words were ready",
                      "\n".join(m for _, m in self.notices))


if __name__ == "__main__":
    unittest.main()
