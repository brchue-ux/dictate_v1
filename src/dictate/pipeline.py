"""The dictation state machine.

    hotkey down ──► capture the focused window handle  (NOT at paste time)
                    start the mic
                    start a caption session
                       │
    audio blocks ──────┼──► utterance buffer   (kept whole, for the GPU pass)
                       └──► caption queue      (disposable, display only)
                       │
    hotkey up   ──► caption text discarded HERE, on screen and in memory
                    utterance handed to the GPU pass
                       ▼
                    clean  ──►  paste into the captured window

Everything in this module is plain Python: no Windows, no audio library, no
model. The platform pieces and the two transcription backends arrive as
constructor arguments, which is what makes the whole flow testable on a Linux
box with no microphone and no GPU - see tests/test_pipeline.py.

Threading contract:

* `start_utterance` / `finish_utterance` are called from the hotkey thread.
* `push_audio` is called from the audio callback thread and must stay cheap:
  it appends to a buffer and enqueues a copy. It never runs a model.
* `pump_captions` is called from one dedicated caption thread (or directly, in
  tests). It is the ONLY thread that ever touches a streaming session.
* The final transcribe/clean/paste runs on whatever `submit` provides.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from .audio.buffer import UtteranceBuffer
from .cleanup.engine import CleanResult
from .errors import DictateError
from .platform.base import CaptionOverlay, OverlayState, TargetWindow

log = logging.getLogger(__name__)

#: Caption audio blocks allowed to queue up before we start dropping the oldest.
#: Captions are disposable, so dropping them is strictly better than blocking the
#: audio callback or growing memory. The utterance buffer is never dropped.
CAPTION_QUEUE_BLOCKS = 64

_CLOSE = object()


class PipelineState(Enum):
    IDLE = "idle"
    RECORDING = "recording"
    FINALIZING = "finalizing"


@dataclass
class Utterance:
    pcm: bytes
    sample_rate: int
    target: TargetWindow | None
    duration_s: float
    overflowed: bool = False


Notify = Callable[[str, str], None]
Submit = Callable[[Callable[[], None]], None]


def caption_tail(text: str, max_chars: int) -> str:
    """The last `max_chars` of caption text, cut at a word boundary.

    The overlay is one line: as the user keeps talking, old words scroll off the
    left rather than the window growing across the screen.
    """
    if len(text) <= max_chars:
        return text
    cut = text[-max_chars:]
    space = cut.find(" ")
    if 0 <= space < 40:
        cut = cut[space + 1:]
    return "… " + cut


class Pipeline:
    def __init__(
        self,
        *,
        batch,
        cleaner: Callable[[str], CleanResult],
        injector,
        windows,
        overlay: CaptionOverlay,
        sample_rate: int = 16000,
        min_utterance_ms: int = 350,
        max_utterance_s: float = 300.0,
        max_caption_chars: int = 220,
        streaming=None,
        submit: Submit | None = None,
        notify: Notify | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.batch = batch
        self.cleaner = cleaner
        self.injector = injector
        self.windows = windows
        self.overlay = overlay
        self.streaming = streaming
        self.sample_rate = sample_rate
        self.min_utterance_ms = min_utterance_ms
        self.max_caption_chars = max_caption_chars
        self.submit: Submit = submit or (lambda fn: fn())
        self.notify: Notify = notify or (lambda level, msg: None)
        self.clock = clock

        self.buffer = UtteranceBuffer(sample_rate, max_utterance_s)
        self._lock = threading.RLock()
        self._recording = False
        self._pending = 0
        self._uid = 0
        self._session = None
        self._target: TargetWindow | None = None
        self._started_at = 0.0
        self._captions: queue.Queue = queue.Queue(maxsize=CAPTION_QUEUE_BLOCKS)
        self._closed = False

        #: Set by tests and by `dictate run --once`; counts completed utterances.
        self.completed = 0

    # -- state -----------------------------------------------------------

    @property
    def state(self) -> PipelineState:
        with self._lock:
            if self._recording:
                return PipelineState.RECORDING
            return PipelineState.FINALIZING if self._pending else PipelineState.IDLE

    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._recording

    # -- hotkey down -----------------------------------------------------

    def start_utterance(self) -> bool:
        """Hotkey pressed. Returns False if we were already recording."""
        with self._lock:
            if self._closed or self._recording:
                return False
            self._recording = True
            self._uid += 1
            self._target = self._capture_target()
            self.buffer.reset()
            self._drain_queue()
            self._started_at = self.clock()
            if self.streaming is not None:
                try:
                    self._session = self.streaming.start_session()
                except DictateError as exc:
                    self._session = None
                    log.warning("live captions unavailable: %s", exc.message)
                    self.notify("warning", exc.report())
                except Exception:
                    self._session = None
                    log.exception("live captions failed to start")
        self.overlay.set_state(OverlayState.LISTENING, "")
        log.info("recording started, target = %s", self._target or "unknown")
        return True

    def _capture_target(self) -> TargetWindow | None:
        """Read the focused window NOW, before the overlay appears."""
        try:
            return self.windows.foreground()
        except Exception:
            log.exception("could not read the focused window")
            return None

    # -- audio -----------------------------------------------------------

    def push_audio(self, pcm: bytes) -> None:
        """Called from the audio callback. Keep this cheap."""
        if not pcm:
            return
        with self._lock:
            if not self._recording:
                return
            fitted = self.buffer.append(pcm)
            uid = self._uid
            want_captions = self._session is not None
        if want_captions:
            self._enqueue((uid, pcm))
        if not fitted:
            log.warning("utterance hit the %.0fs ceiling; finishing it",
                        self.buffer.max_seconds)
            self.notify(
                "warning",
                f"That was longer than the {self.buffer.max_seconds:.0f} second "
                f"limit, so dictate stopped recording and is transcribing what "
                f"it has.",
            )
            self.finish_utterance()

    def _enqueue(self, item) -> None:
        try:
            self._captions.put_nowait(item)
        except queue.Full:
            # Drop the oldest caption block. The utterance buffer is untouched,
            # so the text that actually gets pasted is unaffected.
            try:
                self._captions.get_nowait()
                self._captions.put_nowait(item)
                log.debug("caption queue full; dropped a block")
            except (queue.Empty, queue.Full):
                pass

    def _drain_queue(self) -> None:
        while True:
            try:
                item = self._captions.get_nowait()
            except queue.Empty:
                return
            if isinstance(item, tuple) and item and item[0] is _CLOSE:
                self._close_session(item[1])

    # -- caption pump ----------------------------------------------------

    def pump_captions(self, timeout: float = 0.2) -> bool:
        """Process one queued caption block. The ONLY thread that touches a
        streaming session. Returns False if nothing was waiting."""
        try:
            item = self._captions.get(timeout=timeout)
        except queue.Empty:
            return False
        if isinstance(item, tuple) and item and item[0] is _CLOSE:
            self._close_session(item[1])
            return True
        uid, pcm = item
        with self._lock:
            session = self._session
            current = self._uid
            recording = self._recording
        if session is None or uid != current or not recording:
            return True  # audio from an utterance that is already over
        try:
            session.accept(pcm)
            text = session.text()
        except Exception:
            log.exception("live caption decoding failed; captions off for this utterance")
            with self._lock:
                stale, self._session = self._session, None
            self._close_session(stale)  # we are the pump thread, so close it here
            self.overlay.set_state(
                OverlayState.LISTENING,
                "(live captions stopped - still recording, your text is unaffected)",
            )
            return True
        with self._lock:
            if uid != self._uid or not self._recording:
                return True
        self.overlay.set_state(OverlayState.LISTENING,
                               caption_tail(text, self.max_caption_chars))
        return True

    def _close_session(self, session) -> None:
        if session is None:
            return
        try:
            session.close()
        except Exception:
            log.debug("closing a caption session raised", exc_info=True)

    # -- hotkey up -------------------------------------------------------

    def finish_utterance(self) -> bool:
        """Hotkey released. Returns False if we were not recording."""
        with self._lock:
            if not self._recording:
                return False
            self._recording = False
            self._uid += 1  # invalidates every queued and in-flight caption block
            stale, self._session = self._session, None
            pcm = self.buffer.pcm()
            overflowed = self.buffer.overflowed
            target = self._target
            self.buffer.reset()
            duration = len(pcm) / 2 / self.sample_rate
            self._pending += 1

        # The caption text stops existing here: cleared from the screen, and the
        # decoder that produced it is closed without its text being read again.
        self.overlay.set_state(OverlayState.THINKING, "")
        if stale is not None:
            self._enqueue((_CLOSE, stale))

        if duration * 1000 < self.min_utterance_ms:
            log.info("utterance was %.0f ms, below the %d ms floor; discarding",
                     duration * 1000, self.min_utterance_ms)
            with self._lock:
                self._pending -= 1
            self.overlay.set_state(OverlayState.HIDDEN, "")
            return True

        utt = Utterance(pcm=pcm, sample_rate=self.sample_rate, target=target,
                        duration_s=duration, overflowed=overflowed)
        try:
            self.submit(lambda: self._finalize(utt))
        except Exception:
            with self._lock:
                self._pending -= 1
            log.exception("could not schedule the transcription")
            self._fail("dictate could not start transcribing that. "
                       "The text was not pasted.")
        return True

    def cancel_utterance(self, reason: str = "") -> bool:
        """Abandon the recording in progress. Nothing is transcribed or pasted."""
        with self._lock:
            if not self._recording:
                return False
            self._recording = False
            self._uid += 1
            stale, self._session = self._session, None
            self.buffer.reset()
        if stale is not None:
            self._enqueue((_CLOSE, stale))
        self.overlay.set_state(OverlayState.HIDDEN, "")
        log.info("recording cancelled%s", f": {reason}" if reason else "")
        if reason:
            self.notify("info", reason)
        return True

    # -- the batch pass --------------------------------------------------

    def _finalize(self, utt: Utterance) -> None:
        t0 = self.clock()
        try:
            text = self.batch.transcribe(utt.pcm, utt.sample_rate)
            result = self.cleaner(text)
            if result.rejected_reason:
                log.warning("cleanup discarded: %s", result.rejected_reason)
                self.notify("warning", "Cleanup was skipped for that one - "
                                       + result.rejected_reason + ".")
            final = result.text.strip()
            if not final:
                log.info("transcription came back empty")
                self.overlay.set_state(OverlayState.DONE, "")
                self.notify("info", "dictate did not hear any words in that, so "
                                    "nothing was pasted.")
                return
            self.injector.send(final, utt.target)
            self.completed += 1
            log.info("delivered %d chars to %s in %.2fs",
                     len(final), utt.target or "the focused window", self.clock() - t0)
            self.overlay.set_state(OverlayState.DONE, "")
        except DictateError as exc:
            log.error("%s", exc.report())
            self._fail(exc.report())
        except Exception as exc:  # never let a worker thread die silently
            log.exception("unexpected failure while finishing an utterance")
            self._fail(f"Something went wrong finishing that dictation: {exc}")
        finally:
            with self._lock:
                self._pending = max(0, self._pending - 1)

    def _fail(self, message: str) -> None:
        self.overlay.set_state(OverlayState.ERROR, message.splitlines()[0])
        self.notify("error", message)

    # -- shutdown --------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._recording = False
            stale, self._session = self._session, None
        self._close_session(stale)
        self._drain_queue()
