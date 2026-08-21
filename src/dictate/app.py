"""Wiring: the one place where the platform layer, the two engines and the
pipeline are assembled into a running application.

Thread layout:

    main thread      Tk overlay message loop. Owns every Tk call.
    hotkey thread    owned by the keyboard hook; calls start/finish.
    mouse hook       only with a mouse trigger: two threads of its own, one
                     owning the WH_MOUSE_LL hook and one doing the work it
                     decided on (platform/windows/mouse.py).
    audio thread     owned by PortAudio; calls push_audio and nothing else.
    caption thread   the only thread that touches a streaming session.
    finalize worker  ONE worker, so that if two utterances finish close
                     together their text is pasted in the order it was spoken.
    idle watcher     ticks the clock for `ResidentModel`; a short-lived worker
                     of its own does the actual unload/reload (residency.py).

Startup order matters and is deliberate: the whisper server is brought up and
warmed *before* the hotkey is registered, so the very first press already has a
resident model behind it and nobody discovers a missing model file mid-sentence.

After that the model's residency follows use rather than process lifetime: it is
unloaded after `[whisper] idle_release_minutes` without dictation so the card is
genuinely free, and reloaded from `_on_hotkey_press` - at key DOWN, so the load
runs while he is speaking rather than after he stops. See engines/residency.py.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import (
    autostart as autostart_mod, config_edit, deferred as deferred_mod,
    history as history_mod,
    hotkey_switch, instance, overlay_size, tray as tray_mod,
    update as update_mod,
)
from .cleanup.service import CleanupService
from .config import Config
from .engines.residency import ResidentModel, Residency
from .engines.sherpa_stream import SherpaStreamingTranscriber
from .engines.whisper_backend import WhisperVulkanBackend
from .errors import DictateError, MouseHookError
from .pipeline import Pipeline
from .platform import factory
from .platform.base import OverlayState
from .punctuation.service import PunctuationService

log = logging.getLogger(__name__)

#: `dictate run` finished because something asked for a restart - the tray's
#: Restart item. The caller (`cli.cmd_run`, `autostart.run_at_logon`) starts the
#: new copy AFTER it has let go of the instance lock, never before: a restart
#: that raced its own predecessor would be refused as "already running".
EXIT_RESTART = 7


#: How often the tray re-asks Windows whether the logon task is still there.
#: Deliberately not on the hotkey path and not on every tray refresh - schtasks
#: is a process, and this is a setting that changes about once a year. It is
#: asked on the one loop that already ticks slowly, so that the tick beside
#: "Start when I log in" still catches up with a `dictate autostart enable` typed
#: in another window.
AUTOSTART_POLL_S = 30.0


class Application:
    def __init__(self, cfg: Config, *, console=None,
                 suggest_autostart: bool = False) -> None:
        self.cfg = cfg
        self.console = console or (lambda msg: print(msg, file=sys.stderr, flush=True))
        #: Whether this copy was started by hand in a console, and so is the one
        #: that should mention that it need not have been. A logon-started copy
        #: passes False: it is the proof the offer has already been taken.
        self.suggest_autostart = suggest_autostart
        #: Does dictate start when he logs in? `None` until it has been asked,
        #: and whenever Windows would not say. Read from one place
        #: (`autostart.registered_or_unknown`) so the menu and
        #: `dictate autostart status` cannot answer differently.
        self._autostart_on: bool | None = None
        self._autostart_read_at = 0.0
        self._stopping = threading.Event()
        self._threads: list[threading.Thread] = []
        self._started_at = time.time()
        #: Set by the tray's Restart item; read by whoever owns the lock.
        self.restart_wanted = False
        self.tray = None

        self.overlay = factory.make_overlay(cfg, notify=self.notify)
        self.tracker = factory.make_window_tracker()
        self.injector = factory.make_injector(cfg, self.tracker)
        self.audio = factory.make_audio_capture(cfg)
        # `notify` is how a mouse trigger says that its hook was refused, or
        # has been lost while dictate was running - the one failure that leaves
        # dictate working (on the keyboard chord) rather than stopping it.
        self.hotkey = factory.make_hotkey_listener(cfg, notify=self.notify)

        # The backend is wrapped, not replaced: it still owns the process, the
        # health wait, the restart budget and the clean shutdown. The wrapper
        # only decides how long the model stays loaded between dictations.
        self.batch = ResidentModel(
            WhisperVulkanBackend(cfg.whisper, resolve=cfg.resolve, notify=self.notify),
            idle_release_s=cfg.whisper.idle_release_minutes * 60.0,
            wait_timeout_s=cfg.whisper.startup_timeout_s + 60.0,
        )
        self.cleaner = CleanupService(
            cfg.resolve(cfg.cleanup.rules_file) if cfg.cleanup.enabled else None,
            enabled=cfg.cleanup.enabled,
            notify=self.notify,
        )
        # Spoken punctuation is a stage of its own, and it runs AFTER the
        # cleanup pass: cleanup may only delete words and this substitutes them,
        # so it could not live inside it without taking that guarantee apart.
        # Off unless he turned it on, in which case this is None and the
        # pipeline pastes exactly what it pasted before.
        self.punctuator = PunctuationService(
            cfg.resolve(cfg.punctuation.rules_file) if cfg.punctuation.enabled else None,
            enabled=cfg.punctuation.enabled,
            notify=self.notify,
        )
        self.streaming = (
            SherpaStreamingTranscriber.from_config(cfg) if cfg.captions.enabled else None
        )
        self.history = history_mod.HistoryStore(
            history_mod.path_for(cfg),
            keep=cfg.history.keep,
            enabled=cfg.history.enabled,
            notify=self.notify,
        )
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dictate-finalize")
        # A worker of its own, never the transcription pool above: the mic has
        # to come down the instant a recording ends, not whenever a queued
        # GPU pass happens to finish, and `push_audio` can call all the way
        # into a stop on the audio callback's own thread (the max_utterance_s
        # ceiling) - PortAudio forbids stopping a stream from inside its own
        # callback, so that call can never run inline. See Pipeline's
        # `mic_submit` and the module docstring's guarantee.
        self._mic_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dictate-mic")
        #: Transcriptions queued or running, so the tray can say "transcribing"
        #: for exactly as long as that is true and not a moment longer.
        self._in_flight = 0
        self._holding_hotkey = False
        self._last_error = ""
        #: The last thing published for `dictate update` to read; `None` until
        #: something has been, so the first idle state is written too.
        self._published_activity: str | None = None
        #: The update started from the tray, while it may still be running. It
        #: is kept rather than its pid so that "is it still going?" can be asked
        #: rather than assumed - an update that failed before it stopped
        #: anything leaves this copy running, and he must be able to try again.
        self._update = None

        # Restore mode never restores foreground now. This owner keeps one
        # finished batch result, watches for the user to return to its captured
        # window, and sends through the same one-worker lane as transcription so
        # two text deliveries can never overlap.
        self.deferred = deferred_mod.DeferredDelivery(
            windows=self.tracker,
            injector=self.injector,
            overlay=self.overlay,
            history=self.history,
            submit=self._submit,
            notify=self.notify,
        )

        self.pipeline = Pipeline(
            batch=self.batch,
            cleaner=self.cleaner,
            injector=self.injector,
            windows=self.tracker,
            overlay=self.overlay,
            punctuator=self.punctuator,
            streaming=self.streaming,
            sample_rate=cfg.audio.sample_rate,
            block_ms=cfg.audio.block_ms,
            min_utterance_ms=cfg.audio.min_utterance_ms,
            max_utterance_s=cfg.audio.max_utterance_s,
            max_caption_chars=cfg.overlay.max_chars,
            # The three that decide where the finished text is allowed to go
            # when he has clicked somewhere else since he started speaking. The
            # decision is `delivery.py`; the injector is only ever asked once
            # the pipeline has made it.
            on_focus_change=cfg.paste.on_focus_change,
            restore_focus=cfg.paste.restore_focus,
            hold_to_clipboard=cfg.paste.hold_to_clipboard,
            begin_deferred=self.deferred.begin_utterance,
            defer_delivery=self.deferred.defer,
            submit=self._submit,
            notify=self.notify,
            record=self.history.record,
            # The pipeline owns the mic's lifetime from here - started at
            # press, stopped on every exit from a recording - rather than the
            # single always-on stream this used to be. See `_mic_pool` above.
            audio=self.audio,
            mic_submit=self._mic_pool.submit,
        )
        # The paste guard that clears his modifiers has to know when he has
        # already started the next utterance: a synthesised key-up goes through
        # the same keyboard hook the hotkey listens on, so forcing one then
        # would end the recording he has just begun (`platform/modifier_guard`).
        # Set here rather than passed to the factory, because the pipeline it
        # asks does not exist until this line.
        self.injector.is_recording = self.pipeline.is_recording

    # -- user-facing messages -------------------------------------------

    def notify(self, level: str, message: str) -> None:
        prefix = {"error": "!!", "warning": " !", "info": "  "}.get(level, "  ")
        self.console(f"{prefix} {message}")
        getattr(log, "error" if level == "error" else "info")("%s", message)
        if level == "error":
            # The tray is the only surface a logon-started copy has, so an error
            # it cannot otherwise report goes there and stays there until the
            # next time he dictates successfully.
            self._last_error = message.splitlines()[0] if message else ""
            self._refresh_tray()

    def _submit(self, fn, *args, **kwargs):
        """Everything the pipeline finalises goes through here, so that "is it
        transcribing right now?" has an answer rather than an estimate."""
        self._in_flight += 1
        try:
            future = self._pool.submit(fn, *args, **kwargs)
        except RuntimeError:
            self._in_flight = max(0, self._in_flight - 1)
            raise
        future.add_done_callback(self._finalize_done)
        self._refresh_tray()
        return future

    def _finalize_done(self, _future) -> None:
        self._in_flight = max(0, self._in_flight - 1)
        self._refresh_tray()

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        # First, before anything slow: a request left by a copy that is no
        # longer here was not meant for this one, and honouring it would make
        # dictate exit the moment it started. Clearing it now rather than at the
        # end of start() also means a `dictate stop` sent DURING startup is
        # still seen, instead of being wiped by our own tidying up.
        instance.clear_stop_request()
        self.console(f"dictate: cleanup rules  {self.cfg.cleanup.rules_file}"
                     if self.cfg.cleanup.enabled else "dictate: cleanup disabled")
        self.cleaner.load()
        # Eagerly, for the same reason as the cleanup rules: a rules file he has
        # broken is reported now, not in the middle of a sentence.
        self.console(f"dictate: spoken marks   {self.cfg.punctuation.rules_file}"
                     if self.cfg.punctuation.enabled
                     else "dictate: spoken punctuation off")
        self.punctuator.load()

        self.console("dictate: starting whisper-server (loading the model into "
                     "the graphics card)…")
        self.batch.start()
        self.console(f"dictate: batch engine   {self.batch.describe}")

        if self.streaming is not None:
            try:
                self._warm_captions()
                self.console(f"dictate: live captions  {self.streaming.describe}")
            except DictateError as exc:
                # Captions are a nice-to-have; the product still works without
                # them, so this degrades instead of refusing to start. It says
                # so loudly rather than pretending captions are running.
                self.console("!! live captions are OFF for this session:")
                self.console(f"   {exc.report()}")
                self.streaming = None
                self.pipeline.streaming = None

        # Proved once, here, so a missing or broken microphone is reported now
        # rather than on his first dictation - the same eagerness as the
        # cleanup rules and the punctuation file above. The second argument to
        # `start` is what makes "some of that recording was thrown away" a
        # thing he is told at the release rather than a warning in a log file
        # that stops after the hundredth one. It is stopped again immediately:
        # from here on, the pipeline opens it at each hotkey press and closes
        # it at each release, so the mic is live only while he is actually
        # dictating - not for the rest of the time dictate is running.
        self.audio.start(self.pipeline.push_audio, self.pipeline.note_input_loss)
        self.audio.stop()
        self.console(f"dictate: microphone     {self.audio.describe}")
        self.console(f"dictate: paste method   {self.injector.describe}")
        # Said out loud, once, because a record of everything he says is not
        # something to keep quietly: he should know it exists and where it is.
        self.console(f"dictate: history        {self.history.describe}")

        self._spawn(self._caption_loop, "dictate-captions")
        self._spawn(self._stop_request_loop, "dictate-stop-watch")
        self._start_hotkey()
        # Once, before the tray is built, so that the icon's tick and the line
        # below are the same answer rather than two readings a moment apart.
        self._read_autostart(force=True)
        self._start_tray()
        self.console("dictate: ready. Hold the hotkey and speak. Ctrl+C here to quit,")
        self.console("dictate: or `dictate stop` from any other window.")
        self._offer_autostart()

    def _start_hotkey(self) -> None:
        """Register the trigger, then say which one he actually has.

        A mouse trigger is the only one that can half-work. Its low-level hook
        can be refused - security software is the usual reason - and
        `TriggerPair` raises for that with the keyboard chord already
        registered and running behind it. So this degrades and says so, the way
        live captions and the tray icon do, rather than refusing to start:
        dictate on the chord he has been using for weeks is worth having, and a
        product that will not run because a mouse button was unavailable would
        be absurd. The line below it is the truth either way, because
        `describe` reports what is in force and not what was asked for.
        """
        try:
            self.hotkey.register(self._on_hotkey_press, self._on_hotkey_release)
            self.hotkey.start()
        except MouseHookError as exc:
            # `notify`, not `console`: this is the state the tray icon has to
            # go red for. A logon-started copy has no console to read.
            self.notify("error", exc.report())
        self.console(f"dictate: hotkey         {self.hotkey.describe}")

    def _offer_autostart(self) -> None:
        """Tell a console start, once, that it did not have to be a console.

        This is the third place the same offer is made - the end of setup and the
        tray menu are the other two - and it exists because the product owner
        watched this banner all evening while asking for a feature that was
        already installed on his machine. It says nothing when dictate already
        starts at logon, and nothing when that could not be read.
        """
        if not self.suggest_autostart:
            return
        lines = autostart_mod.console_hint(self._autostart_on)
        if not lines:
            return
        self.console("dictate:")
        for line in lines:
            self.console(f"dictate: {line}")

    # -- the icon in the notification area -------------------------------

    def _start_tray(self) -> None:
        """Put the icon up, or say why there is not one and carry on.

        It is the only thing on screen when dictate starts at logon, so it is
        worth reporting when it is missing - but it is not worth refusing to
        run over, any more than live captions are.
        """
        if not self.cfg.tray.enabled:
            self.console("dictate: tray icon      off ([tray] enabled = false)")
            return
        actions = tray_mod.TrayActions(
            stop=self.request_stop_from_tray,
            restart=self.request_restart,
            open_log=self._open_log_folder,
            check_updates=self.check_for_updates,
            update_now=self.update_now,
            open_history=self._open_history,
            delete_history=self._delete_history,
            set_hotkey=self.change_hotkey,
            set_caption_size=self.set_caption_size,
            toggle_autostart=self.toggle_autostart,
        )
        try:
            self.tray = factory.make_tray_icon(
                actions, icon_dir=instance.state_dir(), state=self._tray_state())
            self.tray.start()
            self.console(f"dictate: tray icon      {self.tray.describe}")
        except DictateError as exc:
            self.tray = None
            self.console("!! there is no dictate icon in the notification area:")
            self.console(f"   {exc.report()}")
        except Exception as exc:  # noqa: BLE001 - an icon may not stop the app
            self.tray = None
            log.exception("the tray icon could not be created")
            self.console(f"!! there is no dictate icon in the notification area: {exc}")
            self.console("   dictate is running anyway - stop it with `dictate stop`.")

    def _tray_state(self) -> tray_mod.TrayState:
        from .platform.hotkey_spec import describe  # noqa: PLC0415

        resident = self.batch.state is Residency.RESIDENT
        if self._stopping.is_set():
            status = tray_mod.TrayStatus.STOPPING
        elif self._holding_hotkey:
            status = tray_mod.TrayStatus.LISTENING
        elif self._in_flight:
            status = tray_mod.TrayStatus.WORKING
        elif self._last_error:
            status = tray_mod.TrayStatus.ERROR
        elif resident:
            status = tray_mod.TrayStatus.READY
        else:
            status = tray_mod.TrayStatus.RESTING
        try:
            hotkey = describe(self.cfg.hotkey.combination)
        except DictateError:
            hotkey = self.cfg.hotkey.combination
        return tray_mod.TrayState(status=status, hotkey=hotkey,
                                  hotkey_combination=self.cfg.hotkey.combination,
                                  model_resident=resident, detail=self._last_error,
                                  updating=self.update_in_flight(),
                                  history=self.history.enabled,
                                  caption_size=self.cfg.overlay.size,
                                  autostart=self._autostart_on)

    def update_in_flight(self) -> bool:
        """Is the update this copy started still going?

        Asked of the process itself, so a run that ended - because it failed
        before it stopped anything, or because he closed its window - puts the
        menu item back rather than greying it out for the rest of the session.
        A process that will not answer is treated as still running: refusing a
        second update is always the safer of the two mistakes.
        """
        process = self._update
        if process is None:
            return False
        try:
            if process.poll() is None:
                return True
        except Exception:
            log.debug("could not tell whether the update is still running",
                      exc_info=True)
            return True
        self._update = None
        return False

    def _refresh_tray(self) -> None:
        state = self._tray_state()
        self._publish_activity(state)
        tray = self.tray
        if tray is None:
            return
        try:
            tray.update(state)
        except Exception:
            log.debug("the tray icon could not be updated", exc_info=True)

    #: What each tray status means to somebody outside this process who is about
    #: to stop it. Only these two are "do not interrupt me".
    _BUSY_WITH = {
        tray_mod.TrayStatus.LISTENING: "an utterance - you are speaking",
        tray_mod.TrayStatus.WORKING: "transcribing what you just said",
    }

    def _publish_activity(self, state: tray_mod.TrayState) -> None:
        """Let `dictate update` know whether this is a moment to be stopped in.

        The same answer the tray icon is already showing, written to a file only
        when it changes - so nothing in the hotkey or transcription path does any
        more work than it did before, and an idle dictate writes nothing at all.
        """
        what = self._BUSY_WITH.get(state.status, "")
        if what == self._published_activity:
            return
        self._published_activity = what
        instance.publish_activity(bool(what), what)

    def _open_log_folder(self) -> None:
        """The tray's last item. The folder, not the file: the log may not
        exist yet, and what he wants is somewhere to look."""
        target = Path(self.cfg.logging.file).parent if self.cfg.logging.file \
            else instance.state_dir()
        try:
            os.startfile(str(target))  # noqa: S606 - Windows only, by design
        except Exception:
            log.exception("could not open %s", target)
            self.notify("warning", f"dictate could not open {target}. Open it "
                                   f"yourself - the log is in there.")

    def check_for_updates(self) -> None:
        """The tray's `dictate update --check`.

        A report and nothing else: it does not stop this copy, does not touch
        the install folder, and does not repair an update that was interrupted -
        it names one. So there is nothing to guard and nothing to undo, and it
        may be run as often as he likes.
        """
        self._start_update(check_only=True)

    def update_now(self) -> None:
        """The tray's `dictate update`.

        The window it opens will ask this process to stop - through the same
        file `dictate stop` uses - update the folder Python loads dictate from,
        and start a fresh copy the way it was started before. So the icon
        disappears part way through and comes back a few seconds later with the
        new version behind it, and the window is what is on screen in between.
        """
        self._start_update(check_only=False)

    def _start_update(self, *, check_only: bool) -> None:
        """Open a window and run `dictate update` in it.

        The update deliberately does NOT run in this process. This is the
        process it is going to stop and replace, and a thread doing the writing
        when that happens would be killed in the middle of it. See the note at
        the foot of `update.py`.

        Nothing here blocks: this runs on the thread that owns the icon, and a
        tray that stops answering is a tray that has stopped being the one
        visible thing. Starting a process is all it does.
        """
        what = "dictate update --check" if check_only else "dictate update"
        if not check_only and self.update_in_flight():
            # The menu greys this out, but the click that opened the menu and
            # the click that chose the item are two moments; this is the one
            # that is actually authoritative. Two updates over one folder is
            # the only way this could hurt him.
            self.console("dictate: an update is already running in its own window.")
            return
        config_path = str(self.cfg.source_path) if self.cfg.source_path else None
        try:
            process = update_mod.start_in_console(config_path, check_only=check_only)
        except DictateError as exc:
            # There is no console to print this in and no dialog worth showing:
            # a modal box would block the thread that owns the icon until
            # somebody clicked it. `notify` turns the icon red and puts the
            # first line in the tooltip, which is the surface this copy has.
            self.notify("error", f"{what} could not be started: {exc.message}")
            log.error("%s", exc.report())
            return
        if not check_only:
            self._update = process
        self.console(f"dictate: {what} is running in a window of its own "
                     f"(process {getattr(process, 'pid', '?')}).")
        self._refresh_tray()

    def _open_history(self) -> None:
        """The tray's history item, and what `dictate history` does. The file
        itself this time, not the folder: it is what he wants to read."""
        path = self.history.path
        if not path.exists():
            self.notify("info", "There is nothing in the dictation history yet - "
                                "it fills up as you dictate.")
            return
        try:
            os.startfile(str(path))  # noqa: S606 - Windows only, by design
        except Exception:
            log.exception("could not open %s", path)
            self.notify("warning", f"dictate could not open {path}. Open it "
                                   f"yourself - it is a plain text file.")

    def _delete_history(self) -> None:
        """The tray's other history item. It deletes; it does not ask.

        There is no dialog because a running dictate is not allowed one (see
        `tray.menu`), so the item says exactly what it does instead.
        """
        if self.history.delete():
            self.notify("info", "The dictation history has been deleted.")
        else:
            self.notify("info", "There was no dictation history to delete.")

    def set_caption_size(self, name: str) -> bool:
        """The tray's "Caption size", and one named rung of `overlay_size`.

        Two things happen and the order is the point, though it is a gentler
        order than the hotkey's above: nothing here can be refused by Windows.

        1. the size on this process's own config changes, so the *next* caption
           panel is the new size. The overlay reads these values once per
           appearance, so a panel on screen right now does not move or resize -
           rule 2b of the look, and it holds here by doing nothing;
        2. then it is written to his config file, so it is still that size
           tomorrow.

        A file that cannot be written is worth saying out loud and is not worth
        losing the change over: he asked for smaller captions and he has them
        for this session. Nothing here may raise - it runs on the thread that
        owns the icon.
        """
        try:
            overlay_size.multiplier(name)
        except DictateError as exc:
            # A menu id from a copy of the menu built by an older version.
            log.warning("%s", exc.message)
            return False
        if name == self.cfg.overlay.size and not self.cfg.overlay.text_size \
                and not self.cfg.overlay.panel_size:
            self.notify("info", f"The captions are already {name}.")
            return False
        # The two overrides go with it: choosing a size is also the way back
        # from a text size and a panel size he has pulled apart by hand.
        self.cfg.overlay.size = name
        self.cfg.overlay.text_size = overlay_size.FOLLOW
        self.cfg.overlay.panel_size = overlay_size.FOLLOW
        kept = self._persist_caption_size(name)
        self.notify("info", f"Captions are now {name}. The next thing you say "
                            f"will be that size."
                            + ("" if kept else " It could not be written to your "
                               "config file, so it lasts until dictate restarts."))
        self._refresh_tray()
        return True

    def _persist_caption_size(self, name: str) -> bool:
        """Write it into his `dictate.toml`, comments untouched.

        Through `overlay_size`, which goes through `config_edit` - the same
        writer the hotkey above uses, and the only one.
        """
        if self.cfg.source_path is None:
            return False
        try:
            overlay_size.write(Path(self.cfg.source_path), {
                "size": name,
                "text_size": overlay_size.FOLLOW,
                "panel_size": overlay_size.FOLLOW,
            })
        except DictateError as exc:
            log.error("%s", exc.report())
            return False
        except Exception:
            log.exception("could not write the caption size")
            return False
        return True

    # -- starting when he logs in -----------------------------------------

    def _read_autostart(self, *, force: bool = False, now: float | None = None) -> None:
        """Ask Windows whether the logon task is there, at most now and then.

        Called from the slow watch loop and after this copy has changed it -
        never from the hotkey or transcription path, and never from
        `_tray_state`, which those two do call. The cost of asking is a process;
        the cost of a stale answer is a tick that is up to `AUTOSTART_POLL_S` out
        of date on a menu he opens by hand.
        """
        now = time.time() if now is None else now
        if not force and now - self._autostart_read_at < AUTOSTART_POLL_S:
            return
        self._autostart_read_at = now
        try:
            self._autostart_on = autostart_mod.registered_or_unknown()
        except Exception:  # noqa: BLE001 - a menu tick may never stop anything
            log.debug("could not read whether dictate starts at logon", exc_info=True)
            self._autostart_on = None

    def toggle_autostart(self) -> None:
        """The tray's "Start when I log in", both ways.

        It is `dictate autostart enable` and `dictate autostart disable` and
        nothing else: `autostart.py` registers and removes the task, this only
        decides which of the two a click means. That decision is made from a
        fresh reading rather than from the label that was drawn - the menu can
        be a moment old, and turning it off when he meant to turn it on is the
        one mistake here that would matter.

        Nothing is asked. Turning it on registers a task, starts the windowless
        copy and prints what each of those did; turning it off deletes the task
        and leaves the running copy alone (`autostart.disable` says why). From
        HERE the start is always the "already running" case - this menu is drawn
        by a copy that is running - and that is the point of it being decided in
        `autostart.start_now` rather than at each of the three call sites: one
        copy at a time survives being reached from a place that is one. A
        running dictate
        may not show a dialog (it holds the instance lock, and the box would
        block the thread that owns the icon), so the item says what it does and
        the tick says what happened.
        """
        self._read_autostart(force=True)
        if self._autostart_on is None:
            self.notify("warning",
                        "dictate could not tell whether it is set to start when "
                        "you log in, so it has changed nothing. `dictate "
                        "autostart status` shows what Windows answered.")
            return
        was_on = self._autostart_on
        try:
            lines = (autostart_mod.disable() if was_on else
                     autostart_mod.enable(
                         self.cfg,
                         config_path=str(self.cfg.source_path)
                         if self.cfg.source_path else None))
        except DictateError as exc:
            what = "disable" if was_on else "enable"
            self.notify("error", f"dictate autostart {what} did not work: "
                                 f"{exc.message}")
            log.error("%s", exc.report())
        else:
            for line in lines:
                self.console(line)
        # Whatever happened, the tick now says what Windows says - including
        # after a failure, where nothing changed and the menu must not pretend
        # otherwise.
        self._read_autostart(force=True)
        self._refresh_tray()

    def change_hotkey(self, combination: str) -> bool:
        """The tray's "Change the hotkey", and `dictate hotkey` for a copy that
        is already running.

        The order is the whole of the safety here, and it is not negotiable:

        1. register the new combination - Windows is the only thing that can say
           whether another program already owns it;
        2. if it will not take it, put the old one back and say so. Nothing has
           been written down, so a restart brings back the hotkey he had;
        3. only once it IS registered, write it to his config file, so it
           survives the restart.

        Doing it the other way round - write, then try - is how somebody ends up
        with a config naming a hotkey that does not work and no way in but a
        text editor, which is the one thing he has said he will not do.

        What is decided rather than performed lives in `hotkey_switch`, which is
        pure and tested; this is the sequence and the two Windows calls.
        """
        decision = hotkey_switch.decide(
            self.cfg.hotkey.combination, combination,
            recording=self.pipeline.is_recording)
        if not decision.act:
            self.notify(decision.level, decision.message)
            return False

        previous, wanted = self.hotkey, decision.combination
        listener = None
        try:
            listener = factory.make_hotkey_listener(self.cfg, wanted,
                                                    notify=self.notify)
            previous.stop()
            listener.register(self._on_hotkey_press, self._on_hotkey_release)
            listener.start()
        except Exception as exc:  # noqa: BLE001 - reported, and the old one is back
            log.warning("the hotkey could not be changed to %s: %s", wanted, exc)
            reason = exc.message if isinstance(exc, DictateError) else str(exc)
            # A trigger can fail with half of itself installed: a mouse button
            # that Windows would not hook comes back with its keyboard chord
            # already registered and running (`platform/trigger_pair.py`), and
            # leaving that alive would be two listeners on one combination.
            # Nothing is changed here means nothing, including that.
            if listener is not None:
                try:
                    listener.stop()
                except Exception:
                    log.debug("stopping the refused trigger raised", exc_info=True)
            self._restore_hotkey(previous)
            self.notify("error", hotkey_switch.refused(
                wanted, self.cfg.hotkey.combination, reason))
            return False

        self.hotkey = listener
        self.cfg.hotkey.combination = wanted
        level, message = hotkey_switch.applied(
            wanted, config_path=str(self.cfg.source_path) if self.cfg.source_path
            else None, persisted=self._persist_hotkey(wanted))
        self.notify(level, message)
        self._refresh_tray()
        return True

    def _persist_hotkey(self, combination: str) -> bool:
        """Write it into his `dictate.toml`, one line, comments untouched."""
        if self.cfg.source_path is None:
            return False
        try:
            config_edit.write_string(self.cfg.source_path, "hotkey",
                                     "combination", combination)
        except DictateError as exc:
            log.error("%s", exc.report())
            return False
        return True

    def _restore_hotkey(self, listener) -> None:
        """Put back the listener that was working a moment ago.

        If even this fails there is no hotkey at all, which is dictate not
        working - so it is said in the loudest terms this process has, and it
        names the one command that fixes anything.
        """
        try:
            listener.register(self._on_hotkey_press, self._on_hotkey_release)
            listener.start()
            self.hotkey = listener
        except MouseHookError as exc:
            # Half of it came back: `TriggerPair` never raises this until the
            # keyboard chord behind the mouse button is registered and running.
            # So he can still dictate, and is told with which - the opposite of
            # the message below, which is for having nothing at all.
            self.hotkey = listener
            self.notify("error", exc.report())
        except Exception:
            log.exception("the previous hotkey could not be registered again")
            self.notify("error", "dictate has no hotkey now: the one it was "
                                 "using could not be registered again. Run "
                                 "`dictate stop` and start it again.")

    def request_stop_from_tray(self) -> None:
        """The tray's Stop item. Exactly what `dictate stop` asks for, through
        exactly the same door, so there is only ever one shutdown path."""
        self.console("\ndictate: stop chosen from the tray; shutting down…")
        self._refresh_tray()
        self.overlay.close()   # ends run_forever, which triggers stop()

    def request_restart(self) -> None:
        """The tray's Restart item: stop, then start a fresh copy.

        The starting is not done here. This process still holds the instance
        lock and the transcription port, and a new copy would be refused by
        both; so it records the wish and shuts down, and whoever owns the lock
        (`cli.cmd_run`) starts the new one once it has been released.
        """
        self.restart_wanted = True
        self.console("\ndictate: restart chosen from the tray; shutting down first…")
        self._refresh_tray()
        self.overlay.close()

    def _on_hotkey_release(self) -> None:
        self._holding_hotkey = False
        self._last_error = ""
        try:
            self.pipeline.finish_utterance()
        finally:
            self._refresh_tray()

    def _on_hotkey_press(self) -> bool:
        """Hotkey down.

        The warm-up is asked for FIRST and returns straight away. If the model
        was released while he was not dictating, it starts loading now, while he
        is still holding the key and speaking - which is exactly as long as the
        load takes. Waiting until he let go would spend that time twice.
        `note_press` never raises and never blocks, so the recording below
        starts either way.
        """
        self.batch.note_press()
        self._holding_hotkey = True
        self._last_error = ""
        self._refresh_tray()
        return self.pipeline.start_utterance()

    def _warm_captions(self) -> None:
        """Load the Zipformer now, not on the first hotkey press."""
        session = self.streaming.start_session()
        session.close()

    def _spawn(self, target, name: str) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    def _caption_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                self.pipeline.pump_captions(timeout=0.2)
            except Exception:
                log.exception("caption pump failed; continuing")

    def _stop_request_loop(self) -> None:
        """Watch for `dictate stop`.

        It has to be a request rather than a kill: this process owns a
        whisper-server child holding ~1.6 GB of VRAM and the transcription port,
        and killing the parent would orphan it - the next start would then fail
        on a port that nothing appears to be using. So `dictate stop` leaves a
        file, and this brings the app down exactly the way Ctrl+C does.
        """
        while not self._stopping.wait(0.5):
            try:
                if instance.stop_requested(since=self._started_at):
                    self.console("\ndictate: stop requested; shutting down…")
                    instance.clear_stop_request()
                    self.overlay.close()  # ends run_forever, which triggers stop()
                    return
                # The same slow, read-only watch notices when the user has put
                # a deferred destination in front again. Delivery itself is
                # submitted to the one finalise worker and re-checks foreground
                # immediately before sending; this loop never types.
                deferred = getattr(self, "deferred", None)
                if deferred is not None:
                    deferred.poll()
                # The same tick keeps the tray honest about the two things it
                # cannot be told about: the model being released after an idle
                # spell, which nothing else in this process announces, and
                # `dictate autostart enable` typed in another window.
                if self.tray is not None:
                    self._read_autostart()
                self._refresh_tray()
            except Exception:
                log.exception("stop-request watch failed; continuing")

    def run(self) -> int:
        """Start everything, then run the overlay loop on the main thread."""
        try:
            self.start()
        except BaseException:
            # A failed start still has to put back whatever it managed to take:
            # a half-started whisper-server left running would hold the port and
            # 1.6 GB of VRAM, and the next `dictate run` would refuse to start.
            self.stop()
            raise
        self._install_signal_handlers()
        try:
            self.overlay.run_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()
        return EXIT_RESTART if self.restart_wanted else 0

    def _install_signal_handlers(self) -> None:
        def handler(_sig, _frame):
            self.console("\ndictate: shutting down…")
            self.overlay.close()  # ends run_forever, which triggers stop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass  # not the main thread, or not supported here

    def stop(self) -> None:
        if self._stopping.is_set():
            return
        self._stopping.set()
        self.overlay.set_state(OverlayState.HIDDEN)
        self._refresh_tray()

        # Order matters. Stop taking new input first, then let a transcription
        # that is already in flight finish and paste - that is the user's last
        # sentence, and it is worth a moment - and only then take the server
        # away. Killing the server first would strand that request.
        #
        # "pipeline" comes before "microphone worker" and "microphone" on
        # purpose: if he is mid-utterance when the stop arrives, `pipeline.
        # close()` is what ends the recording and dispatches the release onto
        # `_mic_pool` - draining that pool is what waits for it to actually
        # happen before the device is torn down, rather than the release
        # racing its own teardown.
        for step, fn in (
            ("hotkey listener", self.hotkey.stop),
            ("pipeline", self.pipeline.close),
            ("microphone worker", lambda: self._mic_pool.shutdown(
                wait=True, cancel_futures=True)),
            ("microphone", self.audio.close),
            ("deferred delivery", self.deferred.close),
            # cancel_futures drops work that has not started; the running job
            # is allowed to complete.
            ("transcription worker", lambda: self._pool.shutdown(
                wait=True, cancel_futures=True)),
            ("whisper-server", self.batch.stop),
            ("tray icon", self._close_tray),
            ("overlay", self.overlay.close),
        ):
            try:
                fn()
            except Exception:
                log.exception("error while stopping the %s", step)
        if self.streaming is not None:
            try:
                self.streaming.close()
            except Exception:
                log.debug("closing the caption model raised", exc_info=True)
        for thread in self._threads:
            thread.join(timeout=2.0)
        instance.clear_activity()
        self.console("dictate: stopped.")

    def _close_tray(self) -> None:
        tray, self.tray = self.tray, None
        if tray is not None:
            tray.close()


def run(cfg: Config, *, console=None, suggest_autostart: bool = False) -> int:
    """Build and run. Any `DictateError` raised here reaches `cli.main`, which
    prints its message and its remedy - never a traceback.

    `suggest_autostart` is what `dictate run` in a console passes and the logon
    task does not: only the copy he started by hand has anything to learn from
    being told it could have started itself.
    """
    return Application(cfg, console=console,
                       suggest_autostart=suggest_autostart).run()


def config_search_note(path: Path) -> str:
    return (f"No config file at {path}. Using built-in defaults - run "
            f"`dictate init` to write one you can edit.")
