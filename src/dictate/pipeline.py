"""The dictation state machine.

    hotkey down ──► capture the focused window handle  (NOT at paste time)
                    start the mic
                    start a caption session
                       │
    audio blocks ──────┼──► utterance buffer   (kept whole, for the GPU pass)
                       └──► caption queue      (disposable, display only)
                       │
    hotkey up   ──► the mic is released FIRST, before anything else below
                    can fail - see the guarantee note
                    the caption DECODER is closed here and its queued audio
                    invalidated: no new caption text can ever be produced
                    the words already on screen stay there, greyed, so he can
                    still see what he said while the GPU works
                    utterance handed to the GPU pass
                       ▼
                    clean  ──►  punctuate  ──►  is the captured window still
                       │                        the one in front? (delivery.py)
                       ├── yes ──►  paste into it
                       │            the screen is cleared and says "pasted" -
                       │            which is the moment the words he was
                       │            reading go
                       └── no  ──►  default: paste NOWHERE; keep the words
                                    restore mode: wait until the captured
                                    window is foreground again, then use the
                                    ordinary paste without raising it
                                    Either way, nothing is ever typed into a
                                    window he did not dictate into.

`clean` may only delete words; `punctuate` turns a spoken "comma" into ",". They
are separate stages in that order on purpose - see `_punctuate`.

**The mic is released on every exit from a recording, not only the one where
he presses the hotkey again.** `finish_utterance`, `cancel_utterance` and
`close` each stop it as the very first thing they do once `_recording` goes
back to `False` - before the caption session is closed, before anything is
queued for transcription, before any of it has a chance to raise. It is a
plain statement, never inside a `try` that could skip it, so nothing later in
those methods can leave the capture stream running. `start_utterance` is the
only place that starts it, guarded the same way `begin_deferred` already is:
a mic that fails to start is logged and notified, never something that stops
the recording from being marked as begun, because leaving `_recording` stuck
`True` from a half-finished press would itself become a way to leak the mic
forever. The actual `AudioCapture.start`/`.stop()` calls are dispatched
through `mic_submit` rather than called inline, because `finish_utterance` can
run on the audio callback's own thread (the `max_utterance_s` ceiling in
`push_audio`), and PortAudio forbids stopping a stream from inside its own
callback. `tests/test_pipeline.py::TheMicIsReleased` holds this across every
path out of a recording - normal stop, cancel, delivery failure, target
closure, a second dictation starting, and `close()` mid-utterance.

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

from . import deferred as deferred_mod, delivery
from .audio.buffer import UtteranceBuffer
from .cleanup.engine import CleanResult
from .errors import DictateError, InjectionError
from .platform.base import KEEP, AudioCapture, CaptionOverlay, OverlayState, TargetWindow
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
    #: Which automatic-wait generation owned this press. A later hotkey press
    #: invalidates it even if this utterance is still in the finalise worker.
    delivery_generation: int = 0
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
#: Called once per dictation that produced words, with the text, the text
#: Whisper produced before the cleanup rules ran, how long he spoke for, how
#: many Return keypresses delivering it involved, and whether it was delivered
#: at all - a dictation held because he had moved to another window is recorded
#: too, marked, because that record is how he gets it back. Returns whether it
#: was written. What is kept out of it, in what shape, and for how long is
#: `history.HistoryStore`'s business, not this module's.
Record = Callable[..., bool]


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
        on_focus_change: str = delivery.HOLD_MODE,
        restore_focus: bool = True,
        hold_to_clipboard: bool = True,
        begin_deferred: Callable[[], int] | None = None,
        defer_delivery: Callable[[deferred_mod.Request], None] | None = None,
        streaming=None,
        submit: Submit | None = None,
        notify: Notify | None = None,
        record: Record | None = None,
        clock: Callable[[], float] = time.monotonic,
        audio: AudioCapture | None = None,
        mic_submit: Submit | None = None,
    ) -> None:
        self.batch = batch
        self.cleaner = cleaner
        self.injector = injector
        self.windows = windows
        self.overlay = overlay
        #: The microphone. `None` is a legitimate embedding (a caller that
        #: does not want this module managing capture at all - most tests);
        #: whenever it is supplied, `start_utterance`/`finish_utterance` etc.
        #: own its lifetime completely - see the module docstring's guarantee.
        self.audio = audio
        #: Where an `AudioCapture.start`/`.stop()` call actually runs. Never
        #: called inline - see the guarantee note above for why. Defaults to
        #: running it on the calling thread, which is only safe for tests and
        #: embeddings with no real audio callback thread to collide with;
        #: `app.Application` gives this a dedicated worker.
        self.mic_submit: Submit = mic_submit or (lambda fn: fn())
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
        #: What to do when the window he pressed the hotkey in is not the one in
        #: front when the words are ready. The decision itself is `delivery.py`;
        #: these three are the settings it is made from.
        self.on_focus_change = on_focus_change
        self.restore_focus = restore_focus
        self.hold_to_clipboard = hold_to_clipboard
        #: Finished text is never retained on this Pipeline. Restore mode hands
        #: it to the separate deferred-delivery owner; the integer generation
        #: is all this state machine keeps between press and release.
        self.begin_deferred = begin_deferred or (lambda: 0)
        self.defer_delivery = defer_delivery
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
        self._delivery_generation = 0
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
            try:
                self._delivery_generation = int(self.begin_deferred())
            except Exception:
                # The automatic route is a convenience above the clipboard and
                # history floor. Its book-keeping may not stop a recording.
                log.exception("could not begin a deferred-delivery generation")
                self._delivery_generation = 0
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
        self._audio_start()
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

    # -- the mic itself ----------------------------------------------------
    #
    # Both of these are called outside `self._lock` - matching every other
    # platform call this class makes - and neither may ever raise: a mic that
    # will not start is a degraded recording, reported and notified, never a
    # reason to leave `_recording` stuck; a mic that will not stop is reported
    # and dropped, because raising here would skip whatever runs after it in
    # the caller, which is exactly the leak this pair exists to prevent.

    def _audio_start(self) -> None:
        if self.audio is None:
            return
        try:
            self.mic_submit(lambda: self.audio.start(self.push_audio, self.note_input_loss))
        except Exception:
            log.exception("could not start the microphone")
            self.notify("warning", "dictate could not start the microphone for "
                                   "that recording.")

    def _audio_stop(self) -> None:
        if self.audio is None:
            return
        try:
            self.mic_submit(self.audio.stop)
        except Exception:
            log.exception("could not stop the microphone")

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
            delivery_generation = self._delivery_generation
            lost = self._lost_now
            dropped = self._dropped_now
            self._lost_now = 0
            self._dropped_now = 0
            self.buffer.reset()
            duration = len(pcm) / 2 / self.sample_rate
            self._pending += 1

        # First, unconditionally, before anything below gets a chance to
        # raise: this is the guarantee the module docstring names. Whatever
        # happens to the rest of this method - a transcription that can never
        # be scheduled, a history that will not write - the mic is already
        # off by the time it happens.
        self._audio_stop()

        # Said at every release, with how long the key was actually held. It is
        # one line and it is the only record of an utterance that ended when he
        # did not mean it to - a chord half-released ends the recording, by
        # design, and afterwards there is otherwise nothing to look at but a
        # sentence that stops early.
        log.info("recording stopped after %.1fs of audio", duration)

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
                        duration_s=duration,
                        delivery_generation=delivery_generation,
                        overflowed=overflowed,
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
        self._audio_stop()
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
            # Where the text is allowed to go. Decided here, from two window
            # handles, and never inside the injector: it is a product decision
            # about his text and it has to be testable without Windows.
            focused = self._focused_now()
            decision = self._decide(utt.target, focused)
            log.info("delivery: %s (%s)", decision.action, decision.why)
            if decision.waits:
                on_clipboard = self._to_clipboard(final)
                if self.defer_delivery is not None and utt.target is not None:
                    try:
                        self.defer_delivery(deferred_mod.Request(
                            generation=utt.delivery_generation,
                            text=final,
                            raw=text,
                            spoke_s=utt.duration_s,
                            target=utt.target,
                            focused=focused,
                            on_clipboard=on_clipboard,
                        ))
                        return
                    except Exception as exc:
                        log.exception("could not start waiting for the captured "
                                      "window")
                        detail = ("dictate could not start its background wait: "
                                  f"{type(exc).__name__}: {exc}")
                else:
                    detail = ("dictate has no delivery watcher for the captured "
                              "window in this run.")
                self._hold(
                    final, raw=text, utt=utt,
                    decision=delivery.Decision(
                        delivery.HOLD, detail, delivery.REFUSED),
                    focused=focused, detail=detail,
                    on_clipboard=on_clipboard, route="deferred")
                return
            if not decision.pastes:
                self._hold(final, raw=text, utt=utt, decision=decision,
                           focused=focused)
                return
            try:
                returns = self.injector.send(final, utt.target) or 0
            except InjectionError as exc:
                # Nothing was pasted (or only part of it was, which the error
                # says). Before this, the text died here and the only remedy on
                # offer was to say the whole sentence again.
                self._hold(final, raw=text, utt=utt,
                           decision=delivery.Decision(
                               delivery.HOLD, exc.message, delivery.REFUSED),
                           focused=focused, detail=exc.report(),
                           partial=getattr(exc, "partial", False),
                           route="foreground")
                return
            self.completed += 1
            log.info("delivery route foreground: succeeded; delivered %d chars "
                     "to %s in %.2fs",
                     len(final), utt.target or "the focused window", self.clock() - t0)
            # An empty string, never KEEP: the caption goes at exactly the
            # moment the real text lands in his document. Leaving it up under
            # the word "pasted" is the one arrangement in which he could take
            # the caption for what was pasted.
            self.overlay.set_state(OverlayState.DONE, "")
            self._remember(final, raw=text, utt=utt, returns=returns)
        except DictateError as exc:
            log.error("%s", exc.report())
            self._fail(exc.report())
        except Exception as exc:  # never let a worker thread die silently
            log.exception("unexpected failure while finishing an utterance")
            self._fail(f"Something went wrong finishing that dictation: {exc}")
        finally:
            with self._lock:
                self._pending = max(0, self._pending - 1)

    # -- where the text is allowed to go ----------------------------------

    def _focused_now(self) -> TargetWindow | None:
        """The window in front at the moment the text is ready.

        The counterpart to `_capture_target`, and the only other time dictate
        asks. It is read once, here, so the decision below and the message he is
        shown are about the same instant - asking twice would let them disagree.
        """
        try:
            return self.windows.foreground()
        except Exception:
            log.exception("could not read which window is in front now")
            return None

    def _decide(self, target: TargetWindow | None,
                focused: TargetWindow | None) -> delivery.Decision:
        """Ask `delivery` what to do. Nothing is decided in here."""
        exists: bool | None = None
        if target is not None and focused is not None \
                and not delivery.same_window(target, focused):
            # Only asked when it can change what he is told - "it has closed" is
            # a different sentence from "you moved". Never asked on the ordinary
            # path, which is one Win32 call that used not to happen at all.
            try:
                exists = bool(self.windows.exists(target))
            except Exception:
                log.debug("could not ask whether the captured window still exists",
                          exc_info=True)
        return delivery.decide(target, focused, target_exists=exists,
                               mode=self.on_focus_change,
                               restore_focus=self.restore_focus)

    def _hold(self, final: str, *, raw: str, utt: Utterance,
              decision: delivery.Decision, focused: TargetWindow | None,
              detail: str = "", partial: bool = False,
              on_clipboard: bool | None = None, route: str = "hold") -> None:
        """Nothing was pasted. Keep his words and tell him where they are.

        The whole of the difference between refusing and losing. Two places, and
        they answer two different questions: the clipboard is "put it where I
        meant it to go, now", one keystroke away and gone the next time he
        copies anything; the dictation history is "what did I say", durable and
        readable an hour later. Neither may raise - a copy that could not be
        kept must still be reported, and reported as not kept.

        Note what is NOT done here: the text is not held on `self`. Nothing in
        this module keeps text between utterances, which is the shape constraint
        4 is enforced by (`CaptionsCanNeverBePasted`), and a "last dictation"
        field would be the first exception to it.
        """
        if on_clipboard is None:
            on_clipboard = self._to_clipboard(final)
        in_history = self._remember(final, raw=raw, utt=utt, delivered=False)
        message = delivery.held_message(
            decision.reason or delivery.REFUSED,
            target=utt.target, focused=focused, on_clipboard=on_clipboard,
            in_history=in_history, partial=partial, detail=detail)
        if decision.reason == delivery.REFUSED:
            log.warning("delivery route %s: failed; text held (%s)",
                        route, decision.why)
        else:
            log.info("delivery route %s: succeeded; pasted nowhere (%s)",
                     route, decision.why)
        self._fail(message)

    def _to_clipboard(self, final: str) -> bool:
        """The single door from undelivered text to the clipboard."""
        if not self.hold_to_clipboard:
            return False
        try:
            return bool(self.injector.to_clipboard(final))
        except Exception:
            log.exception("could not put the held text on the clipboard")
            return False

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

    def _remember(self, final: str, *, raw: str, utt: Utterance,
                  returns: int = 0, delivered: bool = True) -> bool:
        """Hand the finished dictation to whoever is keeping the record.

        Called once a dictation has an outcome - it landed somewhere, or it was
        held because there was nowhere it was allowed to land. `delivered` says
        which, and the history writes it down: a line that says it was not
        pasted is the durable half of "the text is never silently lost", and it
        is why the file's own rule is now "every line is text he said" rather
        than "text that landed somewhere". A transcription that failed is still
        not recorded - there are no words to keep.

        It is guarded here rather than trusted to the callback: a history that
        cannot be written must never turn a dictation that worked into a
        reported failure. Returns whether it was written, because on the held
        path that decides whether he can be told the words are in there.

        `returns` is what the injector reports it pressed Return for. It is the
        answer to "did that just submit something?", which is a question he
        should be able to ask an hour later rather than only in the moment.
        """
        try:
            return bool(self.record(final, raw=raw, spoke_s=utt.duration_s,
                                    returns=returns, delivered=delivered))
        except Exception:
            log.exception("the dictation history could not be written; the text "
                          "was pasted and nothing else is affected")
            return False

    def _fail(self, message: str) -> None:
        self.overlay.set_state(OverlayState.ERROR, message.splitlines()[0])
        self.notify("error", message)

    # -- shutdown --------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._recording = False
            stale, self._session = self._session, None
        self._audio_stop()
        self._close_session(stale)
        self._drain_queue()
