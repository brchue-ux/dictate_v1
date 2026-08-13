"""Giving the GPU its memory back between dictation sessions.

`docs/DESIGN.md` constraint 1 says the Whisper model stays resident in VRAM,
because reloading it per utterance costs ~2 s and blows the latency budget. That
is still true *while dictating*. It stopped being the whole story once the same
card started being used for games and compute: 1.6 GB was held for as long as
dictate was running, whether or not a word had been spoken in hours.

So residency is now bounded by use rather than by process lifetime:

    RESIDENT ──── idle for N minutes, nothing in flight ────► RELEASING
        ▲                                                        │
        │                                                   whisper-server
    load finished                                          shut down cleanly
        │                                                        ▼
     WARMING ◄──────── hotkey PRESSED ──────────────────────  RELEASED

**The warm-up starts at hotkey PRESS, not at release.** He holds the key and
speaks for several seconds before letting go, and that is exactly the window the
model needs to load. Spending it loading means the "few seconds of spin up" he
accepted mostly never appears: by the time he releases, the model is usually
already back. Live captions come from the CPU model and are unaffected, so words
still appear on screen while the GPU model loads behind them.

Nothing about the transcription itself changes - same server, same model, same
arguments, same output. Only how long it stays loaded.

This class wraps a `BatchTranscriber` and is one itself, so the pipeline holds
it exactly as it held the backend. Everything whisper-specific, including the
process supervision, stays in `whisper_backend.py` / `process.py`: a reload is
an ordinary `start()` on the same supervisor, so the health wait, the
restart-on-crash budget and the clean shutdown all apply to the new process too.

`tests/test_residency.py` covers the state machine, the timer, and the awkward
timings - pressing during a shutdown, pressing during a warm-up, an utterance
that ends before the model has loaded, and overlapping utterances - none of
which need a GPU to get wrong.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from enum import Enum

from ..errors import BackendUnavailableError, DictateError

log = logging.getLogger(__name__)

#: Backstop for a caller waiting on someone else's transition. The real deadline
#: is the backend's own `startup_timeout_s`; this only exists so that a wedged
#: transition surfaces as a message instead of a hang.
DEFAULT_WAIT_TIMEOUT_S = 300.0


class Residency(Enum):
    """Where the model is. `describe` and `dictate doctor` report this."""

    RESIDENT = "resident"    # loaded in VRAM, ready to transcribe
    WARMING = "warming"      # being loaded, right now
    RELEASING = "releasing"  # being shut down, right now
    RELEASED = "released"    # not loaded; the card has its memory back


def watch_interval(idle_release_s: float) -> float:
    """How often to check the clock. A tenth of the idle period, within reason:
    often enough that the release lands close to the configured time, rarely
    enough that a mostly-idle app is not waking up constantly."""
    return max(1.0, min(10.0, idle_release_s / 10.0))


def minutes_text(seconds: float) -> str:
    minutes = seconds / 60.0
    text = f"{minutes:g}"
    return f"{text} minute" if minutes == 1 else f"{text} minutes"


class ResidentModel:
    """A `BatchTranscriber` that unloads itself when it has not been used.

    Implements `engines.base.BatchTranscriber`, plus `note_press()`, which the
    hotkey calls on key-DOWN so the load overlaps the speaking.
    """

    def __init__(
        self,
        inner,
        *,
        idle_release_s: float,
        wait_timeout_s: float = DEFAULT_WAIT_TIMEOUT_S,
        clock: Callable[[], float] = time.monotonic,
        poll_interval_s: float | None = None,
    ) -> None:
        self.inner = inner
        self.idle_release_s = max(0.0, idle_release_s)
        self.wait_timeout_s = wait_timeout_s
        self.clock = clock
        self.poll_interval_s = (
            poll_interval_s if poll_interval_s is not None
            else watch_interval(self.idle_release_s)
        )
        #: False means "behave exactly as dictate did before": load at startup,
        #: keep it loaded until the app exits.
        self.enabled = self.idle_release_s > 0

        self._cond = threading.Condition(threading.Lock())
        self._state = Residency.RELEASED
        #: Transcriptions running right now. The model is never released with
        #: one of these outstanding, and this is incremented under the same lock
        #: that guards the state so the idle check cannot slip in between.
        self._in_flight = 0
        self._last_used = clock()
        #: Set only while RELEASING, by whoever wants the model back the moment
        #: the shutdown finishes. The releasing worker consumes and clears it.
        self._want_resident = False
        self._failure: BaseException | None = None
        self._closed = False
        self._worker: threading.Thread | None = None
        self._watcher: threading.Thread | None = None
        self._watch_stop = threading.Event()
        #: Counts completed loads, for the tests and for the log.
        self.loads = 0
        self.releases = 0

    # -- what the pipeline sees -----------------------------------------

    def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        if not pcm:
            return ""
        self._acquire()
        try:
            return self.inner.transcribe(pcm, sample_rate)
        finally:
            self._done()

    def is_healthy(self) -> bool:
        with self._cond:
            if self._closed:
                return False
            if self._state is not Residency.RESIDENT:
                # Released or loading: a transcribe request WILL be served, it
                # just waits for the load first. It is only unhealthy if the
                # last attempt to load failed.
                return self._failure is None
        return self.inner.is_healthy()

    @property
    def describe(self) -> str:
        if not self.enabled:
            return self.inner.describe
        return (f"{self.inner.describe}, unloaded after "
                f"{minutes_text(self.idle_release_s)} of not dictating")

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Load the model and start watching for idle. Called once, at app start.

        Deliberately synchronous and deliberately still done at startup: a
        missing model file or an unbuildable server has to stop `dictate run`,
        not be discovered in the middle of a sentence.
        """
        with self._cond:
            self._closed = False
            self._last_used = self.clock()
            self._wait_for_transition()
            already = self._state is Residency.RESIDENT
            if not already:
                self._state = Residency.WARMING
                self._failure = None
        if not already:
            error = self._load()
            if error is not None:
                raise error
        self._start_watcher()

    def stop(self) -> None:
        """Shut everything down. Safe to call twice, and from any state."""
        with self._cond:
            self._closed = True
            self._want_resident = False
            self._cond.notify_all()
        self._watch_stop.set()
        watcher, self._watcher = self._watcher, None
        if watcher and watcher.is_alive() and watcher is not threading.current_thread():
            watcher.join(timeout=5.0)

        # Stop the server BEFORE joining a warm-up in flight: ManagedProcess
        # watches for this and abandons a start it is part way through, which is
        # what keeps the join below bounded instead of waiting out the whole
        # startup timeout.
        self._stop_inner()
        worker, self._worker = self._worker, None
        if worker and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=30.0)
            if worker.is_alive():
                log.warning("the model %s did not finish before shutdown", worker.name)
        # The worker may have got the server up again in the moment between the
        # two, so ask once more. Both calls are no-ops if it is already down.
        self._stop_inner()
        with self._cond:
            self._state = Residency.RELEASED
            self._cond.notify_all()

    # -- the hotkey ------------------------------------------------------

    def note_press(self) -> None:
        """The hotkey went DOWN. Start loading the model now, in the background.

        Returns immediately: this runs on the hotkey thread, which must never
        block, and the point is to spend the seconds he spends speaking on the
        load rather than to wait for it.

        Never raises. If the warm-up cannot be started, the load happens the
        old way - when the audio arrives - which is slower but not broken.
        """
        try:
            self._note_press()
        except Exception:
            log.exception("could not start loading the model at hotkey press; "
                          "it will be loaded when the utterance is transcribed")

    def _note_press(self) -> None:
        with self._cond:
            self._last_used = self.clock()
            if self._closed or self._state in (Residency.RESIDENT, Residency.WARMING):
                return  # already there, or already on its way
            if self._state is Residency.RELEASING:
                # Too late to call the shutdown back - whisper-server is already
                # being terminated, and abandoning that half way would leave the
                # supervisor owning a process it thinks it does not. Let it
                # finish and reload immediately afterwards.
                self._want_resident = True
                log.info("hotkey pressed while the model was being released; "
                         "it will be reloaded as soon as the shutdown finishes")
                return
            self._state = Residency.WARMING
            self._failure = None
            try:
                self._spawn(self._warm_worker, "dictate-model-warmup")
            except Exception:
                # Nothing is going to do the load, so do not leave the state
                # saying something is.
                self._state = Residency.RELEASED
                self._cond.notify_all()
                raise
        log.info("hotkey pressed with the model released; loading it while he speaks")

    # -- the idle timer --------------------------------------------------

    def check_idle(self) -> bool:
        """One tick of the idle timer. True if it began a release.

        Split out from the watcher thread so the timing is tested with a fake
        clock rather than by sleeping.
        """
        with self._cond:
            if (not self.enabled or self._closed or self._in_flight
                    or self._state is not Residency.RESIDENT):
                return False
            idle_for = self.clock() - self._last_used
            if idle_for < self.idle_release_s:
                return False
            self._state = Residency.RELEASING
            try:
                self._spawn(self._release_worker, "dictate-model-release")
            except Exception:
                self._state = Residency.RESIDENT
                self._cond.notify_all()
                log.exception("could not start the idle release; the model stays loaded")
                return False
        log.info("no dictation for %s; releasing whisper-server so the GPU gets "
                 "its memory back", minutes_text(self.idle_release_s))
        return True

    def _start_watcher(self) -> None:
        if not self.enabled:
            return
        with self._cond:
            if self._watcher is not None and self._watcher.is_alive():
                return
            self._watch_stop.clear()
            self._watcher = threading.Thread(
                target=self._watch, name="dictate-idle-watch", daemon=True)
            self._watcher.start()

    def _watch(self) -> None:
        while not self._watch_stop.wait(self.poll_interval_s):
            try:
                self.check_idle()
            except Exception:  # a timer must never take the app down
                log.exception("the idle check failed; carrying on")

    # -- state -----------------------------------------------------------

    @property
    def state(self) -> Residency:
        with self._cond:
            return self._state

    @property
    def last_failure(self) -> BaseException | None:
        with self._cond:
            return self._failure

    # -- transitions -----------------------------------------------------

    def _spawn(self, target: Callable[[], None], name: str) -> None:
        """Start the one background thread. Called with the lock held; at most
        one transition is ever in flight, so at most one of these exists."""
        self._worker = threading.Thread(target=target, name=name, daemon=True)
        self._worker.start()

    def _warm_worker(self) -> None:
        error = self._load()
        if error is not None:
            # Not surfaced to him here on purpose: he is mid-sentence, and the
            # transcribe that follows raises with the full message, so he is
            # told once, when it actually costs him something.
            log.error("loading the model after the hotkey press failed: %s", error)

    def _release_worker(self) -> None:
        t0 = self.clock()
        self._stop_inner()
        with self._cond:
            self.releases += 1
            self._state = Residency.RELEASED
            want = self._want_resident and not self._closed
            self._want_resident = False
            if want:
                self._state = Residency.WARMING
                self._failure = None
            self._cond.notify_all()
        log.info("whisper-server released in %.1fs; its VRAM is back", self.clock() - t0)
        if want:
            error = self._load()
            if error is not None:
                log.error("reloading the model straight after the release failed: %s",
                          error)

    def _load(self) -> BaseException | None:
        """Bring the model up. The caller owns the WARMING state and holds no lock.

        Returns the failure rather than raising, because half its callers are
        background threads where a raise would be swallowed anyway.
        """
        t0 = self.clock()
        error: BaseException | None = None
        ok = False
        try:
            self.inner.start()
            ok = True
        except Exception as exc:
            error = exc
        finally:
            # In a finally so that even something that is not an Exception
            # leaves a state some later caller can act on, rather than a
            # WARMING nobody is working on.
            with self._cond:
                if ok:
                    self.loads += 1
                    self._state = Residency.RESIDENT
                    self._failure = None
                    self._last_used = self.clock()
                else:
                    self._state = Residency.RELEASED
                    self._failure = error
                self._cond.notify_all()
        if ok:
            log.info("whisper-server resident again after %.1fs", self.clock() - t0)
        return error

    def _stop_inner(self) -> None:
        try:
            self.inner.stop()
        except Exception:
            # A server that will not die is reported by the next start(), which
            # finds the port still in use and says so in plain language.
            log.exception("whisper-server did not shut down cleanly")

    def _wait_for_transition(self) -> None:
        """Wait out somebody else's WARMING/RELEASING. Lock held on entry and exit.

        Timed against the real clock, not the injected one: `clock` exists so
        the idle *policy* can be tested without sleeping, while this is a
        backstop against a hang and has to measure actual seconds.
        """
        deadline = time.monotonic() + self.wait_timeout_s
        while self._state in (Residency.WARMING, Residency.RELEASING):
            if self._closed:
                return  # shutting down; the caller re-checks and gives up
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BackendUnavailableError(
                    "The transcription model has been "
                    f"{self._state.value} for over {self.wait_timeout_s:.0f} "
                    "seconds, which should never take that long.",
                    "Restart dictate. If it keeps happening, raise "
                    "[whisper] startup_timeout_s or set "
                    "[whisper] idle_release_minutes = 0 to stop dictate "
                    "unloading the model at all.",
                )
            self._cond.wait(timeout=min(remaining, 1.0))

    def _acquire(self) -> None:
        """Make the model resident and count this caller as a user of it.

        Blocks - it runs on the finalize worker, never on the hotkey or audio
        thread. On return the model is loaded and cannot be released until the
        matching `_done()`.
        """
        while True:
            with self._cond:
                if self._closed:
                    raise BackendUnavailableError(
                        "dictate is shutting down, so that dictation was not "
                        "transcribed.",
                        "Nothing to fix - this only happens on the way out.",
                    )
                self._last_used = self.clock()
                if self._state is Residency.RESIDENT:
                    self._in_flight += 1
                    return
                if self._state is Residency.RELEASING:
                    # Have the shutdown reload it the moment it finishes rather
                    # than starting a second one on top of it.
                    self._want_resident = True
                if self._state in (Residency.WARMING, Residency.RELEASING):
                    self._wait_for_transition()
                    continue  # re-check: it may have failed, not succeeded
                # RELEASED and nobody is loading it - most often because the
                # utterance was so short the press-triggered warm-up has not
                # even been reached yet. Load it here, on this thread.
                self._state = Residency.WARMING
                self._failure = None
            error = self._load()
            if error is not None:
                raise self._reload_error(error)

    def _done(self) -> None:
        with self._cond:
            self._in_flight = max(0, self._in_flight - 1)
            self._last_used = self.clock()
            self._cond.notify_all()

    def _reload_error(self, cause: BaseException) -> BackendUnavailableError:
        detail = cause.message if isinstance(cause, DictateError) else str(cause)
        remedy = (
            cause.remedy if isinstance(cause, DictateError) and cause.remedy else
            "Run `dictate doctor` to see which piece is missing, then restart "
            "dictate."
        )
        return BackendUnavailableError(
            f"dictate had given the transcription model's memory back after "
            f"{minutes_text(self.idle_release_s)} of not dictating, and could not "
            f"load it again:\n{detail}\n"
            f"That dictation was not pasted.",
            remedy + "\nTo stop dictate unloading the model at all, set "
                     "[whisper] idle_release_minutes = 0 in your config.",
        )
