"""Wiring: the one place where the platform layer, the two engines and the
pipeline are assembled into a running application.

Thread layout:

    main thread      Tk overlay message loop. Owns every Tk call.
    hotkey thread    owned by the keyboard hook; calls start/finish.
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

from . import instance, tray as tray_mod
from .cleanup.service import CleanupService
from .config import Config
from .engines.residency import ResidentModel, Residency
from .engines.sherpa_stream import SherpaStreamingTranscriber
from .engines.whisper_backend import WhisperVulkanBackend
from .errors import DictateError
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


class Application:
    def __init__(self, cfg: Config, *, console=None) -> None:
        self.cfg = cfg
        self.console = console or (lambda msg: print(msg, file=sys.stderr, flush=True))
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
        self.hotkey = factory.make_hotkey_listener(cfg)

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
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dictate-finalize")
        #: Transcriptions queued or running, so the tray can say "transcribing"
        #: for exactly as long as that is true and not a moment longer.
        self._in_flight = 0
        self._holding_hotkey = False
        self._last_error = ""
        #: The last thing published for `dictate update` to read; `None` until
        #: something has been, so the first idle state is written too.
        self._published_activity: str | None = None

        self.pipeline = Pipeline(
            batch=self.batch,
            cleaner=self.cleaner,
            injector=self.injector,
            windows=self.tracker,
            overlay=self.overlay,
            punctuator=self.punctuator,
            streaming=self.streaming,
            sample_rate=cfg.audio.sample_rate,
            min_utterance_ms=cfg.audio.min_utterance_ms,
            max_utterance_s=cfg.audio.max_utterance_s,
            max_caption_chars=cfg.overlay.max_chars,
            submit=self._submit,
            notify=self.notify,
        )

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

        self.audio.start(self.pipeline.push_audio)
        self.console(f"dictate: microphone     {self.audio.describe}")
        self.console(f"dictate: paste method   {self.injector.describe}")

        self._spawn(self._caption_loop, "dictate-captions")
        self._spawn(self._stop_request_loop, "dictate-stop-watch")
        self.hotkey.register(self._on_hotkey_press, self._on_hotkey_release)
        self.hotkey.start()
        self.console(f"dictate: hotkey         {self.hotkey.describe}")
        self._start_tray()
        self.console("dictate: ready. Hold the hotkey and speak. Ctrl+C here to quit,")
        self.console("dictate: or `dictate stop` from any other window.")

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
                                  model_resident=resident, detail=self._last_error)

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
        """The tray's third item. The folder, not the file: the log may not
        exist yet, and what he wants is somewhere to look."""
        target = Path(self.cfg.logging.file).parent if self.cfg.logging.file \
            else instance.state_dir()
        try:
            os.startfile(str(target))  # noqa: S606 - Windows only, by design
        except Exception:
            log.exception("could not open %s", target)
            self.notify("warning", f"dictate could not open {target}. Open it "
                                   f"yourself - the log is in there.")

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
                # The same tick keeps the tray honest about the one thing it
                # cannot be told about: the model being released after an idle
                # spell, which nothing else in this process announces.
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
        for step, fn in (
            ("hotkey listener", self.hotkey.stop),
            ("microphone", self.audio.close),
            ("pipeline", self.pipeline.close),
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


def run(cfg: Config, *, console=None) -> int:
    """Build and run. Any `DictateError` raised here reaches `cli.main`, which
    prints its message and its remedy - never a traceback."""
    return Application(cfg, console=console).run()


def config_search_note(path: Path) -> str:
    return (f"No config file at {path}. Using built-in defaults - run "
            f"`dictate init` to write one you can edit.")
