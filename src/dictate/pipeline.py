"""The dictation state machine.

    hotkey down ──► capture the focused window handle  (NOT at paste time)
                    start the mic
                    start a caption session
                       │
    audio blocks ──────┼──► utterance buffer   (kept whole, for the GPU pass)
                       └──► caption queue      (disposable, display only)
                       │
    hotkey up   ──► the caption DECODER is closed here and its queued audio
                    invalidated: no new caption text can ever be produced
                    the words already on screen stay there, greyed, so he can
                    still see what he said while the GPU works
                    utterance handed to the GPU pass
                       ▼
                    clean  ──►  punctuate  ──►  paste into the captured window
                       ▼
                    the screen is cleared and says "pasted" - which is the
                    moment the words he was reading go

`clean` may only delete words; `punctuate` turns a spoken "comma" into ",". They
are separate stages in that order on purpose - see `_punctuate`.

**Constraint 4, and where it is enforced.** Caption text is display-only: it
must never reach his document. This module is what makes that structural rather
than a matter of timing - it never holds the caption text at all.
`pump_captions` reads a session's text and hands it straight to the overlay
without keeping a reference, and the release changes the overlay's state with
`KEEP` rather than with any text, because there is no text here to send. The
value the finalise worker is given is an `Utterance`, which carries audio; and
the string the injector is handed is what `batch.transcribe` produced, with the
cleanup and punctuation stages applied to it - there is no path by which a
caption session's text could become it.

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
from .platform.base import KEEP, CaptionOverlay, OverlayState, TargetWindow
from .punctuation.engine import PunctuationResult

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
    #: Audio callbacks in which the OS said it had thrown input audio away
    #: before dictate saw it. Non-zero means this recording has holes in it,
    #: and the pasted text is made from it.
    input_lost: int = 0
    #: Blocks the bounded caption queue discarded because the caption thread
    #: was behind. Affects the captions ONLY - the buffer above is untouched.
    captions_dropped: int = 0


Notify = Callable[[str, str], None]
Submit = Callable[[Callable[[], None]], None]
#: Called once per dictation that was actually delivered, with the text that
#: landed, the text Whisper produced before the cleanup rules ran, and how long
#: he spoke for. What is kept out of that, in what shape, and for how long is
#: `history.HistoryStore`'s business, not this module's.
Record = Callable[..., None]


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
        punctuator: Callable[[str], PunctuationResult] | None = None,
        sample_rate: int = 16000,
        block_ms: int = 32,
        min_utterance_ms: int = 350,
        max_utterance_s: float = 300.0,
        max_caption_chars: int = 220,
        streaming=None,
        submit: Submit | None = None,
        notify: Notify | None = None,
        record: Record | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.batch = batch
        self.cleaner = cleaner
        self.injector = injector
        self.windows = windows
        self.overlay = overlay
        #: Spoken punctuation, AFTER cleanup - see `_finalize`. None (the
        #: default, and what `[punctuation] enabled = false` produces) leaves
        #: the cleaned text exactly as it is.
        self.punctuator = punctuator
        self.streaming = streaming
        self.sample_rate = sample_rate
        #: Only ever used to turn "n blocks were thrown away" into a number of
        #: milliseconds he can judge - a count of blocks means nothing to him.
        self.block_ms = max(1, block_ms)
        self.min_utterance_ms = min_utterance_ms
        self.max_caption_chars = max_caption_chars
        self.submit: Submit = submit or (lambda fn: fn())
        self.notify: Notify = notify or (lambda level, msg: None)
        self.record: Record = record or (lambda text, **kwargs: None)
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
        #: Audio thrown away during the utterance in progress. Both are reset at
        #: press and read at release, so every number he is shown belongs to one
        #: dictation rather than to the session - "it has dropped 214 blocks
        #: since Tuesday" is not something anyone can act on.
        self._lost_now = 0
        self._dropped_now = 0
        #: The same two, for the life of the process, so `dictate doctor` and
        #: the log can say whether this is a habit or a one-off.
        self.input_lost = 0
        self.captions_dropped = 0

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
            self._lost_now = 0
            self._dropped_now = 0
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
        # The target goes with the state: it is what tells the overlay which
        # monitor to appear on, and the window it names is the one the text will
        # be pasted into, so the captions come up where he is already looking.
        self.overlay.set_state(OverlayState.LISTENING, "", self._target)
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

    def note_input_loss(self, blocks: int = 1) -> None:
        """The OS discarded input audio before dictate saw it.

        Called from the audio callback thread by whatever is capturing (see
        `platform.base.AudioLossCallback`), so it counts and does nothing else.
        Two counters, because they answer two different questions: the
        per-utterance one is what he is told at the release - this recording has
        a hole in it - and the process-wide one is what says whether it is
        happening while he is not dictating at all, which is a different fault
        with a different fix.
        """
        if blocks <= 0:
            return
        with self._lock:
            self.input_lost += blocks
            if self._recording:
                self._lost_now += blocks

    def _enqueue(self, item) -> None:
        try:
            self._captions.put_nowait(item)
        except queue.Full:
            # Drop the oldest caption block. The utterance buffer is untouched,
            # so the text that actually gets pasted is unaffected - but the
            # caption model now has a splice in what it hears, which is a
            # perfectly good reason for the words on screen to be wrong. It was
            # logged at DEBUG, which is to say never; it is counted now and said
            # out loud at the release.
            try:
                self._captions.get_nowait()
                self._captions.put_nowait(item)
            except (queue.Empty, queue.Full):
                return
            # Under the lock because the audio thread and the hotkey thread both
            # get here, and a lost increment is a lost hole in the evidence.
            with self._lock:
                self.captions_dropped += 1
                if self._recording:
                    self._dropped_now += 1

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
        # Every block, even when the words have not changed - roughly thirty
        # sends a second where the model emits a word every few hundred
        # milliseconds. Skipping the repeats was tried and taken out again: it
        # needs the last caption kept HERE to compare against, and this module
        # holding caption text is precisely what constraint 4 forbids.
        # `tests/test_pipeline.py::CaptionsCanNeverBePasted` failed on the
        # attempt, which is the guarantee doing its job. The overlay already
        # collapses everything that arrives inside one 30 ms tick into a single
        # redraw, so what is left to save is a queue put.
        self.overlay.set_state(OverlayState.LISTENING,
                               caption_tail(text, self.max_caption_chars))
        return True

    # -- what was thrown away --------------------------------------------

    def _report_losses(self, lost: int, dropped: int) -> None:
        """Say what this utterance lost, at the moment it was lost in.

        Two different faults with two different consequences, so they are two
        different sentences and never one:

        * `lost` is audio the OS discarded before dictate saw it. It is missing
          from the recording Whisper is about to transcribe, so the PASTED TEXT
          may be missing words. That is worth interrupting him for.
        * `dropped` is caption blocks this module threw away because the caption
          thread was behind. The utterance buffer is untouched, so the pasted
          text is exactly what it would have been; only the words on screen saw
          a splice. Worth saying, because it is the answer to "why were the
          captions wrong", and worth saying it does not affect the text.
        """
        if lost:
            ms = lost * self.block_ms
            log.warning("input overflow: %d blocks (~%d ms) discarded by the "
                        "OS during that utterance", lost, ms)
            self.notify(
                "warning",
                f"Windows threw away about {ms} ms of that recording before "
                f"dictate saw it, so the pasted text may be missing a word. If "
                f"it keeps happening, close what else is using the microphone, "
                f"or raise [audio] block_ms (32 to 64) in your config.",
            )
        if dropped:
            ms = dropped * self.block_ms
            log.warning("caption queue dropped %d blocks (~%d ms) during that "
                        "utterance", dropped, ms)
            self.notify(
                "info",
                f"Live captions fell behind and skipped about {ms} ms of audio, "
                f"so the words on screen were worse than usual. What was pasted "
                f"is unaffected - it is made from the whole recording.",
            )

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
            lost = self._lost_now
            dropped = self._dropped_now
            self._lost_now = 0
            self._dropped_now = 0
            self.buffer.reset()
            duration = len(pcm) / 2 / self.sample_rate
            self._pending += 1

        # The words stay on screen and the panel says "thinking": he can still
        # read what he said while the GPU works, which is the moment he would
        # otherwise be watching an empty slab and wondering.
        #
        # KEEP, not the text - this module does not have the text. The decoder
        # that produced it has just been detached and is closed below without
        # being read again, and every queued caption block was invalidated by
        # the `_uid` bump, so nothing can add a word to the panel from here on.
        self.overlay.set_state(OverlayState.THINKING, KEEP)
        if stale is not None:
            self._enqueue((_CLOSE, stale))

        if duration * 1000 < self.min_utterance_ms:
            log.info("utterance was %.0f ms, below the %d ms floor; discarding",
                     duration * 1000, self.min_utterance_ms)
            with self._lock:
                self._pending -= 1
            self.overlay.set_state(OverlayState.HIDDEN, "")
            return True

        self._report_losses(lost, dropped)
        utt = Utterance(pcm=pcm, sample_rate=self.sample_rate, target=target,
                        duration_s=duration, overflowed=overflowed,
                        input_lost=lost, captions_dropped=dropped)
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
            final = self._punctuate(result.text).strip()
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
            # An empty string, never KEEP: the caption goes at exactly the
            # moment the real text lands in his document. Leaving it up under
            # the word "pasted" is the one arrangement in which he could take
            # the caption for what was pasted.
            self.overlay.set_state(OverlayState.DONE, "")
            self._remember(final, raw=text, utt=utt)
        except DictateError as exc:
            log.error("%s", exc.report())
            self._fail(exc.report())
        except Exception as exc:  # never let a worker thread die silently
            log.exception("unexpected failure while finishing an utterance")
            self._fail(f"Something went wrong finishing that dictation: {exc}")
        finally:
            with self._lock:
                self._pending = max(0, self._pending - 1)

    def _punctuate(self, text: str) -> str:
        """Spoken punctuation, run AFTER the cleanup pass and never inside it.

        The order is deliberate and it is the only order that leaves the cleanup
        guarantee where it was: cleanup receives, byte for byte, the text
        Whisper produced, exactly as it did before this stage existed, and its
        subsequence check is evaluated over that same text. Running punctuation
        first would also have fed cleanup's filler rules a comma they were
        written to eat - `um,` is removed WITH its comma, so "hello um comma
        world" would have lost the comma this stage had just put in.

        A failure here must never cost him the dictation: the cleaned text is
        what gets pasted if anything goes wrong.
        """
        if self.punctuator is None:
            return text
        try:
            result = self.punctuator(text)
        except Exception:
            log.exception("spoken punctuation failed; pasting the text without it")
            return text
        if result.rejected_reason:
            log.warning("spoken punctuation discarded: %s", result.rejected_reason)
            self.notify("warning", "Spoken punctuation was skipped for that one - "
                                   + result.rejected_reason + ".")
        return result.text

    def _remember(self, final: str, *, raw: str, utt: Utterance) -> None:
        """Hand the finished dictation to whoever is keeping the record.

        Called only after the text has actually been delivered, so every line in
        the history is text that landed somewhere. It is guarded here rather
        than trusted to the callback: a history that cannot be written must
        never turn a dictation that worked into a reported failure.
        """
        try:
            self.record(final, raw=raw, spoke_s=utt.duration_s)
        except Exception:
            log.exception("the dictation history could not be written; the text "
                          "was pasted and nothing else is affected")

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
