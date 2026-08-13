"""Wiring: the one place where the platform layer, the two engines and the
pipeline are assembled into a running application.

Thread layout:

    main thread      Tk overlay message loop. Owns every Tk call.
    hotkey thread    owned by the keyboard hook; calls start/finish.
    audio thread     owned by PortAudio; calls push_audio and nothing else.
    caption thread   the only thread that touches a streaming session.
    finalize worker  ONE worker, so that if two utterances finish close
                     together their text is pasted in the order it was spoken.

Startup order matters and is deliberate: the whisper server is brought up and
warmed *before* the hotkey is registered, so the very first press already has a
resident model behind it and nobody discovers a missing model file mid-sentence.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .cleanup.service import CleanupService
from .config import Config
from .engines.sherpa_stream import SherpaStreamingTranscriber
from .engines.whisper_backend import WhisperVulkanBackend
from .errors import DictateError
from .pipeline import Pipeline
from .platform import factory
from .platform.base import OverlayState

log = logging.getLogger(__name__)


class Application:
    def __init__(self, cfg: Config, *, console=None) -> None:
        self.cfg = cfg
        self.console = console or (lambda msg: print(msg, file=sys.stderr, flush=True))
        self._stopping = threading.Event()
        self._threads: list[threading.Thread] = []

        self.overlay = factory.make_overlay(cfg, notify=self.notify)
        self.tracker = factory.make_window_tracker()
        self.injector = factory.make_injector(cfg, self.tracker)
        self.audio = factory.make_audio_capture(cfg)
        self.hotkey = factory.make_hotkey_listener(cfg)

        self.batch = WhisperVulkanBackend(cfg.whisper, resolve=cfg.resolve)
        self.cleaner = CleanupService(
            cfg.resolve(cfg.cleanup.rules_file) if cfg.cleanup.enabled else None,
            enabled=cfg.cleanup.enabled,
            notify=self.notify,
        )
        self.streaming = (
            SherpaStreamingTranscriber.from_config(cfg) if cfg.captions.enabled else None
        )
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dictate-finalize")

        self.pipeline = Pipeline(
            batch=self.batch,
            cleaner=self.cleaner,
            injector=self.injector,
            windows=self.tracker,
            overlay=self.overlay,
            streaming=self.streaming,
            sample_rate=cfg.audio.sample_rate,
            min_utterance_ms=cfg.audio.min_utterance_ms,
            max_utterance_s=cfg.audio.max_utterance_s,
            max_caption_chars=cfg.overlay.max_chars,
            submit=self._pool.submit,
            notify=self.notify,
        )

    # -- user-facing messages -------------------------------------------

    def notify(self, level: str, message: str) -> None:
        prefix = {"error": "!!", "warning": " !", "info": "  "}.get(level, "  ")
        self.console(f"{prefix} {message}")
        getattr(log, "error" if level == "error" else "info")("%s", message)

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        self.console(f"dictate: cleanup rules  {self.cfg.cleanup.rules_file}"
                     if self.cfg.cleanup.enabled else "dictate: cleanup disabled")
        self.cleaner.load()

        self.console("dictate: starting whisper-server (the model stays loaded "
                     "from here on)…")
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
        self.hotkey.register(self.pipeline.start_utterance, self.pipeline.finish_utterance)
        self.hotkey.start()
        self.console(f"dictate: hotkey         {self.hotkey.describe}")
        self.console("dictate: ready. Hold the hotkey and speak. Ctrl+C here to quit.")

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
        return 0

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
        self.console("dictate: stopped.")


def run(cfg: Config, *, console=None) -> int:
    """Build and run. Any `DictateError` raised here reaches `cli.main`, which
    prints its message and its remedy - never a traceback."""
    return Application(cfg, console=console).run()


def config_search_note(path: Path) -> str:
    return (f"No config file at {path}. Using built-in defaults - run "
            f"`dictate init` to write one you can edit.")
