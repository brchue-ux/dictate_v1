"""Parsing and normalising the hotkey the user wrote in the config file.

Pure string handling, so it is tested on Linux (tests/test_units.py,
tests/test_mouse_trigger.py) even though the listeners it feeds only run on
Windows. This is the code most likely to be exercised by a typo in a config
file, so it gets a real error message rather than a KeyError.

**A trigger is either a keyboard chord or one mouse button, never both.** The
chord is the original: modifiers plus one key, held. The mouse button is what
the product owner asked for next - "I don't like having to press three buttons.
Can I just press mouse four?" - and it is deliberately a button ON ITS OWN. A
"ctrl + mouse 4" would be the three-button chord he is trying to get away from,
and it would also make the low-level mouse hook read the keyboard to find out
whether Ctrl was down, which is a second source of truth for modifier state
inside a callback that must not do any work. So it is refused, by name, with the
reason.
"""

from __future__ import annotations

import re

from ..errors import ConfigError

#: Everything a person might reasonably type -> what the listener expects.
_ALIASES = {
    "ctrl": "control",
    "control": "control",
    "ctl": "control",
    "alt": "alt",
    "option": "alt",
    "opt": "alt",
    "shift": "shift",
    "win": "window",
    "windows": "window",
    "super": "window",
    "cmd": "window",
    "meta": "window",
    "window": "window",
    "space": "space",
    "spacebar": "space",
    "esc": "escape",
    "escape": "escape",
    "return": "enter",
    "enter": "enter",
    "caps": "caps_lock",
    "capslock": "caps_lock",
    "caps_lock": "caps_lock",
}

MODIFIERS = {"control", "alt", "shift", "window"}

#: The mouse buttons dictate will hold, canonical name -> what it is called on
#: screen. The numbering is the conventional one - 1 left, 2 right, 3 the wheel
#: pressed in, 4 and 5 the two thumb buttons - which is why "mouse 4" is the one
#: browsers use for Back.
MOUSE_BUTTONS: dict[str, str] = {
    "mouse3": "Middle mouse button",
    "mouse4": "Mouse 4",
    "mouse5": "Mouse 5",
}

#: How each one is written in a config file. The canonical form ("mouse3") is
#: what dictate stores and compares; this is what a message tells him to type.
SPELT_MOUSE: dict[str, str] = {
    "mouse3": "middle mouse button",
    "mouse4": "mouse 4",
    "mouse5": "mouse 5",
}

#: Said in every refusal, so a wrong value names the right ones.
ACCEPTED_MOUSE = '"mouse 4", "mouse 5" or "middle mouse button"'

#: Spellings collapsed BEFORE the combination is split, because the splitter
#: treats "-" as a separator: without this "mouse-4" would arrive as two keys,
#: and "middle click" as one key nothing recognises.
_SPELLINGS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\bmouse\s*[-_ ]?\s*(\d+)\b"), r"mouse\1"),
    (re.compile(r"(?i)\bx\s*[-_ ]?\s*button\s*[-_ ]?\s*(\d+)\b"), r"xbutton\1"),
    (re.compile(r"(?i)\b(?:middle|(?:scroll\s*|mouse\s*)?wheel)"
                r"(?:\s*[-_ ]?\s*(?:mouse\s*)?(?:click|button))?\b"), "mouse3"),
    (re.compile(r"(?i)\bleft\s*[-_ ]?\s*(?:mouse\s*)?(?:click|button)\b"), "mouse1"),
    (re.compile(r"(?i)\bright\s*[-_ ]?\s*(?:mouse\s*)?(?:click|button)\b"), "mouse2"),
)

#: Spelling -> canonical mouse name, compared with the spaces and underscores
#: taken out ("x button 1", "XBUTTON1" and "xbutton_1" are one thing).
_MOUSE_ALIASES = {
    "mouse3": "mouse3", "mb3": "mouse3", "mmb": "mouse3", "button3": "mouse3",
    "mouse4": "mouse4", "mb4": "mouse4", "button4": "mouse4",
    "xbutton1": "mouse4", "x1": "mouse4",
    "mouse5": "mouse5", "mb5": "mouse5", "button5": "mouse5",
    "xbutton2": "mouse5", "x2": "mouse5",
}

#: The two dictate will not take, and what they are called. Holding the trigger
#: means SWALLOWING it, and a machine whose left or right button dictate is
#: eating is a machine nobody can use - including to turn dictate off again.
_REFUSED_MOUSE = {
    "mouse1": "the left mouse button", "lmb": "the left mouse button",
    "button1": "the left mouse button",
    "mouse2": "the right mouse button", "rmb": "the right mouse button",
    "button2": "the right mouse button",
}

#: Anything shaped like a mouse button that got this far is a value nobody can
#: use, and it says so rather than being passed to a keyboard listener that
#: would fail later with a Windows error code.
_MOUSE_SHAPED = re.compile(r"(?i)^(?:mouse|xbutton|mb|button)\d*$")


def normalise(combination: str) -> str:
    """`"Ctrl + Alt + Space"` -> `"control + alt + space"`, `"Mouse 4"` ->
    `"mouse4"`.

    Raises `ConfigError` with something actionable if the combination is empty,
    has nothing but modifiers in it, or names a mouse button dictate will not
    take.
    """
    text = combination
    for pattern, replacement in _SPELLINGS:
        text = pattern.sub(replacement, text)
    parts = [p.strip().lower() for p in text.replace("-", "+").split("+")]
    parts = [p for p in parts if p]
    if not parts:
        raise ConfigError(
            f"The hotkey {combination!r} has no keys in it.",
            'Write something like combination = "ctrl + alt + space", or '
            'combination = "mouse 4" for a mouse button.',
        )
    keys = [_key_for(p) for p in parts]

    seen: list[str] = []
    for key in keys:
        if key not in seen:
            seen.append(key)
    mods = [k for k in seen if k in MODIFIERS]
    rest = [k for k in seen if k not in MODIFIERS]
    if not rest:
        raise ConfigError(
            f"The hotkey {combination!r} is only modifier keys.",
            "Add a normal key to hold as well, e.g. "
            '"ctrl + alt + space". A modifier-only hotkey would fire every time '
            "you used a keyboard shortcut.",
        )
    if len(rest) > 1:
        raise ConfigError(
            f"The hotkey {combination!r} has more than one non-modifier key "
            f"({', '.join(rest)}).",
            "Push-to-talk holds one key, optionally with modifiers - or one "
            "mouse button on its own.",
        )
    _refuse_impossible_mouse(combination, rest[0])
    if rest[0] in MOUSE_BUTTONS:
        if mods:
            raise ConfigError(
                f"The hotkey {combination!r} asks for modifier keys and a mouse "
                f"button together.",
                f"A mouse button is held on its own - that is the point of it. "
                f'Write combination = "{SPELT_MOUSE[rest[0]]}".',
            )
        return rest[0]
    # Modifiers first, in a stable order, so the listener sees a canonical form.
    order = {"control": 0, "alt": 1, "shift": 2, "window": 3}
    mods.sort(key=lambda m: order[m])
    return " + ".join(mods + rest)


def _key_for(part: str) -> str:
    """One written key -> what the listener expects.

    Mouse names are compared with their spaces and underscores removed, which
    is what lets "middle mouse button" survive being one part of a split.
    """
    compact = part.replace(" ", "").replace("_", "")
    mouse = _MOUSE_ALIASES.get(compact)
    if mouse is not None:
        return mouse
    if compact in _REFUSED_MOUSE or _MOUSE_SHAPED.match(compact):
        return compact
    return _ALIASES.get(part, part)


def _refuse_impossible_mouse(combination: str, key: str) -> None:
    """Everything mouse-shaped that dictate will not hold, with the reason.

    Left and right have a reason of their own; the rest are simply not buttons
    Windows reports separately - a low-level mouse hook can tell dictate about
    the middle button and the two thumb buttons, and about nothing else.
    """
    refused = _REFUSED_MOUSE.get(key)
    if refused:
        raise ConfigError(
            f"The hotkey {combination!r} asks for {refused}.",
            f"dictate will not take it: holding a button to talk means "
            f"swallowing it, and a computer whose {refused.replace('the ', '')} "
            f"does nothing cannot be used - including to turn dictate off "
            f"again. Use {ACCEPTED_MOUSE}.",
        )
    if key in MOUSE_BUTTONS or not _MOUSE_SHAPED.match(key):
        return
    raise ConfigError(
        f"The hotkey {combination!r} is not a mouse button dictate can hold.",
        f"Windows reports three buttons to dictate besides left and right: "
        f"{ACCEPTED_MOUSE}. Keyboard combinations still work too, e.g. "
        f'"ctrl + alt + space".',
    )


def mouse_button(combination: str) -> str | None:
    """The canonical mouse name (`"mouse4"`) if this trigger is a mouse button,
    or `None` if it is a keyboard chord. Raises like `normalise` on nonsense."""
    key = normalise(combination)
    return key if key in MOUSE_BUTTONS else None


def is_mouse(combination: str) -> bool:
    """`True` for a mouse trigger, `False` for a chord, `False` for anything
    that cannot be read - the caller that cares about the third case calls
    `normalise` and gets the message."""
    try:
        return mouse_button(combination) is not None
    except ConfigError:
        return False


def describe(combination: str) -> str:
    """A human-facing rendering: `"Ctrl + Alt + Space"`, `"Mouse 4"`."""
    pretty = {"control": "Ctrl", "alt": "Alt", "shift": "Shift", "window": "Win"}
    normalised = normalise(combination)
    if normalised in MOUSE_BUTTONS:
        return MOUSE_BUTTONS[normalised]
    parts = normalised.split(" + ")
    return " + ".join(pretty.get(p, p.replace("_", " ").title()) for p in parts)
