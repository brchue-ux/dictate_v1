"""What the notification-area icon says, offers and looks like.

The Win32 half of the tray cannot be run here. This is the other half - the
whole of the decision-making - and it is where the requirement actually lives:
that a copy of dictate running with no window is visible, says what it is doing,
and offers the way out without anyone having to remember a command.
"""

from __future__ import annotations

import struct
import unittest
from pathlib import Path

from dictate import tray


def state(**kwargs) -> tray.TrayState:
    return tray.TrayState(**kwargs)


class WhatItSays(unittest.TestCase):
    def test_the_tooltip_answers_is_it_running_and_how_do_i_talk_to_it(self):
        text = tray.tooltip(state(status=tray.TrayStatus.READY,
                                  hotkey="Ctrl + Alt + Space", model_resident=True))
        self.assertIn("dictate", text)
        self.assertIn("ready", text)
        self.assertIn("Ctrl + Alt + Space", text)

    def test_it_says_when_it_is_listening(self):
        self.assertIn("listening",
                      tray.tooltip(state(status=tray.TrayStatus.LISTENING)))

    def test_an_error_is_carried_where_he_will_see_it(self):
        text = tray.tooltip(state(status=tray.TrayStatus.ERROR,
                                  detail="whisper-server stopped responding"))
        self.assertIn("stopped responding", text)

    def test_it_is_never_longer_than_windows_will_show(self):
        # Windows shows NOTHING when a tooltip is over 127 characters, so a
        # long error message would silently empty the one visible surface.
        text = tray.tooltip(state(status=tray.TrayStatus.ERROR, detail="x" * 400))
        self.assertLessEqual(len(text), tray.TOOLTIP_MAX)

    def test_the_status_line_says_whether_the_card_is_holding_the_model(self):
        """The question `[whisper] idle_release_minutes` exists to answer, in
        the one place he can see without typing anything."""
        loaded = tray.status_line(state(status=tray.TrayStatus.READY,
                                        model_resident=True))
        released = tray.status_line(state(status=tray.TrayStatus.RESTING))
        self.assertIn("model loaded", loaded)
        self.assertIn("unloaded", released)
        self.assertIn("hotkey", released)


class Started:
    """A started `dictate update`, standing in for the process object the app
    keeps so it can ask whether that update is still going."""

    def __init__(self, pid: int, code: int | None = None) -> None:
        self.pid = pid
        self.code = code

    def poll(self) -> int | None:
        return self.code


def actions(done: list[str]) -> tray.TrayActions:
    return tray.TrayActions(
        stop=lambda: done.append("stop"),
        restart=lambda: done.append("restart"),
        open_log=lambda: done.append("log"),
        check_updates=lambda: done.append("check"),
        update_now=lambda: done.append("update"),
    )


class WhatItOffers(unittest.TestCase):
    def test_the_menu_is_status_stop_restart_the_updates_and_the_log(self):
        keys = [item.key for item in tray.menu(state())]
        self.assertEqual(keys, [tray.STATUS, tray.STOP, tray.RESTART,
                                tray.CHECK, tray.UPDATE, tray.LOG])

    def test_the_status_line_is_not_clickable(self):
        items = {item.key: item for item in tray.menu(state())}
        self.assertFalse(items[tray.STATUS].enabled)
        self.assertTrue(items[tray.STOP].enabled)

    def test_every_item_teaches_the_command_that_does_the_same_thing(self):
        """The tray is not a second way of doing things - it is the visible
        version of the commands, so it names them."""
        items = {item.key: item for item in tray.menu(state())}
        self.assertEqual(items[tray.STOP].command, "dictate stop")
        self.assertIn("dictate stop", items[tray.RESTART].command)
        self.assertIn("dictate stop", items[tray.STOP].text)
        self.assertEqual(items[tray.CHECK].command, "dictate update --check")
        self.assertEqual(items[tray.UPDATE].command, "dictate update")

    def test_the_two_update_items_say_which_one_changes_something(self):
        """He is choosing between a report and a replacement. The labels are
        the only thing standing between those two, so they are checked."""
        items = {item.key: item for item in tray.menu(state())}
        self.assertEqual(items[tray.CHECK].label, "Check for updates")
        self.assertEqual(items[tray.UPDATE].label, "Update now")
        self.assertIn("--check", items[tray.CHECK].text)

    def test_a_second_update_cannot_be_started_while_one_is_running(self):
        """Two updates over the same folder is the one way this could hurt: the
        second would race the first over the files the first is replacing."""
        items = {item.key: item for item in tray.menu(state(updating=True))}
        self.assertFalse(items[tray.UPDATE].enabled)
        # A check changes nothing at all, so it is still offered - and so is
        # everything about stopping the copy that is being replaced.
        self.assertTrue(items[tray.CHECK].enabled)
        self.assertTrue(items[tray.STOP].enabled)

    def test_stop_is_what_a_click_does(self):
        default = [item for item in tray.menu(state()) if item.default]
        self.assertEqual([item.key for item in default], [tray.STOP])

    def test_the_actions_are_wired_to_the_keys(self):
        done: list[str] = []
        for item in tray.menu(state()):
            actions(done).invoke(item.key)
        self.assertEqual(done, ["stop", "restart", "check", "update", "log"])

    def test_an_unknown_id_does_nothing_rather_than_raising(self):
        done: list[str] = []
        self.assertFalse(actions(done).invoke("nonsense"))
        self.assertFalse(actions(done).invoke(tray.STATUS))
        self.assertEqual(done, [])


class WhatItLooksLike(unittest.TestCase):
    """The icon is drawn here rather than shipped as a file, because its colour
    is the status. So the bytes are checked here - Windows will not tell us."""

    def test_it_is_an_icon_windows_can_read(self):
        data = tray.ico_bytes("#c8a45c")
        reserved, kind, count = struct.unpack("<HHH", data[:6])
        self.assertEqual((reserved, kind, count), (0, 1, 1))
        width, height, colours, _pad, planes, bits, size, offset = struct.unpack(
            "<BBBBHHII", data[6:22])
        self.assertEqual((width, height), (16, 16))
        self.assertEqual((planes, bits, colours), (1, 32, 0))
        self.assertEqual(offset, 22)
        self.assertEqual(len(data), offset + size)

        # The bitmap header inside it: height is doubled, because the format
        # counts the colour rows and the mask rows together.
        header = struct.unpack("<IiiHHIIiiII", data[22:62])
        self.assertEqual(header[0], 40)
        self.assertEqual((header[1], header[2]), (16, 32))

    def test_the_colour_is_the_status(self):
        for status, colour in tray.STATE_COLOURS.items():
            with self.subTest(status=status):
                self.assertEqual(state(status=status).colour, colour)
        self.assertNotEqual(tray.STATE_COLOURS[tray.TrayStatus.LISTENING],
                            tray.STATE_COLOURS[tray.TrayStatus.RESTING])
        self.assertNotEqual(tray.STATE_COLOURS[tray.TrayStatus.ERROR],
                            tray.STATE_COLOURS[tray.TrayStatus.READY])

    def test_the_pixels_are_that_colour(self):
        data = tray.ico_bytes("#c8a45c")
        pixels = data[62:]
        # Middle of the middle row, in the bottom-up BGRA the format wants.
        row, column = 8, 8
        at = ((16 - 1 - row) * 16 + column) * 4
        blue, green, red, alpha = pixels[at:at + 4]
        self.assertEqual((red, green, blue), (0xC8, 0xA4, 0x5C))
        self.assertEqual(alpha, 255)

    def test_the_corners_are_transparent_so_it_reads_as_a_dot(self):
        pixels = tray.ico_bytes("#c8a45c")[62:]
        self.assertEqual(pixels[3], 0)          # bottom-left corner's alpha

    def test_a_colour_it_cannot_read_is_refused_rather_than_drawn_wrong(self):
        with self.assertRaises(ValueError):
            tray.ico_bytes("nonsense")


class WhatTheAppTellsIt(unittest.TestCase):
    """The wiring between the running app and the one thing on screen.

    `Application` cannot be built off Windows - the platform factory refuses,
    deliberately - so the parts that decide what the icon shows are driven on
    their own, with the four things they read supplied.
    """

    def setUp(self):
        # The app publishes what it is doing into the state directory, so that
        # `dictate update` can decline to stop it mid-sentence. Point that
        # somewhere disposable rather than at this machine's real one.
        import os
        import tempfile

        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.state = Path(self._tmp.name)
        self._previous = os.environ.get("DICTATE_STATE_DIR")
        os.environ["DICTATE_STATE_DIR"] = str(self.state)
        self.addCleanup(self._restore)

    def _restore(self):
        import os

        if self._previous is None:
            os.environ.pop("DICTATE_STATE_DIR", None)
        else:
            os.environ["DICTATE_STATE_DIR"] = self._previous
        self._tmp.cleanup()

    def app(self, **kwargs):
        import threading
        from types import SimpleNamespace

        from dictate import config as config_mod
        from dictate.app import Application
        from dictate.engines.residency import Residency

        from . import fakes

        app = object.__new__(Application)
        app.cfg = config_mod.load(None)
        app.console = lambda _msg="": None
        app.overlay = fakes.FakeOverlay()
        app.tray = None
        app.restart_wanted = False
        app._stopping = threading.Event()
        app._holding_hotkey = False
        app._in_flight = 0
        app._last_error = ""
        app._published_activity = None
        app._update = None
        app.batch = SimpleNamespace(state=kwargs.pop("residency", Residency.RESIDENT))
        for key, value in kwargs.items():
            setattr(app, key, value)
        return app

    def test_it_says_ready_with_the_hotkey_when_the_model_is_loaded(self):
        state = self.app()._tray_state()
        self.assertIs(state.status, tray.TrayStatus.READY)
        self.assertTrue(state.model_resident)
        self.assertIn("Ctrl", state.hotkey)

    def test_it_says_the_card_has_its_memory_back(self):
        from dictate.engines.residency import Residency

        state = self.app(residency=Residency.RELEASED)._tray_state()
        self.assertIs(state.status, tray.TrayStatus.RESTING)
        self.assertFalse(state.model_resident)

    def test_it_follows_the_hotkey_and_the_transcription(self):
        self.assertIs(self.app(_holding_hotkey=True)._tray_state().status,
                      tray.TrayStatus.LISTENING)
        self.assertIs(self.app(_in_flight=1)._tray_state().status,
                      tray.TrayStatus.WORKING)

    def test_an_error_reaches_it_and_a_new_dictation_clears_it(self):
        app = self.app()
        app.notify("error", "whisper-server stopped responding\nsecond line")
        state = app._tray_state()
        self.assertIs(state.status, tray.TrayStatus.ERROR)
        self.assertEqual(state.detail, "whisper-server stopped responding")

        app._last_error = ""            # what the next hotkey press does
        self.assertIs(app._tray_state().status, tray.TrayStatus.READY)

    def test_stop_from_the_tray_goes_through_the_one_shutdown_path(self):
        app = self.app()
        app.request_stop_from_tray()
        # Closing the overlay is what ends the Tk loop, which is what runs the
        # clean shutdown - the same door Ctrl+C and `dictate stop` use.
        self.assertTrue(app.overlay.closed.is_set())
        self.assertFalse(app.restart_wanted)

    def test_it_publishes_whether_now_is_a_moment_to_be_stopped_in(self):
        """The seam `dictate update` reads before it stops anything.

        The same answer the icon is already showing, put where another process
        can see it - so an update never takes the app away mid-sentence.
        """
        from dictate import instance

        app = self.app(_holding_hotkey=True)
        app._refresh_tray()
        activity = instance.read_activity()
        self.assertIsNotNone(activity)
        self.assertTrue(activity.busy)
        self.assertIn("speaking", activity.what)

        app._holding_hotkey = False
        app._in_flight = 1
        app._refresh_tray()
        self.assertIn("transcribing", instance.read_activity().what)

        app._in_flight = 0
        app._refresh_tray()
        self.assertFalse(instance.read_activity().busy)

    def test_an_idle_app_does_not_write_the_same_answer_over_and_over(self):
        """It is written on change, not on a timer: the tray is refreshed twice
        a second for as long as dictate runs, and that may not become two disk
        writes a second for as long as dictate runs."""
        from dictate import instance

        app = self.app()
        app._refresh_tray()
        path = instance.activity_path()
        first = path.stat().st_mtime_ns
        path.write_text("busy=1\nwhat=tampered\n", encoding="utf-8")
        for _ in range(20):
            app._refresh_tray()
        self.assertEqual(path.read_text(encoding="utf-8"),
                         "busy=1\nwhat=tampered\n")
        self.assertIsNotNone(first)

    def test_the_update_runs_in_another_process_and_not_in_this_one(self):
        """The reason both update items exist as they do.

        This process is the one an update replaces and restarts. Doing the work
        in here would mean a thread writing files while the process it is in is
        being asked to stop - so what the menu item does is start `dictate
        update` elsewhere and let it stop this copy the ordinary way.
        """
        from unittest import mock

        app = self.app()
        with mock.patch("dictate.update.start_in_console",
                        return_value=Started(4321)) as start:
            app.update_now()
        self.assertFalse(start.call_args.kwargs["check_only"])
        # Nothing about this copy was touched from in here: no shutdown was
        # asked for, and the update is what will ask for one.
        self.assertFalse(app.overlay.closed.is_set())
        self.assertFalse(app.restart_wanted)

    def test_check_for_updates_starts_the_command_that_changes_nothing(self):
        from unittest import mock

        app = self.app()
        with mock.patch("dictate.update.start_in_console",
                        return_value=Started(99)) as start:
            app.check_for_updates()
        self.assertTrue(start.call_args.kwargs["check_only"])
        # A check may be run as often as he likes, because it cannot leave
        # anything different behind.
        self.assertFalse(app.update_in_flight())
        self.assertFalse(app.overlay.closed.is_set())

    def test_a_second_update_is_refused_even_if_the_menu_is_clicked_twice(self):
        from unittest import mock

        app = self.app()
        with mock.patch("dictate.update.start_in_console",
                        return_value=Started(4321)) as start:
            app.update_now()
            self.assertTrue(app.update_in_flight())
            self.assertTrue(app._tray_state().updating)
            app.update_now()
        self.assertEqual(start.call_count, 1)

    def test_an_update_that_ended_without_stopping_us_can_be_tried_again(self):
        """The case that would otherwise grey the item out for good: `dictate
        update` refusing before it changed anything - not signed in to GitHub,
        say - leaves this copy running exactly as it was."""
        from unittest import mock

        app = self.app()
        finished = Started(4321)
        with mock.patch("dictate.update.start_in_console",
                        return_value=finished) as start:
            app.update_now()
            finished.code = 2                # its window said why, and closed
            self.assertFalse(app.update_in_flight())
            self.assertFalse(app._tray_state().updating)
            app.update_now()
        self.assertEqual(start.call_count, 2)

    def test_an_update_that_will_not_say_is_treated_as_still_running(self):
        from unittest import mock

        class Silent:
            pid = 1

            def poll(self):
                raise OSError("no idea")

        app = self.app()
        with mock.patch("dictate.update.start_in_console", return_value=Silent()):
            app.update_now()
        self.assertTrue(app.update_in_flight())

    def test_an_update_that_cannot_be_started_turns_the_icon_red(self):
        """There is no console to say it in and no dialog worth showing - a
        modal box would hold the thread that owns the icon until somebody
        clicked it. The icon itself is the surface this copy has."""
        from unittest import mock

        from dictate.errors import DictateError

        app = self.app()
        with mock.patch("dictate.update.start_in_console",
                        side_effect=DictateError("no window to update in", "type it")):
            app.update_now()          # must not raise: the tray thread is here
        state = app._tray_state()
        self.assertIs(state.status, tray.TrayStatus.ERROR)
        self.assertIn("no window to update in", state.detail)
        self.assertFalse(app.update_in_flight())

    def test_restart_from_the_tray_shuts_down_first_and_says_so(self):
        from dictate.app import EXIT_RESTART

        app = self.app()
        app.request_restart()
        self.assertTrue(app.overlay.closed.is_set())
        self.assertTrue(app.restart_wanted)
        # The new copy is started by whoever holds the instance lock, once it
        # has been released - never from in here.
        self.assertEqual(EXIT_RESTART, 7)


if __name__ == "__main__":
    unittest.main()
