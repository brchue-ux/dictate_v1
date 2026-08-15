"""Where the finished text goes when he has moved on since he started speaking.

The window the text belongs to is read at hotkey **press** (`Pipeline._capture_target`),
because between press and paste the caption panel appears, a notification may pop
and he may alt-tab - so "whatever is focused when we finish" is the wrong answer.
That much has always been true. What this module decides is the case that handle
was never enough for on its own: **he clicked somewhere else while he was talking,
and is still there when the words are ready.**

Three things could happen and none of them is obviously right:

* **Wait for the window he started in.** Windows' ordinary keyboard input has no
  target handle: it goes to the foreground queue. Attaching input queues does
  not change that, and setting a background window's focus activates it. A
  targeted ``WM_PASTE`` is documented only for standard Edit controls, not as a
  generic route into custom terminal surfaces. So the route which reuses the
  already-proven paste into the owner's terminal without stealing focus is to
  keep the finished text and deliver it the moment he puts that window in front
  again.
* **Paste into whatever is focused now.** Text arriving in an application that
  never asked for it, which is the same family of harm as the stray Return that
  ran a command in his terminal (`platform/line_breaks.py`, constraint 6). Not
  offered here, in any mode.
* **Do not paste. Keep the text and say so.** Nothing is typed anywhere he did
  not choose, and nothing lands in a window he is not looking at. Safe, and
  useless if the text is then unreachable - which is why the hold path is only
  half of this change, and `Pipeline._hold` is the other half.

The third is the default. The first is `[paste] on_focus_change = "restore"`.
The spelling stays because existing config files already contain it; what it
restores now is the destination, when the user returns there, never the window's
foreground position.

**The common accidental case never reaches a hold.** If he clicks away and clicks
back before the transcription comes back - which is most of them - the window in
front at paste time IS the captured one, and this returns `DELIVER` exactly as
though nothing had happened. A hold only fires when he is genuinely somewhere
else at the moment the words are ready.

Everything here is plain Python and decided from two window handles, so the whole
of it is tested off Windows (tests/test_delivery.py). Reading the two handles is
the platform's share, and it is two read-only calls -
`WindowTracker.foreground()` and `WindowTracker.exists()`.
"""

from __future__ import annotations

from dataclasses import dataclass

from .platform.base import TargetWindow

#: What `Pipeline._finalize` should do with the finished text.
DELIVER = "deliver"   # the window he started in is the one in front: paste, as ever
DEFER = "defer"       # wait until the captured window is foreground again
HOLD = "hold"         # paste nowhere; keep the text and tell him where it is

#: The values `[paste] on_focus_change` takes. Deliberately two: "paste into
#: whatever is focused now" is not a mode, because there is no configuration in
#: which dictate typing into a window he never dictated into is what he meant.
HOLD_MODE = "hold"
RESTORE_MODE = "restore"
MODES = (HOLD_MODE, RESTORE_MODE)

#: Why a hold happened. Three different sentences, because they are three
#: different things that went wrong and only one of them is his doing.
MOVED = "moved"       # he is in another window now
CLOSED = "closed"     # the window he was dictating into has gone
REFUSED = "refused"   # the paste itself failed - Windows would not take it


@dataclass(frozen=True)
class Decision:
    """What to do, and the one line the log gets for why."""

    action: str
    why: str
    #: Set only on a hold, and only for the two cases decided here.
    reason: str = ""

    @property
    def pastes(self) -> bool:
        return self.action == DELIVER

    @property
    def waits(self) -> bool:
        return self.action == DEFER


def decide(
    target: TargetWindow | None,
    focused: TargetWindow | None,
    *,
    target_exists: bool | None = None,
    mode: str = HOLD_MODE,
    restore_focus: bool = True,
) -> Decision:
    """Deliver, defer or hold, from the two windows and the two settings.

    `target` is what was captured at press and `focused` is what is in front
    now; either may be `None`, because reading the foreground window can fail and
    the honest answer to "which window is that" is sometimes "I could not tell".
    `target_exists` is `IsWindow` on the captured handle, or `None` if nobody
    asked - it only ever changes which sentence he is shown, never the action.

    **An unknown is never treated as a change.** With no target, or no reading of
    what is focused now, this returns `DELIVER` and dictate behaves exactly as it
    did before this module existed. Refusing to paste because a query came back
    empty would be reporting a healthy system as broken, over the one thing the
    product exists to do.
    """
    if target is None:
        return Decision(DELIVER, "no window was captured at press")
    if focused is None:
        return Decision(DELIVER, "could not read which window is in front now")
    if same_window(target, focused):
        return Decision(DELIVER, "still in the window it was started in")

    reason = CLOSED if target_exists is False else MOVED
    if reason == CLOSED:
        # Nothing to wait for even in restore mode: there is no window left.
        return Decision(HOLD, f"the captured window {target} no longer exists",
                        CLOSED)
    if mode == RESTORE_MODE:
        # `restore_focus` is deliberately irrelevant here. It remains a config
        # key for existing files and for the ordinary injector path, but this
        # route never calls a foreground API at either value.
        return Decision(DEFER, f"focus moved to {focused}; waiting for {target} "
                               "without bringing it to the front")
    return Decision(HOLD, f"focus moved from {target} to {focused}", MOVED)


def same_window(captured: TargetWindow, current: TargetWindow) -> bool:
    """Is ``current`` still the window represented by ``captured``?

    Titles legitimately change and process names are not identities. HWND is
    the primary identity, with the captured pid as a reuse guard when both
    reads supplied one. An unknown pid keeps the pre-existing handle-only
    behaviour: an unreadable property may not turn a healthy delivery into a
    refusal.
    """
    if captured.handle != current.handle:
        return False
    if captured.process_id and current.process_id:
        return captured.process_id == current.process_id
    return True


def waiting_message(
    target: TargetWindow,
    focused: TargetWindow | None,
    *,
    on_clipboard: bool = False,
    in_history: bool = False,
) -> str:
    """What he is told while restore mode waits for its captured window."""
    where = _where(on_clipboard, in_history)
    lines = [f"Waiting to paste into {_name(target)} when you return to it. "
             f"{where[0]}"]
    if focused is not None:
        lines.append(f"{_name(focused)} stays in front. dictate did not raise "
                     f"another window and did not type into this one.")
    else:
        lines.append("dictate did not raise another window and did not type "
                     "into whichever window is in front now.")
    lines.append("When the captured window is in front again, dictate will use "
                 "its ordinary paste route there. Starting another dictation "
                 "ends this automatic wait; the clipboard and history copies "
                 "remain.")
    lines.extend(where[1:])
    return "\n".join(lines)


def stopped_waiting_message(
    target: TargetWindow,
    why: str,
    *,
    on_clipboard: bool = False,
    in_history: bool = False,
) -> str:
    """What he is told when a deferred paste becomes an ordinary hold."""
    where = _where(on_clipboard, in_history)
    lines = [f"No longer waiting to paste into {_name(target)} - {why}. "
             f"{where[0]}"]
    lines.extend(where[1:])
    return "\n".join(lines)


def held_message(
    reason: str,
    *,
    target: TargetWindow | None = None,
    focused: TargetWindow | None = None,
    on_clipboard: bool = False,
    in_history: bool = False,
    partial: bool = False,
    detail: str = "",
) -> str:
    """What he is told when the text was not pasted.

    The first line is the whole of it in one sentence, because that is the line
    the caption panel shows (`Pipeline._fail` splits on the newline) and the line
    the tray tooltip carries. Everything after it is for the console and the log.

    It always ends by saying where the words are. That is the difference between
    refusing and losing, and it is the reason refusing is allowed to be the
    default at all.
    """
    if reason == CLOSED:
        opening = ("Not pasted - the window you were dictating into has closed."
                   if target is None else
                   f"Not pasted - {_name(target)} has closed.")
        body = ("dictate reads the window to paste into when you press the "
                "hotkey, so that the caption panel appearing cannot change it. "
                "That window is gone, and dictate will not pick another one for "
                "you.")
    elif reason == REFUSED:
        # Deliberately not "Windows would not accept the text": this reason also
        # covers a window that could not be brought back to the front, which is
        # a different refusal. The injector's own message is the detail below and
        # it says which - a step may only name a cause it has established.
        opening = "Not pasted - dictate could not put it into that window."
        body = detail or ""
    else:
        opening = "Not pasted - you moved to another window while you were speaking."
        body = (f"You started dictating into {_name(target)} and "
                f"{_name(focused)} is in front now, so dictate did not type "
                f"anywhere: it pastes into the window you were in when you "
                f"pressed the hotkey, and it will not bring that window back "
                f"over the top of what you are doing. Press the hotkey in the "
                f"window you want the text in and it goes there, as usual."
                if target is not None and focused is not None else
                "dictate pastes into the window you were in when you pressed the "
                "hotkey, and it will not bring that window back over the top of "
                "what you are doing.")

    where = _where(on_clipboard, in_history)
    lines = [f"{opening} {where[0]}"]
    if body:
        lines.append(body)
    if partial:
        lines.append("Some of it may already have been typed into that window "
                     "before Windows refused the rest, so look before you paste.")
    lines.extend(where[1:])
    if reason == MOVED:
        lines.append('To have dictate wait and paste when you return to that '
                     'window, without bringing it to the front, set [paste] '
                     'on_focus_change = "restore" in your dictate.toml.')
    return "\n".join(lines)


def _where(on_clipboard: bool, in_history: bool) -> list[str]:
    """Where the words are now, said in the order he can act on."""
    if on_clipboard and in_history:
        return ["Your words are on the clipboard: press Ctrl+V where you want them.",
                "They are also at the top of your dictation history (right-click "
                "the dictate icon by the clock, or run `dictate history`)."]
    if on_clipboard:
        return ["Your words are on the clipboard: press Ctrl+V where you want them."]
    if in_history:
        return ["Your words are at the top of your dictation history (right-click "
                "the dictate icon by the clock, or run `dictate history`)."]
    # Both failed. The log records the failure, not the user's full text. Say
    # the hard truth rather than inventing a recoverable copy.
    return ["dictate could not keep a recoverable copy of them either - not on "
            "the clipboard and not in your dictation history."]


def _name(window: TargetWindow | None) -> str:
    return "the window you were dictating into" if window is None else str(window)
