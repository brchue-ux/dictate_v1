"""Which font the overlay actually got, as opposed to which one it asked for.

Tk does not fail when a font family is missing. It picks something else and says
nothing, so an overlay configured for a font nobody installed looks wrong for a
reason that is invisible from the config file and invisible in the log. That is
the whole problem this module exists to solve: ask for a family, find out what
really resolved, and if it is not what was asked for, choose the fallback
deliberately and **say so on startup**.

The lookup itself is Tk's (`tkinter.font.Font(...).actual("family")`), which is
Windows-only in practice. It arrives here as a callable, so the decision - which
is the part that can be wrong - is testable on any machine.

Family names are compared case-insensitively and ignoring spaces and hyphens,
because Tk normalises differently on different builds: "Fira Code", "fira code"
and "FiraCode" are the same family and a mismatch between them is not a
substitution.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

#: Tried in order when the configured family does not resolve. Ordinary Windows
#: installs have all of these; Consolas is first because the configured default is
#: a monospace font and the substitute should be one too.
FALLBACKS: tuple[str, ...] = ("Consolas", "Cascadia Mono", "Segoe UI", "Arial")


def normalise(family: str) -> str:
    return family.replace(" ", "").replace("-", "").replace("_", "").casefold()


@dataclass(frozen=True)
class FontChoice:
    """The family to actually use, and whether anybody needs to be told."""

    family: str
    #: What the config asked for.
    requested: str
    #: True when `requested` really is what Tk resolved.
    resolved: bool
    #: What Tk substituted when it did not. Empty when it did.
    substituted: str = ""

    @property
    def ok(self) -> bool:
        return self.resolved

    def report(self) -> str:
        """One line for the startup output. Empty when there is nothing to say."""
        if self.resolved:
            return ""
        if self.family == self.requested:
            return (
                f'the caption font "{self.requested}" is not installed, and no '
                f"fallback resolved either - Windows substituted "
                f'"{self.substituted}". Captions will not look as intended. '
                f"Install {self.requested}, or set [overlay] font_family to a "
                f"font you have."
            )
        return (
            f'the caption font "{self.requested}" is not installed - Windows '
            f'substituted "{self.substituted}", so dictate is using '
            f'"{self.family}" instead. Install {self.requested}, or set '
            f"[overlay] font_family to the font you want."
        )


def choose_font(
    requested: str,
    resolve: Callable[[str], str],
    fallbacks: Sequence[str] = FALLBACKS,
) -> FontChoice:
    """Pick the family to use, given a way to ask what a family resolves to.

    `resolve(family)` returns the family Tk actually produced for that request.
    Anything it raises is treated as "that family did not resolve", because a
    fallback chain that dies on a bad name is worse than one that moves on.
    """
    requested = requested.strip()
    if not requested:
        requested = fallbacks[0] if fallbacks else "Segoe UI"

    actual = _try(requested, resolve)
    if actual is not None and normalise(actual) == normalise(requested):
        return FontChoice(family=requested, requested=requested, resolved=True)

    substituted = actual or ""
    for candidate in fallbacks:
        if normalise(candidate) == normalise(requested):
            continue
        got = _try(candidate, resolve)
        if got is not None and normalise(got) == normalise(candidate):
            return FontChoice(family=candidate, requested=requested,
                              resolved=False, substituted=substituted)

    # Nothing resolved. Ask for the configured family anyway and let Tk
    # substitute - the alternative is refusing to draw captions at all, which is
    # a worse answer than captions in the wrong font, as long as it is said out
    # loud. `report()` covers this case separately.
    return FontChoice(family=requested, requested=requested, resolved=False,
                      substituted=substituted)


def _try(family: str, resolve: Callable[[str], str]) -> str | None:
    try:
        got = resolve(family)
    except Exception:
        return None
    return got.strip() if isinstance(got, str) and got.strip() else None
