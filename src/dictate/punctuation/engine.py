"""Spoken punctuation: turning the word "comma" into ",".

    "hello comma world"                 ->  "hello, world"
    "that is all period the next thing" ->  "That is all. The next thing"
    "the comma goes here"               ->  "the comma goes here"   (untouched)

WHY THIS IS NOT PART OF THE CLEANUP PASS
----------------------------------------
The cleanup pass is guaranteed to only ever DELETE words, and that guarantee is
what stops a rule file being able to put words into a document that were never
spoken. Spoken punctuation SUBSTITUTES, so putting it there would mean deleting
the guarantee. `cleanup/` is untouched by this module: same schema, same
subsequence check, same place in the pipeline. This stage runs after it.

THE GUARANTEE THIS STAGE CARRIES INSTEAD
----------------------------------------
It substitutes, but only ever punctuation for a spoken mark phrase, so it still
cannot invent a word:

1. `insert` is checked at load time to contain no letters or digits, including
   for a rule the user wrote (`rules._check_insert`).
2. `apply()` re-checks the actual output with `cleanup.engine`'s OWN subsequence
   check - the same function, imported rather than copied, so there is one
   implementation of "these words were already there, in this order" in the
   product. If a rule ever breaks it the whole stage is discarded and the text
   is passed on exactly as it arrived.

Deleting the words of the mark phrase is a deletion, and adding "," adds no
word, so a correct rule always passes. Point 2 is what makes point 1
trustworthy in the presence of a user-editable data file.

MARK OR WORD
------------
"the comma goes here" must not become "the , goes here". The rule, in full, is
`_is_guarded` below: a mark phrase is a WORD when the word in front of it is one
of the file's `guard_words` - determiners and possessives, as shipped - with no
sentence break in between. It is a MARK everywhere else. Plurals never match at
all, because a phrase is matched whole-word: "commas", "periods" and "question
marks" are simply not the phrase.

That is a blunt rule and it is wrong in both directions sometimes; the cases it
gets wrong are written out in `tests/test_punctuation.py::WhereTheRuleIsWrong`
so that nobody has to discover them one at a time. The escape - saying "literal
comma" - is the way out of every one of them.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

# Imported, not copied and not moved: this is literally the check that guards
# the cleanup pass, and cleanup/engine.py is not modified by this feature.
from ..cleanup.engine import words, words_are_subsequence
from .rules import Mark, PunctuationRules

log = logging.getLogger(__name__)

#: A word, for matching a spoken phrase against Whisper's text. Same definition
#: as the cleanup engine's, so the two stages agree on what a word is.
_WORD_RE = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)

#: Punctuation a substitution absorbs from either side. Whisper writes its own
#: marks around a phrase it heard as a spoken mark, and pasting both would give
#: "Hello,, world."
_ABSORB = ".,;:!?…"

#: What a pair-closing mark absorbs to its RIGHT. A closing quote does not end
#: the sentence, so the full stop after it is Whisper's and it stays:
#: MEASURED, "he said open quote hello there close quote" comes back as
#: "He said open, quote, hello there close, quote." - the fence commas go, the
#: final full stop does not.
_ABSORB_CLAUSE = ",;:…"

#: Characters after which a mark that wants a space in front of it does not get
#: one, because they open something.
_OPENERS = "([{\"“'‘"

#: Characters that do not want a space in FRONT of them, so the space a mark
#: puts after itself is dropped when one of these is what follows. Without this
#: `he said open quote hello close quote.` ends " ."
_CLOSERS = ",.;:!?)]}\"”'’…"

#: A guard word only guards across ordinary text. "That is all. The comma is
#: next" is a new sentence, so "The" is still a guard; "all period. comma" is
#: not the shape this is about. Only these end a sentence.
_SENTENCE_END = ".!?"


@dataclass
class PunctuationResult:
    text: str
    original: str
    #: One entry per substitution, in the order they were made. Every one of
    #: them is also logged - a wrong mark has to be traceable.
    applied: list[str] = field(default_factory=list)
    #: Set when the safety check tripped and the whole stage was thrown away.
    rejected_reason: str | None = None

    @property
    def changed(self) -> bool:
        return self.text != self.original


@dataclass(frozen=True)
class _Hit:
    """One thing to splice into the text."""

    #: The mark to insert, or None for an escape ("literal comma" -> "comma").
    mark: Mark | None
    #: Character span of what is being replaced, phrase words only.
    start: int
    end: int
    #: The words that were spoken, for the log.
    spoken: str


def apply(text: str, rules: PunctuationRules) -> PunctuationResult:
    """Substitute spoken marks. Returns the text unchanged on any doubt."""
    original = text
    if not text or not text.strip() or not rules.marks:
        return PunctuationResult(text=text, original=original)

    spans = [(m.group(0).lower().replace("’", "'"), m.start(), m.end())
             for m in _WORD_RE.finditer(text)]
    hits = _find(text, spans, rules)
    if not hits:
        # The overwhelmingly common case: he did not speak a mark, so this
        # stage is not merely harmless, it is the identity.
        return PunctuationResult(text=text, original=original)

    out, applied = _splice(text, hits)

    # --- the guarantee, checked with the cleanup pass's own function -------
    if not words_are_subsequence(words(out), words(original)):
        return PunctuationResult(
            text=original,
            original=original,
            applied=[],
            rejected_reason=(
                "a spoken punctuation rule added or changed words rather than "
                "only turning a spoken mark into a mark, so the whole stage was "
                "discarded and the text was left as it was"
            ),
        )
    for line in applied:
        log.info("voice punctuation: %s", line)
    return PunctuationResult(text=out, original=original, applied=applied)


# ---------------------------------------------------------------------------
# Finding the marks
# ---------------------------------------------------------------------------


def _find(text: str, spans: list[tuple[str, int, int]],
          rules: PunctuationRules) -> list[_Hit]:
    phrases = rules.phrases()
    escape = rules.escape_word
    shared = {w for w in rules.guard_words if w}
    hits: list[_Hit] = []
    i = 0
    while i < len(spans):
        # The escape comes first: "literal comma" is the word, whatever the
        # guard rule would have said about it.
        if escape and spans[i][0] == escape:
            found = _match_at(text, spans, i + 1, phrases)
            if found is not None:
                length, mark = found
                hits.append(_Hit(mark=None, start=spans[i][1], end=spans[i + 1][1],
                                 spoken=escape))
                i += 1 + length
                continue
        found = _match_at(text, spans, i, phrases)
        if found is None:
            i += 1
            continue
        length, mark = found
        if _is_guarded(text, spans, i, mark, shared):
            i += 1
            continue
        hits.append(_Hit(mark=mark, start=spans[i][1], end=spans[i + length - 1][2],
                         spoken=" ".join(w for w, _, _ in spans[i:i + length])))
        i += length
    return hits


def _match_at(text: str, spans: list[tuple[str, int, int]], i: int,
              phrases: list[tuple[tuple[str, ...], Mark]]) -> tuple[int, Mark] | None:
    """The longest phrase starting at word `i`, or None.

    A phrase is matched WORD by word, not against the raw text, so whatever
    Whisper wrote between the words of the phrase does not stop it matching.
    That is not a nicety - MEASURED, "open quote" comes back as "open, quote,"
    and "semicolon" as "semi-colon", and a plain string search finds neither.

    The one thing that does stop it is a full stop, question mark or exclamation
    mark inside the phrase: "I have a question. Mark will answer" is two
    sentences that happen to meet at a phrase boundary, not a question mark.
    """
    for phrase, mark in phrases:  # already sorted longest first
        n = len(phrase)
        if i + n > len(spans):
            continue
        if any(spans[i + k][0] != phrase[k] for k in range(n)):
            continue
        gaps = (text[spans[i + k][2]:spans[i + k + 1][1]] for k in range(n - 1))
        if any(ch in _SENTENCE_END for gap in gaps for ch in gap):
            continue
        return n, mark
    return None


def _is_guarded(text: str, spans: list[tuple[str, int, int]], i: int,
                mark: Mark, shared: set[str]) -> bool:
    """Was this phrase spoken as a word rather than as a mark?

    Yes, when the word in front of it is a guard word - "the comma goes here",
    "set the period to five minutes", "put it on a new line" - and nothing
    ending a sentence stands between the two.
    """
    if i == 0:
        return False
    previous, _, prev_end = spans[i - 1]
    if previous not in shared and previous not in mark.guard_words:
        return False
    between = text[prev_end:spans[i][1]]
    return not any(ch in _SENTENCE_END for ch in between)


# ---------------------------------------------------------------------------
# Putting the marks in, with the right spacing
# ---------------------------------------------------------------------------


def _splice(text: str, hits: list[_Hit]) -> tuple[str, list[str]]:
    out: list[str] = []
    applied: list[str] = []
    pos = 0
    #: Emitted before the next chunk, not after the mark, so a mark at the very
    #: end of the text does not leave a trailing space.
    pending_sep = ""
    pending_capital = False

    for hit in hits:
        left, right = _absorb(text, hit, floor=pos)
        chunk = text[pos:left]
        pos = right

        if chunk:
            if pending_capital:
                chunk = _capitalize(chunk)
                pending_capital = False
            out.append(_join(pending_sep if out else "", chunk))
            pending_sep = ""

        if hit.mark is None:
            # The escape: the escape word itself goes, the phrase after it stays
            # a phrase. A space takes the escape word's place so "the literal
            # comma" does not come out as "thecomma".
            pending_sep = " "
            applied.append(f'"{hit.spoken} ..." escape: the words after it were '
                           f"left as words")
            continue

        before, after = _separators(hit.mark.spacing)
        if before and not _needs_no_space(out):
            out.append(before)
        out.append(hit.mark.insert)
        pending_sep = after
        pending_capital = hit.mark.capitalize_next
        applied.append(_describe(text, hit))

    tail = text[pos:]
    if tail:
        if pending_capital:
            tail = _capitalize(tail)
        out.append(_join(pending_sep if out else "", tail))
    return "".join(out), applied


def _absorb(text: str, hit: _Hit, *, floor: int) -> tuple[int, int]:
    """Widen the replaced span over the whitespace and punctuation around it.

    Whitespace always goes, on both sides: `_separators` puts back exactly the
    spacing the mark wants, so "hello comma world" cannot come out as
    "hello , world".

    Whisper's own punctuation goes too, and that is the part that matters.
    MEASURED on large-v3-turbo (see the PR): a dictated mark comes back
    punctuated as well as spelled out - "hello comma world" is transcribed
    "Hello, comma, world." and "are you sure question mark" is transcribed "Are
    you sure? Question mark." Leaving those in would paste "Hello,, world."
    He said which mark he wanted, so in that one spot his beats Whisper's.

    The left side keeps its punctuation for an OPENING mark, which is what makes
    `He said, open quote hello` come out as `He said, "hello` - MEASURED: that
    comma is Whisper's own, not a fence, and dropping it would be worse English.
    """
    left, right = hit.start, hit.end
    if hit.mark is not None and hit.mark.spacing != "before":
        while left > floor and (text[left - 1] in _ABSORB or text[left - 1] in " \t"):
            left -= 1
    else:
        while left > floor and text[left - 1] in " \t":
            left -= 1
    n = len(text)
    take_right = "" if hit.mark is None else (
        _ABSORB_CLAUSE if hit.mark.spacing == "close" else _ABSORB)
    while right < n and (text[right] in take_right or text[right] in " \t"):
        right += 1
    return left, right


def _join(sep: str, chunk: str) -> str:
    """`sep` unless what follows it does not want a space in front of it."""
    if sep == " " and chunk and chunk[0] in _CLOSERS:
        return chunk
    return sep + chunk


def _separators(spacing: str) -> tuple[str, str]:
    """(what goes before the mark, what goes after it)."""
    return {
        "after": ("", " "),
        "close": ("", " "),
        "before": (" ", ""),
        "around": (" ", " "),
        "break": ("", ""),
    }[spacing]


def _needs_no_space(out: list[str]) -> bool:
    """True at the very start of the text, or right after whitespace or an
    opening character - none of which want another space in front of a mark."""
    for part in reversed(out):
        if part:
            return part[-1].isspace() or part[-1] in _OPENERS
    return True


def _capitalize(chunk: str) -> str:
    """Put a capital on the first letter of the next sentence, the way Whisper
    would have written it had it heard the full stop rather than the word."""
    for i, ch in enumerate(chunk):
        if ch.isspace():
            continue
        return chunk[:i] + ch.upper() + chunk[i + 1:] if ch.islower() else chunk
    return chunk


#: Characters of Whisper's text shown either side of a substitution in the log.
_CONTEXT = 28


def _describe(text: str, hit: _Hit) -> str:
    """One traceable line: what was said, what went in, and where.

    Without this a wrong mark is a mystery - the word he spoke is gone from the
    output, so the pasted text cannot be read backwards to find it. The context
    is Whisper's text verbatim, punctuation and all, because Whisper's own
    fencing is usually the thing that explains a surprising result.

    Deliberately ASCII. This line goes to the console as well as to the log, and
    a Windows console redirected to a file encodes with the local code page, not
    UTF-8 - a decorative bracket there is a UnicodeEncodeError in the middle of
    `dictate punctuate --explain > out.txt`.
    """
    lo, hi = max(0, hit.start - _CONTEXT), min(len(text), hit.end + _CONTEXT)
    context = (("..." if lo else "") + text[lo:hit.start]
               + "[[" + text[hit.start:hit.end] + "]]"
               + text[hit.end:hi] + ("..." if hi < len(text) else ""))
    return (f'"{hit.spoken}" -> {hit.mark.insert!r} ({hit.mark.name}) in '
            f"{context.replace(chr(10), ' ')!r}")
