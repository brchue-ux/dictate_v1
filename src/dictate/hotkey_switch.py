"""Changing the hotkey: what is offered, what is allowed, and what he is told.

**Why a short list and not "press the keys you want".** Capturing the next
combination he presses is the obvious answer and it is the wrong one here. It
needs somewhere to say "go on then, press it" and somewhere to say "that one is
taken, try another" - and a running dictate is not allowed a dialog, because it
holds the single-instance lock and a box nobody clicks would sit there until the
next morning while `dictate run` answered "already running" (`tray.menu` and
`autostart.run_at_logon` carry that reasoning). It also has to swallow the very
keystrokes it is asking for, which is a second global hook fighting the first.
Opening `dictate.toml` at the right line is the other obvious answer, and he has
said he will not hand-edit a config file.

So: a handful of combinations that are known to work, on the menu, each one a
single click. Every keyboard one is spelled with a letter or the space bar, so
nothing depends on a key name the hotkey library may or may not know; the one he
is using now is always among them even if it is not one of these; and the item
names `dictate hotkey`, which takes anything at all, for the day none of these
suits him.

**The three mouse buttons are on the same list**, and they are on it because
this menu is now the answer to "how do I change this": a trigger he cannot pick
here is one he will not find, and he asked for the thumb button by name. Each of
them costs him something the keyboard chords do not - the button already had a
job - so each carries what it costs on the menu line itself and at length in
`MOUSE_COST`, which is the one place that text lives. `platform/mouse_trigger.py`
carries what dictate actually does to the button; this file carries what he is
told about it.

**What "it must not leave him with a hotkey that does not work" means here.**
The order is: register the new one, and only if Windows accepts it write it to
the config file. A combination another program owns fails at the register, the
old one goes straight back, and nothing has been written down. That ordering is
`app.Application.change_hotkey`'s to keep; this module decides whether the
change should be attempted at all and what he is told about it either way.

Pure, and tested in tests/test_hotkey_switch.py - and, for the mouse buttons, in
tests/test_mouse_trigger.py.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass

from .errors import ConfigError, DictateError
from .platform.hotkey_spec import describe, mouse_button, normalise

#: What the tray offers, in order, each with the reason it is on the list.
#: The keyboard ones are spelled with letters and the space bar only - see the
#: module docstring - and the mouse ones are every button dictate can hold, in
#: the order of what using one costs.
CHOICES: tuple[tuple[str, str], ...] = (
    ("ctrl + alt + space", "the one dictate starts with"),
    ("ctrl + shift + space", "when something else already owns Ctrl+Alt+Space"),
    ("ctrl + alt + d", "off the space bar, for an app that wants Space itself"),
    ("ctrl + shift + d", "the same again, if Ctrl+Alt+D is taken too"),
    ("mouse 4", "one button, no chord - a quick click still goes Back"),
    ("mouse 5", "the same, on the button browsers use for Forward"),
    ("middle mouse button", "one button - but holding it is what starts autoscroll"),
)


@dataclass(frozen=True)
class MouseCost:
    """What one mouse button is normally for, and what holding it costs.

    Written down once, here, because the tray, `dictate hotkey` and
    `dictate doctor` all say it and three copies would drift. Every sentence
    is about what he LOSES: choosing a trigger is a trade and he should be
    able to see the trade before he clicks it.
    """

    #: What a click of it normally does.
    normally: str
    #: What HOLDING it normally does - the whole of the middle button's problem.
    held: str
    #: The paragraph `dictate hotkey` prints when this is the trigger.
    cost: str


#: Push-to-talk means holding the button down for seconds at a time, so what an
#: application does with the button HELD is what decides how good a trigger it
#: is. That is the difference between the thumb buttons and the wheel.
MOUSE_COST: dict[str, MouseCost] = {
    "mouse4": MouseCost(
        normally="Back, in browsers, file managers and most editors",
        held="nothing - no common application does anything with it held down",
        cost="A quick click of Mouse 4 still goes Back: dictate holds the "
             "button back and passes the click on when you let go, a "
             "millisecond or two later than the click itself. What you lose is "
             "holding Mouse 4 for anything else, and Back inside a window "
             "running as administrator, which Windows does not let a program "
             "like dictate send a click to.",
    ),
    "mouse5": MouseCost(
        normally="Forward, in browsers and file managers",
        held="nothing - no common application does anything with it held down",
        cost="A quick click of Mouse 5 still goes Forward, a millisecond or "
             "two after you let go. It is the cheapest of the three: Forward "
             "is the least used of the buttons on a mouse. What you lose is "
             "holding Mouse 5 for anything else, and Forward inside a window "
             "running as administrator, for the same reason.",
    ),
    "mouse3": MouseCost(
        normally="open a link in a new tab, close a tab, paste in some editors",
        held="starts autoscroll - the scrolling cursor - in browsers and "
             "Windows Explorer, and pans in map, drawing and PDF applications",
        cost="A quick click of the middle button still opens the link in a new "
             "tab or closes the tab, a millisecond or two after you let go. "
             "The cost is in the holding: autoscroll is what the middle button "
             "held down means to a browser, and push-to-talk means holding it "
             "for seconds at a time. dictate swallows the button while it is "
             "running, so autoscroll does not start - but anywhere dictate's "
             "hook does not reach (a window running as administrator, a game "
             "reading the mouse directly), holding it starts autoscroll in the "
             "middle of your sentence. It is a worse fit for hold-to-talk than "
             "Mouse 4 for that reason, on top of losing new-tab and close-tab.",
    ),
}

#: Which one this project would pick, and why, said in one line wherever the
#: choice is offered. Mouse 4 because nothing anywhere does anything with it
#: HELD, and because what a click of it does - Back - is one keystroke to undo
#: (Alt+Right) if a click ever does go astray.
RECOMMENDED_MOUSE = "mouse4"

#: What the menu says for anything not on the list. It is a real command and it
#: takes any combination `hotkey_spec` can parse.
EXAMPLE_COMMAND = 'dictate hotkey "ctrl + alt + k"'


def command_for(combination: str) -> str:
    """The typed form of choosing this combination, which the menu shows."""
    return f'dictate hotkey "{combination}"'


def trigger_note(combination: str, *, keyboard_fallback: str = "",
                 click_through: bool = True) -> list[str]:
    """What has to be said about a trigger, as lines. Empty for a chord.

    A keyboard chord needs no explanation - he has been holding one for weeks.
    A mouse button does: it is a button that already had a job, and the whole
    of what dictate does to that job is here.
    """
    try:
        button = mouse_button(combination)
    except DictateError:
        return []
    if button is None:
        return []
    cost = MOUSE_COST[button]
    lines = _wrapped(
        f"{describe(button)} is held to talk. Windows reports a mouse button to "
        f"dictate only through a low-level mouse hook, so dictate installs one: "
        f"a callback of its own in the path of every mouse event on this "
        f"machine, which is why it decides and returns and does the work "
        f"elsewhere.")
    lines += ([""]
              + _wrapped(f"That button normally does: {cost.normally}.")
              + _wrapped(f"Held down, it normally: {cost.held}.")
              + [""])
    if click_through:
        lines += _wrapped(cost.cost)
    else:
        lines += _wrapped(
            f"[hotkey] mouse_click_through is false, so {describe(button)} "
            f"belongs to dictate entirely while dictate is running: a click of "
            f"it does not reach the window you clicked in at all. Set it back "
            f"to true and a click too short to be a dictation is passed on.")
    if button != RECOMMENDED_MOUSE:
        lines.append("")
        lines += _wrapped(
            f"Of the three, this project would pick "
            f"{describe(RECOMMENDED_MOUSE)}: nothing common does anything with "
            f"it HELD DOWN, which is the whole of what push-to-talk asks of a "
            f"button, and what a click of it does - Back - is one keystroke to "
            f"undo if a click ever goes astray.")
    if keyboard_fallback:
        lines.append("")
        try:
            chord = describe(keyboard_fallback)
        except DictateError:
            chord = keyboard_fallback
        lines += _wrapped(
            f"{chord} still works and always will: it is registered alongside "
            f"the mouse button, so a hook Windows refuses - or security "
            f"software removes - leaves dictate working rather than silently "
            f"doing nothing.")
    return lines


def _wrapped(text: str, width: int = 76) -> list[str]:
    return textwrap.wrap(text, width=width)


def choices_for(current: str) -> list[tuple[str, str, bool]]:
    """`(combination, why, is_the_current_one)`, current first if it is not
    already on the list.

    His own hotkey is always on the menu, ticked. A menu of four alternatives
    that does not include what he is using now cannot be read - there is nothing
    to tell him which one he has.
    """
    now = _normalised(current)
    # Compared normalised, offered as written: "ctrl + alt + space" and
    # "control + alt + space" are the same hotkey, and only one of them is what
    # anybody would want to read on a menu.
    listed = [(combo, why, bool(now) and _normalised(combo) == now)
              for combo, why in CHOICES]
    if now and not any(is_current for _, _, is_current in listed):
        listed.insert(0, (now, "the one you are using now", True))
    return listed


def _normalised(combination: str) -> str:
    """The canonical form, or `""` for anything that cannot be read.

    A hotkey that will not parse is a config he has to be told about, and he
    is - at startup, by `config.validate`. It must not also empty the tray menu,
    which is the only surface a logon-started copy has.
    """
    try:
        return normalise(combination) if combination else ""
    except DictateError:
        return ""


@dataclass(frozen=True)
class Decision:
    """Whether to try the change, and what to say when we are not going to."""

    #: The normalised combination to install. Empty when nothing is to be done.
    combination: str = ""
    level: str = "info"
    message: str = ""

    @property
    def act(self) -> bool:
        return bool(self.combination)


def decide(current: str, wanted: str, *, recording: bool) -> Decision:
    """Should this change be attempted?

    Three answers, and the two "no"s both say why in his words:

    * he is mid-sentence - the hotkey he is holding is the one being taken
      away, and taking it away now would lose what he is saying;
    * it is the hotkey he already has;
    * anything else: yes, with the combination normalised.
    """
    try:
        combination = normalise(wanted)
    except ConfigError as exc:
        return Decision(level="error", message=exc.report())
    if recording:
        return Decision(
            level="warning",
            message=(f"dictate is recording. Finish what you are saying, then "
                     f"choose {describe(combination)} again."),
        )
    try:
        if current and normalise(current) == combination:
            return Decision(
                level="info",
                message=f"The hotkey is already {describe(combination)}.",
            )
    except ConfigError:
        pass  # an unreadable current hotkey is a reason to change it, not to stop
    return Decision(combination=combination)


def applied(combination: str, *, config_path: str | None,
            persisted: bool) -> tuple[str, str]:
    """`(level, message)` for a change Windows accepted.

    Whether it survives a restart is the second sentence every time, because a
    hotkey that quietly goes back to the old one at the next logon is exactly
    the kind of thing he would spend an evening being confused by.
    """
    head = f"The hotkey is now {describe(combination)}."
    if persisted and config_path:
        return "info", f"{head} Written to {config_path}, so it stays that way."
    if config_path:
        return "warning", (f"{head} It could NOT be written to {config_path}, so "
                           f"the old one comes back next time dictate starts.")
    return "warning", (f"{head} There is no config file to write it to, so the "
                       f"old one comes back next time dictate starts - run "
                       f"`dictate init` to get one.")


def refused(combination: str, current: str, reason: str) -> str:
    """A combination Windows would not take. The old one is already back."""
    return (f"Windows would not give dictate {describe(combination)}: {reason}\n"
            f"Another program probably owns it. Nothing was changed - the "
            f"hotkey is still {describe(current)}.")
