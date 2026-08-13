"""The opacity ramp, as arithmetic.

Split out of the Tk code for the same reason `geometry.py` is: this is the part
of "smoother in and out" that can be wrong in a way nobody would notice without
watching it, and it is pure arithmetic, so it gets tested (tests/test_overlay.py)
even though the window itself cannot be.

The shape of it is one decision. The overlay is committed to reading as something
heavy (see the block comment at the top of `windows/overlay.py`), and heavy things
do not snap: they start slowly, cover the middle quickly, and settle slowly. That
is a cubic ease-in-out, and it is why a linear ramp is not used here. The fade out
is deliberately longer than the fade in - an object is slower to leave than to
arrive - which costs nothing, because by the time it runs the text has already
been pasted.

Two properties matter more than the curve:

* **A reversal resumes from wherever the opacity actually is.** Pressing the
  hotkey again while the overlay is still fading out must not restart from zero
  and flash; it picks up the alpha on screen and drives it back up, over a
  proportionally shorter time.
* **Nothing here blocks.** `Fade` holds no locks, does no I/O and knows nothing
  about Tk. The caller asks it for the next value, whenever it happens to ask.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Below this, a fade is not a fade - it is a flicker. `config.validate()` refuses
#: shorter durations rather than shipping something that reads as a snap.
MIN_FADE_MS = 60


def ease_in_out_cubic(t: float) -> float:
    """`t` in 0..1 -> eased 0..1. Slow start, fast middle, slow settle."""
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    if t < 0.5:
        return 4.0 * t * t * t
    return 1.0 - ((-2.0 * t + 2.0) ** 3) / 2.0


def steps(duration_ms: int, step_ms: int) -> int:
    """How many frames a fade of `duration_ms` takes at `step_ms` per frame.

    At least one, so a duration shorter than a single step still moves rather
    than dividing by zero.
    """
    if step_ms <= 0:
        raise ValueError("step_ms must be positive")
    return max(1, round(duration_ms / step_ms))


@dataclass
class Fade:
    """A one-dimensional opacity ramp that can be redirected mid-flight.

    The caller drives it: `to()` sets a destination, then `advance()` is called
    once per frame and returns the alpha to apply. `done` says when to stop
    calling. Nothing is scheduled here - the Tk code owns the clock, because Tk
    owns the thread.
    """

    alpha: float = 0.0
    target: float = 0.0
    step_ms: int = 16
    #: 0..1 progress along the current ramp, not the alpha itself: the easing is
    #: applied between `_from` and `target`, so a redirected fade stays smooth.
    _t: float = 1.0
    _from: float = 0.0
    _steps: int = 1

    def to(self, target: float, duration_ms: int) -> None:
        """Aim at `target`, taking `duration_ms` from *here*, not from the start.

        A reversal that is already 80% of the way down has only 20% of the
        distance left to cover, so it is given 20% of the time. That is what
        keeps a quick second press from feeling like it re-runs the whole
        animation.
        """
        target = 0.0 if target < 0.0 else (1.0 if target > 1.0 else target)
        remaining = abs(target - self.alpha)
        self._from = self.alpha
        self.target = target
        if remaining <= 0.0 or duration_ms <= 0:
            self.alpha = target
            self._t = 1.0
            self._steps = 1
            return
        self._t = 0.0
        self._steps = steps(round(duration_ms * remaining), self.step_ms)

    def advance(self) -> float:
        """Move one frame along and return the alpha to apply now."""
        if self.done:
            return self.alpha
        self._t = min(1.0, self._t + 1.0 / self._steps)
        eased = ease_in_out_cubic(self._t)
        self.alpha = self._from + (self.target - self._from) * eased
        return self.alpha

    def jump_to(self, alpha: float) -> None:
        """Go straight there. Used when fading is turned off, and on shutdown."""
        self.alpha = self.target = 0.0 if alpha < 0.0 else (1.0 if alpha > 1.0 else alpha)
        self._from = self.alpha
        self._t = 1.0
        self._steps = 1

    @property
    def done(self) -> bool:
        return self._t >= 1.0
