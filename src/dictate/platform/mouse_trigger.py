"""Holding a mouse button to talk: what dictate swallows, and what it gives back.

**The problem this file exists for.** Mouse 4 is Back in every browser, file
manager and editor; the middle button opens a link in a new tab, closes a tab,
and starts autoscroll when it is held. A push-to-talk trigger has to SWALLOW the
button - Windows offers no way to observe a button globally and let it through
selectively at the same moment - and dictate now starts at logon and runs all
day. So the two blanket rules are both unacceptable:

* swallow always, and the button's own job stops working for as long as dictate
  is running - which is always;
* pass through always, and every single dictation also navigates Back, or opens
  autoscroll, in whatever window happens to be focused. That is worse.

**The rule that is neither.** The press is provisional. The button-down is
always swallowed, so the browser does not go Back the instant he starts talking.
What he does next says which of the two things it was:

* he **holds** it and speaks - the up is swallowed too, and nothing but dictate
  ever hears about that button;
* he **clicks** it - shorter than `[audio] min_utterance_ms`, the same number
  that already decides an utterance was a mis-press - and dictate puts the click
  back: the up is swallowed and a fresh down-and-up is sent, so the app gets the
  click it was waiting for. It arrives a millisecond or two after he let go
  rather than the instant he pressed, which is the whole of what he loses on a
  click, and the utterance is dropped for being too short exactly as a
  mis-pressed hotkey already is.

`[hotkey] mouse_click_through = false` turns the second half off: the button is
then dictate's alone while dictate is running, which is the right answer for
somebody who never uses it for anything else and does not want a synthesised
click in the middle of his day.

Everything above is decided HERE, in plain Python, and tested in
tests/test_mouse_trigger.py. `platform/windows/mouse.py` holds the low-level
mouse hook and does what this says: it may not decide anything, because nobody
on this build can run it.

Three edges that are easy to get wrong, and are what most of the state below is
about:

* **the button already held when dictate starts.** We never saw its down, so the
  app did: swallowing the up would leave that app believing the button is still
  down forever. An up with no down of ours passes straight through.
* **the button still held when dictate stops.** We swallowed its down, so ending
  the recording is ours to do - `disarm()` returns the release. The physical up
  then reaches the app on its own, which is the harmless half of the pair.
* **our own replayed click.** It goes back through the same hook. It carries a
  tag (`dwExtraInfo`) rather than being recognised by "it was injected", because
  a remapping utility or a mouse driver injects events too and dictate should
  still trigger on those.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

#: The two things that happen to a button, as this module names them. The
#: Windows message numbers are `windows/mouse.py`'s business.
DOWN = "down"
UP = "up"


@dataclass(frozen=True)
class Action:
    """What the hook does with one event, and what the app is told afterwards.

    `swallow` is the only part that has to happen inside the hook callback -
    it is the callback's return value. The other three are handed to a worker
    thread, because a low-level hook that takes too long is dropped by Windows
    and takes the whole machine's mouse with it while it lasts.
    """

    #: Do not pass this event on to the rest of Windows.
    swallow: bool = False
    #: Tell the app the trigger went down: start recording.
    press: bool = False
    #: Tell the app the trigger came up: finish the utterance.
    release: bool = False
    #: Send the button's own click, because that is what it turned out to be.
    replay: bool = False


#: Nothing to do; the event is not ours. The overwhelmingly common answer, and
#: the one every mouse move gets.
PASS = Action()


class MouseTrigger:
    """The press/release state machine for one mouse button.

    Not a listener and not a hook: it holds no Windows anything and answers
    questions. `event()` is called from the hook callback and must stay as cheap
    as it looks - one lock, a few comparisons, no allocation beyond the answer.
    """

    def __init__(self, button: str, *, click_through: bool = True,
                 min_hold_s: float = 0.35) -> None:
        self.button = button
        self.click_through = click_through
        self.min_hold_s = max(0.0, min_hold_s)
        self._lock = threading.Lock()
        self._armed = False
        self._down_at: float | None = None

    @property
    def armed(self) -> bool:
        with self._lock:
            return self._armed

    @property
    def holding(self) -> bool:
        with self._lock:
            return self._down_at is not None

    def arm(self) -> None:
        """Start deciding. Called once the hook is installed.

        A button that is already down when this happens is not adopted: the app
        under the cursor has its down and will get its up.
        """
        with self._lock:
            self._armed = True
            self._down_at = None

    def disarm(self) -> Action:
        """Stop deciding, and say whether an utterance was left open.

        The answer is a release and never a replay: he is holding the button
        because he is talking, and giving the app a click he did not ask for on
        the way out would be dictate navigating Back as it shut down.
        """
        with self._lock:
            was_down = self._down_at is not None
            self._armed = False
            self._down_at = None
        return Action(release=True) if was_down else PASS

    def event(self, kind: str, button: str, *, now: float,
              ours: bool = False) -> Action:
        """One mouse event. Returns what to do about it.

        `ours` is the tag on a click this module asked to be replayed; it goes
        through untouched, which is what stops the replay from being read as a
        new press.
        """
        if button != self.button or ours:
            return PASS
        with self._lock:
            if not self._armed:
                return PASS
            if kind == DOWN:
                if self._down_at is not None:
                    # A second down with no up between. Swallow it - the app
                    # must not see half a pair - but do not start a second
                    # utterance on top of the one already running.
                    return Action(swallow=True)
                self._down_at = now
                return Action(swallow=True, press=True)
            if kind == UP:
                down_at, self._down_at = self._down_at, None
                if down_at is None:
                    # His down went to the app - it was held before dictate
                    # started, or before the hook went in. The up belongs to
                    # the app too.
                    return PASS
                held = now - down_at
                if held < self.min_hold_s and self.click_through:
                    return Action(swallow=True, release=True, replay=True)
                return Action(swallow=True, release=True)
        return PASS
