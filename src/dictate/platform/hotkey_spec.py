"""Parsing and normalising the hotkey the user wrote in the config file.

Pure string handling, so it is tested on Linux (tests/test_hotkey_spec.py) even
though the listener it feeds only runs on Windows. This is the code most likely
to be exercised by a typo in a config file, so it gets a real error message
rather than a KeyError.
"""

from __future__ import annotations

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


def normalise(combination: str) -> str:
    """`"Ctrl + Alt + Space"` -> `"control + alt + space"`.

    Raises `ConfigError` with something actionable if the combination is empty
    or has nothing but modifiers in it.
    """
    parts = [p.strip().lower() for p in combination.replace("-", "+").split("+")]
    parts = [p for p in parts if p]
    if not parts:
        raise ConfigError(
            f"The hotkey {combination!r} has no keys in it.",
            'Write something like combination = "ctrl + alt + space".',
        )
    keys = [_ALIASES.get(p, p) for p in parts]

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
            "Push-to-talk holds one key, optionally with modifiers.",
        )
    # Modifiers first, in a stable order, so the listener sees a canonical form.
    order = {"control": 0, "alt": 1, "shift": 2, "window": 3}
    mods.sort(key=lambda m: order[m])
    return " + ".join(mods + rest)


def describe(combination: str) -> str:
    """A human-facing rendering: `"Ctrl + Alt + Space"`."""
    pretty = {"control": "Ctrl", "alt": "Alt", "shift": "Shift", "window": "Win"}
    parts = normalise(combination).split(" + ")
    return " + ".join(pretty.get(p, p.replace("_", " ").title()) for p in parts)
