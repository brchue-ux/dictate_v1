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
single click. Every one of them is spelled with a letter or the space bar, so
nothing depends on a key name the hotkey library may or may not know; the one he
is using now is always among them even if it is not one of these; and the item
names `dictate hotkey`, which takes anything at all, for the day none of these
suits him.

**What "it must not leave him with a hotkey that does not work" means here.**
The order is: register the new one, and only if Windows accepts it write it to
the config file. A combination another program owns fails at the register, the
old one goes straight back, and nothing has been written down. That ordering is
`app.Application.change_hotkey`'s to keep; this module decides whether the
change should be attempted at all and what he is told about it either way.

Pure, and tested in tests/test_hotkey_switch.py.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import ConfigError, DictateError
from .platform.hotkey_spec import describe, normalise

#: What the tray offers, in order, each with the reason it is on the list.
#: Letters and the space bar only - see the module docstring.
CHOICES: tuple[tuple[str, str], ...] = (
    ("ctrl + alt + space", "the one dictate starts with"),
    ("ctrl + shift + space", "when something else already owns Ctrl+Alt+Space"),
    ("ctrl + alt + d", "off the space bar, for an app that wants Space itself"),
    ("ctrl + shift + d", "the same again, if Ctrl+Alt+D is taken too"),
)

#: What the menu says for anything not on the list. It is a real command and it
#: takes any combination `hotkey_spec` can parse.
EXAMPLE_COMMAND = 'dictate hotkey "ctrl + alt + k"'


def command_for(combination: str) -> str:
    """The typed form of choosing this combination, which the menu shows."""
    return f'dictate hotkey "{combination}"'


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
