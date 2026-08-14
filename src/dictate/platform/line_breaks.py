"""Whether dictated text is allowed to press a key rather than type a character.

**Why this module exists.** Everything dictate pastes is characters, with two
exceptions: a line break has to be a Return keypress and a tab has to be a Tab
keypress, because that is the only way to put either into another application
(`injection_plan.py`). In a terminal, a chat box, a search field or a form,
**Return submits** - so a line break in dictated text does not add a line, it
runs the command. The product owner had that happen to him without noticing at
the time, which is the worst version of it.

So the default is that dictated text cannot press either key: a line break is
delivered as a space. `[paste] line_breaks = "return"` turns the keypress back
on for someone who dictates into a document and wants "new paragraph" to work.

**This is a policy, not a repair.** The line break that produced his stray
Enters was whisper.cpp's own segment delimiter and is fixed where it comes from
(`engines/whisper_server.py`). What is here covers every other way a line break
can reach the paste - a spoken-punctuation rule, a rule he writes himself, a
future stage nobody has thought of yet - without any of them having to know.

Pure string handling, so it is tested anywhere (tests/test_line_breaks.py).
"""

from __future__ import annotations

import re

#: A line break is delivered as a space. The default.
SPACE = "space"
#: A line break is delivered as a Return keypress, which submits in a terminal,
#: a chat box and most search fields. His to choose, never dictate's.
RETURN = "return"

MODES = (SPACE, RETURN)

#: Every character that would leave the injector as a keypress instead of as a
#: character. Return and Tab are the two `plan_text` translates; the rest are
#: line breaks as far as any text control is concerned (vertical tab, form feed,
#: the file/group/record/unit separators, NEL, and the two Unicode separators),
#: and none of them has any business in a sentence somebody spoke.
_KEYPRESS = ("\t\n\v\f\r"          # tab, line feed, vertical tab, form feed, CR
             "\x1c\x1d\x1e\x85"    # the file/group/record separators, and NEL
             "\N{LINE SEPARATOR}\N{PARAGRAPH SEPARATOR}")

#: One or more of those, with whatever plain spacing surrounds them: the whole
#: run becomes a single space, so "one.\n\n  two." reads as "one. two." rather
#: than gaining the gaps the line break used to justify.
_RUN_RE = re.compile(f"[ \t]*[{_KEYPRESS}][ \t{_KEYPRESS}]*")

#: What actually presses Return. Tab is not counted: it is flattened for the
#: same reason, but it never submitted anything.
_RETURN_RE = re.compile("\r\n|[\n\r\v\f\x1c\x1d\x1e\x85"
                        "\N{LINE SEPARATOR}\N{PARAGRAPH SEPARATOR}]")


def flatten(text: str) -> str:
    """`"one.\\ntwo."` -> `"one. two."`. Every keypress character becomes a space."""
    return _RUN_RE.sub(" ", text)


def apply(text: str, mode: str) -> str:
    """The text as it will actually be delivered, under `mode`."""
    return text if mode == RETURN else flatten(text)


def returns_in(text: str) -> int:
    """How many Return keypresses delivering `text` unflattened would produce.

    CRLF counts once, because `plan_text` sends it as one Return. This is what
    the dictation history reports, so that an Enter he did not expect is
    something he can go and look up afterwards rather than something he has to
    have caught happening.
    """
    return len(_RETURN_RE.findall(text))
