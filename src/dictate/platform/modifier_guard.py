"""Not typing into a keyboard shortcut.

The hotkey is a chord - Ctrl+Alt+Space by default - and the listener ends the
recording when any one key of it comes up, because that is how people let go of
a chord: the thumb leaves the space bar and the other two fingers follow. The
paste happens half a second to a second later. If Ctrl is still down when it
does, the window that receives it does not see the letters `t`, `h`, `e`; it
sees Ctrl+T, Ctrl+H, Ctrl+E - new tab, history, and whatever the third one is
bound to. Reported as item 14 of the improvement scout's report, ESTIMATED
there and not reproducible from here.

It is the same defect as the stray Return this module shipped alongside, in a
different disguise: **dictated text executing something instead of being typed.**
So the answer is the same shape. Wait for his own modifiers to come up - he is
letting go anyway, and the wait costs nothing when they are already up - and if
they are still down at the deadline, synthesise the key-ups before typing.

What is decided here is pure and tested (tests/test_modifier_guard.py); reading
the keyboard and synthesising a key-up are two calls passed in, and they live in
`windows/inject.py`.
"""

from __future__ import annotations

import time
from collections.abc import Callable

CONTROL = "control"
ALT = "alt"
WINDOW = "window"

#: The three that turn a character into a command. Shift is deliberately not
#: here: Shift+E is E, not a shortcut, and a Shift that dictate forced up would
#: be interfering with a key he may be holding for a reason of his own.
GUARDED = (CONTROL, ALT, WINDOW)


def settle(
    is_down: Callable[[str], bool],
    force_up: Callable[[list[str]], None],
    *,
    wait_s: float,
    recording: Callable[[], bool] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    poll_s: float = 0.02,
) -> tuple[list[str], list[str]]:
    """Make sure no modifier is held before text is typed.

    Returns `(held, forced)`: what was down when we looked, and what had to be
    forced up because it was still down at the deadline. Both empty is the
    ordinary case - he let go before the transcription came back - and it costs
    one read of the keyboard.

    `recording` is the one case where a modifier that will not come up is left
    alone: he is already holding the chord for the NEXT utterance, and a
    synthesised key-up goes through the same low-level keyboard hook the hotkey
    listens on - which ends the recording he has just started. Forcing there
    would be this module causing the fault it is here to prevent, one layer up.

    `wait_s = 0` turns the whole thing off: nothing is read and nothing is
    forced, which is exactly how dictate behaved before this existed.
    """
    if wait_s <= 0:
        return [], []
    held = [name for name in GUARDED if is_down(name)]
    if not held:
        return [], []

    deadline = clock() + wait_s
    still = held
    while clock() < deadline:
        sleep(min(poll_s, max(0.0, deadline - clock())))
        still = [name for name in GUARDED if is_down(name)]
        if not still:
            return held, []
    if not still:
        return held, []
    if recording is not None and recording():
        return held, []
    # He is holding them for longer than anyone lets go of a chord, so this is
    # no longer "wait for him". Sending the key-ups is the only way the next
    # character is a character. His own key-up arrives afterwards and is
    # harmless - it is a key-up for a key the target already believes is up.
    force_up(still)
    return held, still
