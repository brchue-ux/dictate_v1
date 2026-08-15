"""Deliver finished text after its captured window returns to the foreground.

This is the honest implementation of ``[paste] on_focus_change = "restore"``.
Windows' keyboard stream has no target handle, and a custom terminal surface
cannot be assumed to expose the standard Edit control which accepts a targeted
``WM_PASTE``. Dictate therefore waits for the user to restore the destination
himself. It never calls ``WindowTracker.focus`` and the injector is explicitly
told to refuse if the captured window is no longer foreground at the final
check.

One wait is kept at a time. Starting another utterance ends it, including when
the older utterance was still transcribing and had not joined the wait yet.
Before it is held in memory, dictate attempts both the clipboard and history;
every message names which copies actually succeeded. Closing dictate, closing
the target, a new utterance, or a failed paste therefore leaves the same honest
recovery floor rather than silently discarding the result.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass

from . import delivery
from .errors import InjectionError, TargetNotForegroundError
from .history import HeldReceipt
from .platform.base import OverlayState, TargetWindow

log = logging.getLogger(__name__)

Submit = Callable[[Callable[[], None]], object]
Notify = Callable[[str, str], None]


@dataclass(frozen=True)
class Request:
    """Finished batch text and the destination captured at hotkey press."""

    generation: int
    text: str
    raw: str
    spoke_s: float
    target: TargetWindow
    focused: TargetWindow | None
    on_clipboard: bool


@dataclass(frozen=True)
class _Pending:
    request: Request
    receipt: HeldReceipt | None

    @property
    def in_history(self) -> bool:
        return self.receipt is not None


class DeferredDelivery:
    """A single, bounded wait whose decisions are testable off Windows."""

    def __init__(self, *, windows, injector, overlay, history, submit: Submit,
                 notify: Notify | None = None) -> None:
        self.windows = windows
        self.injector = injector
        self.overlay = overlay
        self.history = history
        self.submit = submit
        self.notify = notify or (lambda level, message: None)
        self._lock = threading.RLock()
        self._generation = 0
        self._pending: _Pending | None = None
        self._scheduled = False
        self._closed = False

    @property
    def waiting(self) -> bool:
        with self._lock:
            return self._pending is not None

    def begin_utterance(self) -> int:
        """Start a new ownership generation and retire any older wait."""
        with self._lock:
            self._generation += 1
            generation = self._generation
            pending, self._pending = self._pending, None
            self._scheduled = False
        if pending is not None:
            req = pending.request
            log.info("delivery route deferred: stopped waiting for %s because "
                     "another dictation started", req.target)
            self.notify("info", delivery.stopped_waiting_message(
                req.target, "another dictation started",
                on_clipboard=req.on_clipboard,
                in_history=pending.in_history))
        return generation

    def defer(self, request: Request) -> None:
        """Keep one finished transcription until its exact target returns."""
        receipt = self._record_held(request)
        pending = _Pending(request, receipt)
        with self._lock:
            current = request.generation == self._generation and not self._closed
            if current:
                self._pending = pending
                self._scheduled = False
        if not current:
            # A later utterance began while this one was still in the finalise
            # worker. Its words are recoverable, but an old automatic paste may
            # not appear after the newer intent.
            log.info("delivery route deferred: not queued for %s because a "
                     "later dictation already started", request.target)
            self.notify("info", delivery.stopped_waiting_message(
                request.target, "another dictation started before these words "
                "were ready", on_clipboard=request.on_clipboard,
                in_history=pending.in_history))
            return

        message = delivery.waiting_message(
            request.target, request.focused,
            on_clipboard=request.on_clipboard,
            in_history=pending.in_history)
        log.info("delivery route deferred: waiting for %s; foreground unchanged",
                 request.target)
        self.overlay.set_state(OverlayState.WAITING, message.splitlines()[0],
                               request.target)
        self.notify("info", message)

    def poll(self) -> None:
        """Schedule delivery when the user has put the target in front again."""
        with self._lock:
            pending = self._pending
            if pending is None or self._scheduled or self._closed:
                return
        try:
            focused = self.windows.foreground()
        except Exception:
            log.debug("could not read the foreground window while waiting",
                      exc_info=True)
            return
        if focused is None:
            return
        if not delivery.same_window(pending.request.target, focused):
            if self._target_closed(pending.request.target):
                self._finish_closed(pending)
            return

        with self._lock:
            if self._pending is not pending or self._closed:
                return
            self._scheduled = True
        try:
            self.submit(lambda: self._deliver(pending))
        except Exception:
            with self._lock:
                if self._pending is pending:
                    self._scheduled = False
            log.exception("could not schedule the waiting delivery; will retry")

    def close(self) -> None:
        """Forget the in-memory convenience copy; clipboard/history remain."""
        with self._lock:
            self._closed = True
            self._generation += 1
            self._pending = None
            self._scheduled = False

    def _deliver(self, pending: _Pending) -> None:
        req = pending.request
        with self._lock:
            if self._pending is not pending or self._closed \
                    or req.generation != self._generation:
                return
        try:
            returns = self.injector.send(
                req.text, req.target, require_target_foreground=True,
                still_allowed=lambda: self._is_current(pending)) or 0
        except TargetNotForegroundError as exc:
            # The user moved again between the watch loop and the final guard.
            # With no partial input, leave the same request waiting for a later
            # foreground observation. If a long SendInput run already sent a
            # batch, retrying the whole text would duplicate it, so that is a
            # permanent held outcome like any other partial injection.
            if exc.partial:
                self._finish_failed(pending, exc.report(), partial=True)
                return
            with self._lock:
                if self._pending is not pending:
                    log.info("delivery route deferred: cancelled before send "
                             "because a newer dictation started")
                    return
                self._scheduled = False
            log.info("delivery route deferred: target moved before send; still "
                     "waiting for %s", req.target)
            return
        except InjectionError as exc:
            self._finish_failed(pending, exc.report(), partial=exc.partial)
            return
        except Exception as exc:
            log.exception("unexpected failure delivering waiting text")
            self._finish_failed(
                pending,
                f"The waiting delivery raised {type(exc).__name__}: {exc}")
            return

        with self._lock:
            if self._pending is pending:
                self._pending = None
            self._scheduled = False
        if pending.receipt is not None:
            self.history.mark_delivered(pending.receipt, returns=returns)
        log.info("delivery route deferred: succeeded to %s; foreground unchanged",
                 req.target)
        self.overlay.set_state(OverlayState.DONE, "", req.target)
        self.notify("info", f"Pasted the words that were waiting into "
                    f"{req.target} after you returned to it. dictate did not "
                    "bring any window to the front.")

    def _finish_failed(self, pending: _Pending, detail: str, *,
                       partial: bool = False) -> None:
        with self._lock:
            if self._pending is not pending:
                return
            self._pending = None
            self._scheduled = False
        req = pending.request
        message = delivery.held_message(
            delivery.REFUSED, target=req.target,
            on_clipboard=req.on_clipboard, in_history=pending.in_history,
            partial=partial, detail=detail)
        log.warning("delivery route deferred: failed to %s; text held", req.target)
        self.overlay.set_state(OverlayState.ERROR, message.splitlines()[0], req.target)
        self.notify("error", message)

    def _finish_closed(self, pending: _Pending) -> None:
        with self._lock:
            if self._pending is not pending:
                return
            self._pending = None
            self._scheduled = False
        req = pending.request
        message = delivery.held_message(
            delivery.CLOSED, target=req.target,
            on_clipboard=req.on_clipboard, in_history=pending.in_history)
        log.warning("delivery route deferred: target closed; text held for %s",
                    req.target)
        self.overlay.set_state(OverlayState.ERROR, message.splitlines()[0], req.target)
        self.notify("error", message)

    def _target_closed(self, target: TargetWindow) -> bool:
        try:
            return not bool(self.windows.exists(target))
        except Exception:
            log.debug("could not ask whether deferred target still exists",
                      exc_info=True)
            return False

    def _is_current(self, pending: _Pending) -> bool:
        """May this exact waiting result still be sent?"""
        with self._lock:
            return (self._pending is pending and not self._closed
                    and pending.request.generation == self._generation)

    def _record_held(self, request: Request) -> HeldReceipt | None:
        try:
            return self.history.record_held(
                request.text, raw=request.raw, spoke_s=request.spoke_s)
        except Exception:
            # HistoryStore itself never raises, but this boundary has the same
            # guarantee as Pipeline._remember: a record cannot cost dictation.
            log.exception("the deferred dictation could not be written to history")
            return None
