"""Turning text into a list of synthetic key events.

Separated from the ctypes call for the same reason as `geometry.py`: the tricky
parts - UTF-16 surrogate pairs for characters outside the Basic Multilingual
Plane, and the fact that a newline has to be a Return keypress rather than a
Unicode character - are pure data transformations, so they are tested
(tests/test_units.py) on a machine with no Windows.

**A keypress has to be asked for.** `plan_text` will not emit Return or Tab
unless the caller passes `allow_return=True`; with the default it flattens them
to spaces (`line_breaks.py`) and the plan is characters and nothing else. That
is deliberately a property of this function rather than a check the caller is
trusted to have done first, because Return in a terminal runs the command line
and the product owner had that happen to him without noticing at the time. The
policy lives in `line_breaks.py` and is decided by `[paste] line_breaks`; this
is the wall it is enforced against.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import line_breaks

#: Windows virtual key codes, repeated here so this module needs no ctypes.
VK_RETURN = 0x0D
VK_TAB = 0x09


@dataclass(frozen=True)
class KeyEvent:
    #: "unicode" -> send `code` as a UTF-16 code unit with KEYEVENTF_UNICODE.
    #: "vk"      -> send `code` as a virtual key.
    kind: str
    code: int
    up: bool


def plan_text(text: str, *, allow_return: bool = False) -> list[KeyEvent]:
    """Key events that reproduce `text` in the focused window.

    Every character is sent as a Unicode code unit, which means the target
    application receives exactly the character regardless of the user's keyboard
    layout - a layout-dependent scancode approach would mangle punctuation on a
    non-US keyboard.

    `allow_return` is the only way to get a Return or a Tab out of this
    function. Without it a line break is flattened to a space before anything is
    planned, so no plan this returns can submit a form, send a chat message or
    run a command line - whatever the text turns out to contain, and whoever put
    it there. See the module docstring.
    """
    if not allow_return:
        text = line_breaks.flatten(text)
    events: list[KeyEvent] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\r":
            # Normalise CRLF to a single Return press.
            if i + 1 < len(text) and text[i + 1] == "\n":
                i += 1
            events += _vk(VK_RETURN)
        elif ch == "\n":
            events += _vk(VK_RETURN)
        elif ch == "\t":
            events += _vk(VK_TAB)
        else:
            for unit in _utf16_units(ch):
                events.append(KeyEvent("unicode", unit, False))
                events.append(KeyEvent("unicode", unit, True))
        i += 1
    return events


def _vk(code: int) -> list[KeyEvent]:
    return [KeyEvent("vk", code, False), KeyEvent("vk", code, True)]


def _utf16_units(ch: str) -> list[int]:
    """One code unit for a BMP character, a surrogate pair for anything above.

    SendInput's KEYEVENTF_UNICODE carries a 16-bit value, so an emoji has to go
    as two events - high surrogate then low surrogate.
    """
    cp = ord(ch)
    if cp <= 0xFFFF:
        return [cp]
    cp -= 0x10000
    return [0xD800 + (cp >> 10), 0xDC00 + (cp & 0x3FF)]


def chunk(events: list[KeyEvent], size: int = 200) -> list[list[KeyEvent]]:
    """Split into SendInput-sized batches.

    One SendInput call per character is needlessly slow for a paragraph of
    dictation, and one call for the whole thing risks a very large array; a few
    hundred events per call is the usual middle ground.
    """
    if size < 1:
        raise ValueError("size must be at least 1")
    return [events[i:i + size] for i in range(0, len(events), size)]
