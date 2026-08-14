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

from dictate import overlay_size, tray


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
        open_history=lambda: done.append("history"),
        delete_history=lambda: done.append("history-delete"),
        set_hotkey=lambda combination: done.append(f"hotkey:{combination}"),
        set_caption_size=lambda name: done.append(f"size:{name}"),
        toggle_autostart=lambda: done.append("autostart"),
    )


def hotkey_submenu(**kwargs) -> list[tray.MenuItem]:
    items = {item.key: item for item in tray.menu(state(**kwargs))}
    return list(items[tray.HOTKEY].children)


def size_submenu(**kwargs) -> list[tray.MenuItem]:
    items = {item.key: item for item in tray.menu(state(**kwargs))}
    return list(items[tray.SIZE].children)


class WhatItOffers(unittest.TestCase):
    def test_the_menu_is_status_size_stop_restart_updates_the_settings_and_the_log(self):
        keys = [item.key for item in tray.menu(state())]
        self.assertEqual(keys, [tray.STATUS, tray.SIZE, tray.STOP, tray.RESTART,
                                tray.CHECK, tray.UPDATE, tray.HOTKEY,
                                tray.AUTOSTART, tray.LOG])

    def test_the_captions_can_be_resized_from_the_only_surface_there_is(self):
        """A panel that is a bit too big must not need a terminal: this is the
        one visible thing a copy started at logon has."""
        items = {item.key: item for item in tray.menu(state())}
        self.assertEqual(items[tray.SIZE].command, "dictate look")
        self.assertFalse(items[tray.SIZE].default)
        offered = [i.key for i in items[tray.SIZE].children]
        self.assertEqual(offered[:-1],
                         [tray.SIZE_PREFIX + n for n in overlay_size.names()])

    def test_the_size_he_is_on_is_the_one_with_the_tick(self):
        """The submenu is the only place that says what size the captions are,
        which is half of what it is for."""
        ticked = [i for i in size_submenu(caption_size="medium") if i.checked]
        self.assertEqual([i.key for i in ticked],
                         [tray.SIZE_PREFIX + "medium"])

    def test_with_nobody_having_said_the_shipped_size_is_the_ticked_one(self):
        ticked = [i for i in size_submenu(caption_size="") if i.checked]
        self.assertEqual([i.key for i in ticked],
                         [tray.SIZE_PREFIX + overlay_size.DEFAULT])

    def test_each_size_says_what_it_measures_and_names_its_command(self):
        item = {i.key: i for i in size_submenu()}[tray.SIZE_PREFIX + "compact"]
        self.assertIn("15 px", item.label)
        self.assertEqual(item.command, "dictate look compact")

    def test_the_things_a_menu_cannot_do_are_named_rather_than_hidden(self):
        """The words and the box apart, and the font: a preview-and-judge job,
        so the submenu's last line points at the command that does it."""
        last = size_submenu()[-1]
        self.assertFalse(last.enabled)
        self.assertIn("dictate overlay", last.command)

    def test_a_history_being_kept_can_be_opened_and_deleted_from_here(self):
        """The tray is the only surface a logon-started copy has, so it is
        where a record of everything he says has to be reachable - and where
        getting rid of it has to be one click and no hunting for a file."""
        keys = [item.key for item in tray.menu(state(history=True))]
        self.assertEqual(keys[-2:], [tray.HISTORY, tray.HISTORY_DELETE])

    def test_a_history_he_has_turned_off_is_not_mentioned_at_all(self):
        keys = [item.key for item in tray.menu(state(history=False))]
        self.assertNotIn(tray.HISTORY, keys)
        self.assertNotIn(tray.HISTORY_DELETE, keys)

    def test_the_delete_item_says_what_it_deletes(self):
        items = {item.key: item for item in tray.menu(state(history=True))}
        self.assertIn("Delete", items[tray.HISTORY_DELETE].label)
        self.assertEqual(items[tray.HISTORY_DELETE].command,
                         "dictate history --delete")
        # Not the default: a click on the icon must never be able to hit it.
        self.assertFalse(items[tray.HISTORY_DELETE].default)

    def test_starting_at_logon_is_on_the_menu_with_its_current_state(self):
        """The defect this item exists for: the feature was finished, shipped
        and on his machine, and he asked for it anyway because the only way in
        was a command he had never been shown."""
        on = {item.key: item for item in tray.menu(state(autostart=True))}[
            tray.AUTOSTART]
        off = {item.key: item for item in tray.menu(state(autostart=False))}[
            tray.AUTOSTART]
        self.assertEqual(on.label, "Start when I log in")
        self.assertEqual(off.label, "Start when I log in")
        # The tick is the state, and the command is what clicking will do.
        self.assertTrue(on.checked)
        self.assertFalse(off.checked)
        self.assertEqual(on.command, "dictate autostart disable")
        self.assertEqual(off.command, "dictate autostart enable")
        self.assertTrue(on.enabled)
        self.assertTrue(off.enabled)

    def test_off_is_exactly_as_easy_as_on(self):
        """One click either way, from the same item, in the same place."""
        for value in (True, False):
            item = {i.key: i for i in tray.menu(state(autostart=value))}[
                tray.AUTOSTART]
            self.assertTrue(item.enabled)
            self.assertFalse(item.children)
            self.assertFalse(item.default)

    def test_a_state_windows_would_not_answer_is_said_rather_than_guessed(self):
        """Claiming "it is off" about something nobody could read is how a
        menu ends up disagreeing with `dictate autostart status`."""
        item = {i.key: i for i in tray.menu(state(autostart=None))}[tray.AUTOSTART]
        self.assertFalse(item.enabled)
        self.assertFalse(item.checked)
        self.assertIn("cannot tell", item.label)
        self.assertEqual(item.command, "dictate autostart status")

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
        wired = actions(done)
        for item in tray.menu(state(history=True)):
            wired.invoke(item.key)
        self.assertEqual(done, ["stop", "restart", "check", "update",
                                "autostart", "log", "history", "history-delete"])

    def test_a_size_chosen_from_the_submenu_reaches_the_action_with_its_name(self):
        """Like the hotkey's, the key carries the value, so the Win32 side
        still knows nothing but a string."""
        done: list[str] = []
        wired = actions(done)
        self.assertTrue(wired.invoke(tray.SIZE_PREFIX + "medium"))
        self.assertEqual(done, ["size:medium"])

    def test_a_size_action_that_was_never_supplied_does_nothing(self):
        bare = tray.TrayActions(stop=lambda: None, restart=lambda: None,
                                open_log=lambda: None,
                                check_updates=lambda: None,
                                update_now=lambda: None)
        self.assertFalse(bare.invoke(tray.SIZE_PREFIX + "medium"))
        self.assertFalse(actions([]).invoke(tray.SIZE_PREFIX))

    def test_an_unknown_id_does_nothing_rather_than_raising(self):
        done: list[str] = []
        self.assertFalse(actions(done).invoke("nonsense"))
        self.assertFalse(actions(done).invoke(tray.STATUS))
        self.assertEqual(done, [])

    def test_the_hotkey_item_is_a_submenu_of_combinations(self):
        """He does not have a settings window and will not edit a config file,
        so the combinations are on the menu and each one is a click."""
        items = {item.key: item for item in tray.menu(state())}
        self.assertEqual(items[tray.HOTKEY].command, "dictate hotkey")
        keys = [child.key for child in items[tray.HOTKEY].children]
        self.assertTrue(all(k.startswith(tray.HOTKEY_PREFIX)
                            for k in keys[:-1]), keys)
        self.assertEqual(keys[-1], tray.HOTKEY_OTHER)

    def test_the_one_he_is_using_is_ticked_and_is_on_the_list(self):
        children = hotkey_submenu(hotkey_combination="ctrl + alt + space")
        ticked = [child for child in children if child.checked]
        self.assertEqual([child.key for child in ticked],
                         [tray.HOTKEY_PREFIX + "ctrl + alt + space"])

    def test_a_hotkey_of_his_own_is_added_to_the_list_rather_than_hidden(self):
        """A menu of four alternatives that does not include what he is using
        cannot be read: there is nothing to say which one he has."""
        children = hotkey_submenu(hotkey_combination="ctrl + shift + f")
        ticked = [child for child in children if child.checked]
        self.assertEqual([child.key for child in ticked],
                         [tray.HOTKEY_PREFIX + "control + shift + f"])
        self.assertIn("Ctrl + Shift + F", ticked[0].label)

    def test_a_hotkey_that_cannot_be_parsed_does_not_break_the_menu(self):
        """The menu is the only surface a logon-started copy has. A config with
        nonsense in it must not be able to empty it."""
        children = hotkey_submenu(hotkey_combination="++")
        self.assertTrue(children)
        self.assertFalse([child for child in children if child.checked])

    def test_every_combination_names_the_command_that_sets_it(self):
        for child in hotkey_submenu():
            if child.key.startswith(tray.HOTKEY_PREFIX):
                combination = child.key[len(tray.HOTKEY_PREFIX):]
                self.assertEqual(child.command,
                                 f'dictate hotkey "{combination}"')

    def test_anything_else_is_a_line_that_names_the_command_and_is_not_clickable(self):
        other = hotkey_submenu()[-1]
        self.assertEqual(other.key, tray.HOTKEY_OTHER)
        self.assertFalse(other.enabled)
        self.assertIn("dictate hotkey", other.command)

    def test_choosing_one_hands_the_combination_to_the_app(self):
        done: list[str] = []
        wired = actions(done)
        self.assertTrue(wired.invoke(tray.HOTKEY_PREFIX + "ctrl + alt + d"))
        self.assertEqual(done, ["hotkey:ctrl + alt + d"])

    def test_the_submenu_parent_itself_does_nothing(self):
        done: list[str] = []
        self.assertFalse(actions(done).invoke(tray.HOTKEY))
        self.assertFalse(actions(done).invoke(tray.HOTKEY_OTHER))
        self.assertFalse(actions(done).invoke(tray.HOTKEY_PREFIX))
        self.assertEqual(done, [])

    def test_a_hotkey_action_that_was_never_supplied_does_nothing(self):
        bare = tray.TrayActions(stop=lambda: None, restart=lambda: None,
                                open_log=lambda: None,
                                check_updates=lambda: None,
                                update_now=lambda: None)
        self.assertFalse(bare.invoke(tray.HOTKEY_PREFIX + "ctrl + alt + d"))

    def test_a_history_action_that_was_never_supplied_does_nothing(self):
        """A menu id from before the history was turned off must not reach a
        handler that is not there - least of all the delete one."""
        bare = tray.TrayActions(stop=lambda: None, restart=lambda: None,
                                open_log=lambda: None,
                                check_updates=lambda: None,
                                update_now=lambda: None)
        self.assertFalse(bare.invoke(tray.HISTORY_DELETE))
        self.assertFalse(bare.invoke(tray.HISTORY))

    def test_an_autostart_action_that_was_never_supplied_does_nothing(self):
        bare = tray.TrayActions(stop=lambda: None, restart=lambda: None,
                                open_log=lambda: None,
                                check_updates=lambda: None,
                                update_now=lambda: None)
        self.assertFalse(bare.invoke(tray.AUTOSTART))


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
        import tempfile
        import threading
        from pathlib import Path
        from types import SimpleNamespace

        from dictate import config as config_mod, history as history_mod
        from dictate.app import Application
        from dictate.engines.residency import Residency

        from . import fakes

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        app = object.__new__(Application)
        app.cfg = config_mod.load(None)
        app.console = lambda _msg="": None
        app.notices = []
        app.history = history_mod.HistoryStore(
            Path(tmp.name) / "history.txt",
            enabled=kwargs.pop("history_enabled", True))
        app.overlay = fakes.FakeOverlay()
        app.tray = None
        app.restart_wanted = False
        app._stopping = threading.Event()
        app._holding_hotkey = False
        app._in_flight = 0
        app._last_error = ""
        app._published_activity = None
        app._update = None
        # Nothing here is Windows, so "does it start at logon?" has no answer
        # unless a test supplies one.
        app._autostart_on = kwargs.pop("autostart", None)
        app._autostart_read_at = 0.0
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

    def test_the_menu_only_mentions_a_history_that_is_being_kept(self):
        self.assertTrue(self.app()._tray_state().history)
        self.assertFalse(self.app(history_enabled=False)._tray_state().history)

    def test_delete_from_the_tray_really_deletes_it_and_says_it_did(self):
        app = self.app()
        said: list[str] = []
        app.notify = lambda level, message: said.append(message)
        app.history.record("Something he would rather not keep.")
        self.assertTrue(app.history.path.exists())

        app._delete_history()
        self.assertFalse(app.history.path.exists())
        self.assertIn("deleted", said[0])

        app._delete_history()          # again, with nothing there
        self.assertIn("no dictation history", said[1])

    def test_opening_an_empty_history_says_so_rather_than_failing(self):
        """Before he has dictated anything the file does not exist yet, and
        nothing on Windows can open a file that is not there."""
        app = self.app()
        said: list[str] = []
        app.notify = lambda level, message: said.append(message)
        app._open_history()
        self.assertIn("nothing in the dictation history yet", said[0])

    def test_resizing_from_the_tray_changes_the_next_panel_and_the_config(self):
        """Both halves matter: the size this copy uses from now on, so the next
        thing he says is the size he just asked for, and the file, so it is
        still that size tomorrow."""
        import tempfile
        from pathlib import Path

        from dictate import config as config_mod

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dictate.toml"
            path.write_text('[overlay]\nsize = "huge"\n', encoding="utf-8")
            app = self.app()
            app.cfg = config_mod.load(path)
            said: list[str] = []
            app.notify = lambda level, message: said.append(message)

            self.assertTrue(app.set_caption_size("small"))
            self.assertEqual(app.cfg.overlay.size, "small")
            self.assertEqual(config_mod.load(path).overlay.size, "small")
            self.assertIn("small", said[0])
            # Nothing on screen moves: the overlay reads this once, when the
            # next panel appears.
            self.assertFalse(app.overlay.states)

    def test_resizing_from_the_tray_puts_a_split_pair_back_together(self):
        import tempfile
        from pathlib import Path

        from dictate import config as config_mod

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dictate.toml"
            path.write_text('[overlay]\nsize = "huge"\ntext_size = "small"\n',
                            encoding="utf-8")
            app = self.app()
            app.cfg = config_mod.load(path)
            app.notify = lambda level, message: None
            app.set_caption_size("medium")
            self.assertEqual(app.cfg.overlay.text_size, "")
            self.assertEqual(config_mod.load(path).overlay.text_size, "")

    def test_choosing_the_size_it_is_already_on_says_so_and_writes_nothing(self):
        app = self.app()
        said: list[str] = []
        app.notify = lambda level, message: said.append(message)
        app.cfg.overlay.size = "small"
        self.assertFalse(app.set_caption_size("small"))
        self.assertIn("already", said[0])

    def test_a_size_from_an_older_menu_does_nothing_rather_than_raising(self):
        """It runs on the thread that owns the icon, so nothing here may raise
        - and a menu built by a version with different names is the way an
        unknown one could arrive."""
        app = self.app()
        app.notify = lambda level, message: None
        self.assertFalse(app.set_caption_size("enormous"))
        self.assertEqual(app.cfg.overlay.size, "compact")

    def test_a_config_it_cannot_write_still_resizes_for_this_session(self):
        """He asked for smaller captions. A read-only config file is worth
        saying out loud and is not worth refusing him over."""
        from pathlib import Path

        app = self.app()
        said: list[str] = []
        app.notify = lambda level, message: said.append(message)
        app.cfg.source_path = Path("/nowhere/at/all/dictate.toml")
        self.assertTrue(app.set_caption_size("medium"))
        self.assertEqual(app.cfg.overlay.size, "medium")
        self.assertIn("could not be written", said[-1])

    def test_no_config_file_at_all_is_a_message_and_not_a_crash(self):
        app = self.app()
        said: list[str] = []
        app.notify = lambda level, message: said.append(message)
        app.cfg.source_path = None
        self.assertTrue(app.set_caption_size("medium"))
        self.assertEqual(app.cfg.overlay.size, "medium")
        self.assertIn("could not be written", said[-1])

    def test_the_menu_shows_the_size_the_running_copy_is_using(self):
        app = self.app()
        app.cfg.overlay.size = "small"
        self.assertEqual(app._tray_state().caption_size, "small")

    def _autostart_app(self, registered):
        """An app whose only window on Task Scheduler is a list of calls.

        `autostart.enable` and `autostart.disable` are the real ones everywhere
        else and refuse to run off Windows, deliberately; what is under test here
        is which of the two a click reaches, and what the tick says afterwards.
        """
        from dictate import autostart as autostart_mod

        app = self.app()
        calls: list[str] = []
        said: list[tuple[str, str]] = []
        app.notify = lambda level, message: said.append((level, message))
        app._refresh_tray = lambda: None

        def enable(cfg, config_path=None):
            calls.append("enable")
            registered[0] = True
            return ["dictate will now start when you log in."]

        def disable():
            calls.append("disable")
            registered[0] = False
            return ["dictate will no longer start when you log in."]

        self._patch(autostart_mod, "enable", enable)
        self._patch(autostart_mod, "disable", disable)
        self._patch(autostart_mod, "registered_or_unknown", lambda: registered[0])
        return app, calls, said

    def _patch(self, module, name, value):
        previous = getattr(module, name)
        setattr(module, name, value)
        self.addCleanup(setattr, module, name, previous)

    def test_the_tray_turns_starting_at_logon_on_and_off_again(self):
        """Off has to stay exactly as easy as on: the same item, one click."""
        registered = [False]
        app, calls, _said = self._autostart_app(registered)

        app.toggle_autostart()
        self.assertEqual(calls, ["enable"])
        self.assertTrue(app._tray_state().autostart)

        app.toggle_autostart()
        self.assertEqual(calls, ["enable", "disable"])
        self.assertFalse(app._tray_state().autostart)

    def test_which_way_a_click_goes_is_read_now_rather_than_from_the_menu(self):
        """He can have enabled it in a PowerShell window since the menu was
        drawn. Turning it off when he meant to turn it on is the one mistake
        here that would matter."""
        registered = [False]
        app, calls, _said = self._autostart_app(registered)
        app._autostart_on = False        # what the menu was drawn from
        registered[0] = True             # what is true now
        app.toggle_autostart()
        self.assertEqual(calls, ["disable"])

    def test_a_state_that_cannot_be_read_changes_nothing(self):
        registered = [None]
        app, calls, said = self._autostart_app(registered)
        app.toggle_autostart()
        self.assertEqual(calls, [])
        self.assertIn("could not tell", said[0][1])
        self.assertIsNone(app._tray_state().autostart)

    def test_a_refusal_from_windows_leaves_the_tick_where_windows_left_it(self):
        """An enable Windows would not accept must not draw a tick: the menu
        would then be the only thing on the machine that believes it is on."""
        from dictate import autostart as autostart_mod
        from dictate.errors import DictateError

        registered = [False]
        app, _calls, said = self._autostart_app(registered)

        def refuse(cfg, config_path=None):
            raise DictateError("Windows did not accept the task.", "Try again.")

        self._patch(autostart_mod, "enable", refuse)
        app.toggle_autostart()
        self.assertEqual(said[0][0], "error")
        self.assertIn("did not work", said[0][1])
        self.assertFalse(app._tray_state().autostart)

    def test_the_answer_is_not_re_asked_on_every_refresh(self):
        """`schtasks` is a process. The hotkey and transcription paths call
        `_tray_state`; neither may pay for a menu tick."""
        from dictate import autostart as autostart_mod
        from dictate.app import AUTOSTART_POLL_S

        asked = []
        self._patch(autostart_mod, "registered_or_unknown",
                    lambda: (asked.append(1), True)[1])
        app = self.app()
        app._read_autostart(force=True, now=1000.0)
        for _ in range(50):
            app._tray_state()
        app._read_autostart(now=1001.0)
        self.assertEqual(len(asked), 1)
        # ...and it does catch up, on the slow loop, with a change made in
        # another window.
        app._read_autostart(now=1000.0 + 2 * AUTOSTART_POLL_S)
        self.assertEqual(len(asked), 2)

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
