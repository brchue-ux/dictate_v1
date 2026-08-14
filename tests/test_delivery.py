"""Where the text is allowed to go when he has moved on since he started talking.

The whole decision, off Windows: it is made from two window handles and two
settings, which is the point of it living in `delivery.py` rather than inside the
injector. What cannot be tested here is the two Win32 reads it is made from -
`GetForegroundWindow` and `IsWindow` - and the paste itself.
"""

from __future__ import annotations

import unittest

from dictate import delivery
from dictate.platform.base import TargetWindow

TERMINAL = TargetWindow(handle=11, title="Windows PowerShell", process="pwsh.exe")
BROWSER = TargetWindow(handle=22, title="Firefox", process="firefox.exe")


class HeIsStillWhereHeStarted(unittest.TestCase):
    """The ordinary dictation, and it must not have changed at all."""

    def test_the_same_window_pastes(self):
        d = delivery.decide(TERMINAL, TERMINAL)
        self.assertEqual(d.action, delivery.DELIVER)

    def test_it_is_the_handle_that_is_compared_not_the_title(self):
        """A terminal's title changes with the directory, and a browser's with
        the tab. Comparing anything but the handle would hold back a paste
        because he had changed directory while speaking."""
        renamed = TargetWindow(handle=TERMINAL.handle, title="pwsh - src",
                               process="pwsh.exe")
        self.assertEqual(delivery.decide(TERMINAL, renamed).action, delivery.DELIVER)

    def test_a_window_that_lost_focus_and_got_it_back_is_not_a_change(self):
        """The case the press-time capture already covered: a notification takes
        focus for a moment and gives it back. Nothing here ever sees it."""
        self.assertEqual(delivery.decide(TERMINAL, TERMINAL, target_exists=True).action,
                         delivery.DELIVER)


class AnUnknownIsNeverTreatedAsAChange(unittest.TestCase):
    """"Nothing may report a healthy system as broken" applies to the one thing
    the product is for. If a query comes back empty, dictate pastes."""

    def test_no_window_was_captured_at_press(self):
        d = delivery.decide(None, BROWSER)
        self.assertEqual(d.action, delivery.DELIVER)

    def test_the_window_in_front_could_not_be_read(self):
        d = delivery.decide(TERMINAL, None)
        self.assertEqual(d.action, delivery.DELIVER)

    def test_neither_could_be_read(self):
        self.assertEqual(delivery.decide(None, None).action, delivery.DELIVER)


class HeMovedWhileHeWasSpeaking(unittest.TestCase):
    def test_the_default_holds_rather_than_pasting_anywhere(self):
        d = delivery.decide(TERMINAL, BROWSER)
        self.assertEqual(d.action, delivery.HOLD)
        self.assertEqual(d.reason, delivery.MOVED)
        self.assertFalse(d.pastes)

    def test_restore_mode_brings_the_window_he_started_in_back(self):
        d = delivery.decide(TERMINAL, BROWSER, mode=delivery.RESTORE_MODE)
        self.assertEqual(d.action, delivery.RESTORE)
        self.assertTrue(d.pastes)

    def test_there_is_no_mode_that_pastes_into_the_window_he_moved_to(self):
        """The one behaviour that is not on offer, at any setting. Text arriving
        in an application that never asked for it is the same family of harm as
        the stray Return that ran a command in his terminal."""
        for mode in delivery.MODES:
            for restore in (True, False):
                with self.subTest(mode=mode, restore_focus=restore):
                    d = delivery.decide(TERMINAL, BROWSER, mode=mode,
                                        restore_focus=restore)
                    self.assertIn(d.action, (delivery.HOLD, delivery.RESTORE))
                    # RESTORE pastes into the CAPTURED window, never `focused`.
                    self.assertNotIn(str(BROWSER.handle), d.action)

    def test_restore_without_permission_to_raise_a_window_holds(self):
        """`config.validate` refuses that pair, so this is the belt: restoring
        means raising, and doing the other half instead is not an option."""
        d = delivery.decide(TERMINAL, BROWSER, mode=delivery.RESTORE_MODE,
                            restore_focus=False)
        self.assertEqual(d.action, delivery.HOLD)


class TheWindowClosedWhileHeWasSpeaking(unittest.TestCase):
    def test_it_holds_and_says_which_of_the_two_happened(self):
        d = delivery.decide(TERMINAL, BROWSER, target_exists=False)
        self.assertEqual(d.action, delivery.HOLD)
        self.assertEqual(d.reason, delivery.CLOSED)

    def test_restore_mode_cannot_restore_a_window_that_is_gone(self):
        d = delivery.decide(TERMINAL, BROWSER, target_exists=False,
                            mode=delivery.RESTORE_MODE)
        self.assertEqual(d.action, delivery.HOLD)
        self.assertEqual(d.reason, delivery.CLOSED)

    def test_an_unanswerable_is_window_reads_as_moved(self):
        """`None` is "nobody asked, or Windows would not say". It costs the
        accuracy of one sentence and never an action - both hold."""
        d = delivery.decide(TERMINAL, BROWSER, target_exists=None)
        self.assertEqual(d.action, delivery.HOLD)
        self.assertEqual(d.reason, delivery.MOVED)


class WhatHeIsTold(unittest.TestCase):
    """The message is the whole of the difference between refusing and losing,
    so it is tested like any other output."""

    def test_the_first_line_says_where_the_words_are(self):
        """It is the only line the caption panel and the tray tooltip get."""
        first = delivery.held_message(delivery.MOVED, target=TERMINAL,
                                      focused=BROWSER, on_clipboard=True,
                                      in_history=True).splitlines()[0]
        self.assertIn("Not pasted", first)
        self.assertIn("Ctrl+V", first)

    def test_it_names_both_windows(self):
        message = delivery.held_message(delivery.MOVED, target=TERMINAL,
                                        focused=BROWSER, on_clipboard=True)
        self.assertIn("Windows PowerShell", message)
        self.assertIn("Firefox", message)

    def test_it_says_how_to_get_the_old_behaviour_back(self):
        message = delivery.held_message(delivery.MOVED, target=TERMINAL,
                                        focused=BROWSER, on_clipboard=True)
        self.assertIn("on_focus_change", message)
        self.assertIn("restore", message)

    def test_a_closed_window_is_not_blamed_on_him(self):
        message = delivery.held_message(delivery.CLOSED, target=TERMINAL,
                                        on_clipboard=True, in_history=True)
        self.assertIn("closed", message)
        self.assertNotIn("you moved", message)

    def test_the_history_is_offered_when_the_clipboard_was_not_written(self):
        message = delivery.held_message(delivery.MOVED, target=TERMINAL,
                                        focused=BROWSER, on_clipboard=False,
                                        in_history=True)
        self.assertNotIn("Ctrl+V", message)
        self.assertIn("dictation history", message)

    def test_with_neither_copy_it_says_so_rather_than_implying_one(self):
        """The worst case, and the one where a hopeful sentence would be a lie:
        the clipboard refused and the history is off or unwritable."""
        message = delivery.held_message(delivery.MOVED, target=TERMINAL,
                                        focused=BROWSER, on_clipboard=False,
                                        in_history=False)
        self.assertNotIn("Ctrl+V", message)
        self.assertIn("could not keep a copy", message)

    def test_a_half_finished_paste_says_to_look_before_pasting_again(self):
        """The one way this change could paste a dictation twice: some of it
        went in before Windows refused the rest, and the whole of it is now on
        the clipboard."""
        message = delivery.held_message(delivery.REFUSED, target=TERMINAL,
                                        on_clipboard=True, partial=True,
                                        detail="Windows accepted only 12 of 400")
        self.assertIn("already have been typed", message)

    def test_a_paste_that_delivered_nothing_does_not_mention_looking(self):
        message = delivery.held_message(delivery.REFUSED, target=TERMINAL,
                                        on_clipboard=True, partial=False,
                                        detail="Windows accepted only 0 of 400")
        self.assertNotIn("already have been typed", message)
        self.assertIn("Windows accepted only 0 of 400", message)

    def test_every_message_ends_up_somewhere_readable(self):
        """No case may produce an empty first line: it is what the panel shows."""
        for reason in (delivery.MOVED, delivery.CLOSED, delivery.REFUSED):
            for clip in (True, False):
                with self.subTest(reason=reason, on_clipboard=clip):
                    message = delivery.held_message(reason, target=TERMINAL,
                                                    focused=BROWSER,
                                                    on_clipboard=clip,
                                                    in_history=not clip)
                    self.assertTrue(message.splitlines()[0].strip())


if __name__ == "__main__":
    unittest.main()
