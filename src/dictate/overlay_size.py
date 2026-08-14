"""How big the caption panel is, how big its words are, and what they are set in.

**Three controls, and why they are not five pixel values.** Every measurement in
`[overlay]` is a pixel number, and they only look right in relation to each
other: shrink the type and leave the padding and the width alone and you get a
small line of text floating in a large slab. Asking anyone to keep five numbers
consistent by hand is asking them to do a designer's arithmetic every time they
want the captions a bit smaller, and the product owner said plainly he will not
remember fiddly steps. So there are three things to change, all of them one
word:

* **`size`** - the whole panel, type and box together. The one knob, and the
  one that is right nearly always.
* **`text_size`** - the words on their own, when he wants them bigger or
  smaller without the box following.
* **`panel_size`** - the box on its own: the width, the margin off the screen
  edge, the padding and the shoulder, without the words following.

`font_family` is the fourth thing he can change and needs nothing new here: it
was always one word in the config, and `dictate overlay --font` now tries one
without editing anything (`platform/fonts.py` says out loud when Windows
substitutes a family that is not installed).

**What the names mean.** The pixel values in `[overlay]` describe the panel at
`huge`; every other size is a fraction of that, in eighths. The two overrides
are empty by default, which means "follow `size`" - at which point the whole
design is self-similar and the text column works out at about 60 characters a
line at *every* rung, because the width and the type move together.

**When he moves one on its own**, two floors in `platform/geometry.py` keep it
coherent, and both are the words winning over the box: padding may not fall
below a share of the type it surrounds, and the panel may not be made so narrow
that the `max_chars` of caption it is sent no longer fit on the lines it
reserves - which would silently clip the newest words off the bottom, while he
is still speaking. So `panel_size` shrinks the box down to that point and no
further, and `text_size` grows the words with the box following only as far as
it must. Neither floor moves anything at all while the two agree.

**What this module is not.** It is not the DPI scaling. That is a separate
multiplication, done per monitor, per appearance, in `platform/geometry.py`, and
all of this sits on top of it: a `compact` panel on a 150% display is still 150%
of a `compact` panel. Everything here is plain Python and tested anywhere
(`tests/test_overlay_size.py`).
"""

from __future__ import annotations

from pathlib import Path

from . import config_edit
from .errors import ConfigError

#: Name -> what it multiplies the `[overlay]` pixel values by. In order, small
#: to large, because "one step bigger" has to mean something. Eighths: each rung
#: is a 3-pixel step in caption text at 100% scaling (12, 15, 18, 21, 24), which
#: is a step you can see rather than one you have to measure.
SIZES: dict[str, float] = {
    "small": 0.5,
    "compact": 0.625,
    "medium": 0.75,
    "large": 0.875,
    "huge": 1.0,
}

#: The shipped size. `huge` is what the panel used to be, and on the product
#: owner's display it was too big by about the amount this is smaller by: the
#: caption goes from 24 px to 15 px at 100% scaling, and the panel from
#: 1080x136 to 675x83 - a bit over a third of the screen area it used to cover.
DEFAULT = "compact"

#: What the two overrides say when they are not overriding anything.
FOLLOW = ""

#: What `dictate look` accepts instead of a name, so that "a bit less" is a word
#: rather than a lookup.
STEPS = {"smaller": -1, "bigger": 1}

#: The keys this module knows how to write, in the order they are shown.
KEYS = ("size", "text_size", "panel_size", "font_family")


def names() -> list[str]:
    """The ladder, smallest first."""
    return list(SIZES)


def multiplier(name: str, *, key: str = "size") -> float:
    """What `name` multiplies the `[overlay]` pixel values by."""
    try:
        return SIZES[name]
    except KeyError:
        raise ConfigError(
            f"[overlay] {key} is {name!r}, which is not one of the caption sizes.",
            "Use one of: " + ", ".join(names()) + f". {DEFAULT} is the default.",
        ) from None


def effective(size: str, text_size: str = FOLLOW,
              panel_size: str = FOLLOW) -> tuple[str, str]:
    """`(text, panel)` - the size name in force for each, overrides applied.

    An empty override means "follow `size`", which is what makes a config file
    written before any of this existed, or one that only ever sets the one knob,
    behave like the coherent design rather than like a special case.
    """
    return (text_size or size), (panel_size or size)


def multipliers(size: str, text_size: str = FOLLOW,
                panel_size: str = FOLLOW) -> tuple[float, float]:
    """`(text, panel)` as multipliers, ready for `geometry.plan_slab`."""
    text, panel = effective(size, text_size, panel_size)
    return multiplier(text, key="text_size"), multiplier(panel, key="panel_size")


def caption_px(name: str, font_size: float = 18.0) -> int:
    """Roughly what the caption text measures at `name`, at 100% scaling.

    Shown to the user rather than used to lay anything out: a number in pixels
    is easier to judge "is that smaller than I asked for" against than a
    multiplier is. The real one comes from `platform.geometry`.
    """
    return max(6, round(font_size * multiplier(name) * 96 / 72))


def step(name: str, delta: int) -> str:
    """One rung `delta` steps from `name`, stopping at the ends of the ladder."""
    ladder = names()
    index = ladder.index(name) if name in ladder else ladder.index(DEFAULT)
    return ladder[max(0, min(len(ladder) - 1, index + delta))]


def resolve(word: str) -> tuple[str, str]:
    """`(word, kind)` for what someone typed: a size name, or bigger/smaller.

    `kind` is "name" or "step", which is what tells the caller whether it has a
    size in its hand or a direction to move in.
    """
    word = word.strip().lower()
    if word in STEPS:
        return word, "step"
    if word in SIZES:
        return word, "name"
    raise ConfigError(
        f"{word!r} is not a caption size.",
        "Use one of: " + ", ".join(names()) + ", or `smaller` / `bigger` to "
        "move one step from where you are.",
    )


def apply_word(word: str, current: str) -> str:
    """The size `word` asks for, given where things are now."""
    value, kind = resolve(word)
    return step(current, STEPS[value]) if kind == "step" else value


def ladder(text: str, panel: str, font_size: float = 18.0) -> list[str]:
    """The whole ladder as lines, with the two knobs marked on it.

    Printed by `dictate overlay` and `dictate look`: seeing the rungs either
    side of where he is now is the whole difference between "change it" and
    "work out what to change it to". When the two agree - which is the shipped
    state - there is one mark, not two.
    """
    lines = []
    for name in names():
        marks = [what for what, value in (("text", text), ("panel", panel))
                 if value == name]
        mark = "<- " + " and ".join(marks) if marks else ""
        lines.append(f"  {'>' if marks else ' '} {name:<8} "
                     f"caption text about {caption_px(name, font_size):>2} px  {mark}".rstrip())
    return lines


# ---------------------------------------------------------------------------
# Writing it back to his config file
#
# Through `config_edit`, which is the one writer: it changes the line and leaves
# every other byte - his comments, his CRLF endings, his byte order mark - the
# byte it was, and it is what the tray's hotkey item already uses. A second
# implementation of "edit one line of his TOML" is how two ideas of what is safe
# start to drift apart.
# ---------------------------------------------------------------------------

#: The section all of this lives in, and the keys this module will write. A key
#: that is not one of these is a programming error, not a thing to guess at.
SECTION = "overlay"


def _known(values: dict[str, str]) -> None:
    unknown = sorted(set(values) - set(KEYS))
    if unknown:
        raise ConfigError(
            f"dictate does not write {', '.join(unknown)} for you.",
            "Only " + ", ".join(KEYS) + " can be changed this way.",
        )


def set_in_text(text: str, values: dict[str, str]) -> str:
    """`text` with each of `values` set in `[overlay]`. Nothing else is touched.

    Written last-listed first, because `config_edit` adds a key it cannot find
    at the *top* of the section: doing it in this order leaves a file that lists
    them in `KEYS` order, whichever order he typed the flags in.
    """
    _known(values)
    for key in reversed(KEYS):
        if key in values:
            text = config_edit.set_string(text, SECTION, key, values[key])
    return text


def write(path: Path, values: dict[str, str]) -> None:
    """Set `values` in the config file at `path`, one setting at a time.

    One `os.replace` per setting rather than one for all of them, because that
    is what `config_edit` offers and a second writer is not worth having: every
    intermediate state is a valid config file that says something true, so an
    interrupted `dictate look medium` leaves a smaller panel and not a broken
    one.
    """
    _known(values)
    for key in reversed(KEYS):        # see `set_in_text`, for the same reason
        if key in values:
            config_edit.write_string(Path(path), SECTION, key, values[key])
