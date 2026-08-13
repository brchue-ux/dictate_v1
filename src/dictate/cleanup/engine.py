"""Rule-based cleanup.

What it does: strips filler words and phrases, collapses doubled words, collapses
verbatim mid-sentence restarts, and tidies the punctuation those deletions leave
behind.

What it deliberately does NOT do: punctuate or capitalise from scratch. Whisper
already emits both, and re-doing them would only make the output worse.

The hard guarantee - it never invents words - is enforced twice:

1. Every rule is a *deletion*. The rule schema has no replacement field
   (`rules.py`), and the built-in transformations only remove spans.
2. `clean()` verifies the guarantee on the actual result: the sequence of words
   in the output must be a subsequence of the words in the input. If any rule
   (including one the user wrote) breaks that, the whole cleanup is discarded
   and the original Whisper text is returned untouched.

Rule 2 is what makes rule 1 trustworthy in the presence of a user-editable
regex file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .rules import CleanupRules

#: A "word" for the purposes of the safety check: letters/digits with optional
#: internal apostrophes. Unicode-aware, so accented words count as words.
_WORD_RE = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)

#: Bounded so a pathological rule set cannot spin. Each pass strictly shrinks
#: the text, so this is a safety net rather than the normal exit condition.
_MAX_PASSES = 8


@dataclass
class CleanResult:
    text: str
    original: str
    applied: list[str] = field(default_factory=list)
    #: Set when the safety check tripped and the cleanup was thrown away.
    rejected_reason: str | None = None

    @property
    def changed(self) -> bool:
        return self.text != self.original


def words(text: str) -> list[str]:
    """The word sequence used by the safety check: lowercased, punctuation-free."""
    return [m.group(0).lower().replace("’", "'") for m in _WORD_RE.finditer(text)]


def words_are_subsequence(candidate: list[str], source: list[str]) -> bool:
    """True if every word of `candidate` appears in `source`, in order.

    This is the machine-checked form of "cleanup may only delete". Note it is
    case-insensitive, so re-capitalising a word after deleting the filler in
    front of it is allowed; adding, reordering or altering a word is not.
    """
    it = iter(source)
    return all(w in it for w in candidate)


def clean(text: str, rules: CleanupRules) -> CleanResult:
    """Apply the rules. Returns the original text unchanged on any doubt."""
    original = text
    if not text or not text.strip():
        return CleanResult(text="", original=original)

    applied: list[str] = []
    out = text

    # Whisper occasionally emits its own leading/trailing space and newlines.
    out = out.strip()

    if rules._phrase_re is not None:
        out, n = rules._phrase_re.subn("", out)
        if n:
            applied.append(f"filler phrases x{n}")
    if rules._filler_re is not None:
        out, n = rules._filler_re.subn("", out)
        if n:
            applied.append(f"filler words x{n}")
    for rule in rules.deletions:
        out, n = rule.regex.subn("", out)
        if n:
            applied.append(f"{rule.name} x{n}")

    if rules.collapse_repeated_phrases:
        out, n = _collapse_repeated_phrases(out, rules.max_repeat_phrase_words)
        if n:
            applied.append(f"repeated phrases x{n}")
    if rules.collapse_doubled_words:
        exceptions = {w.lower() for w in rules.doubled_word_exceptions}
        out, n = _collapse_doubled_words(out, exceptions)
        if n:
            applied.append(f"doubled words x{n}")

    if rules.repair_punctuation:
        repaired = _repair_punctuation(out)
        if repaired != out:
            applied.append("punctuation tidy")
        out = repaired

    out = re.sub(r"[ \t]{2,}", " ", out).strip()

    if rules.recapitalize_sentences:
        recased = _recapitalize(out, original)
        if recased != out:
            applied.append("sentence capitals")
        out = recased

    # Every word was a filler and only punctuation is left. Pasting a lone full
    # stop into his document is worse than pasting nothing, and the pipeline
    # already has a "did not hear any words in that" path for exactly this.
    if out and not words(out):
        applied.append("nothing but punctuation left")
        out = ""

    # --- the guarantee ------------------------------------------------
    if not words_are_subsequence(words(out), words(original)):
        return CleanResult(
            text=original.strip(),
            original=original,
            applied=[],
            rejected_reason=(
                "a cleanup rule changed or added words rather than only removing "
                "them, so the cleanup was discarded and the transcript was pasted "
                "exactly as Whisper produced it"
            ),
        )
    return CleanResult(text=out, original=original, applied=applied)


# ---------------------------------------------------------------------------
# Built-in transformations. Every one of these deletes a span and nothing else.
# ---------------------------------------------------------------------------


def _word_spans(text: str) -> list[tuple[str, int, int]]:
    return [(m.group(0).lower().replace("’", "'"), m.start(), m.end())
            for m in _WORD_RE.finditer(text)]


def _collapse_doubled_words(text: str, exceptions: set[str]) -> tuple[str, int]:
    """`The the cat` -> `The cat`. Only when the two are genuinely adjacent
    (whitespace, or whitespace plus a dash) - never across a sentence boundary.

    The SECOND copy is the one deleted, so the first keeps whatever capital
    Whisper gave it. Dropping the first instead would turn "The the cat" into
    "the cat" and force this pass to re-case the sentence, which it should not
    have to do.
    """
    total = 0
    for _ in range(_MAX_PASSES):
        spans = _word_spans(text)
        cut = None
        for i in range(len(spans) - 1):
            w1, _, e1 = spans[i]
            w2, s2, e2 = spans[i + 1]
            if w1 != w2 or w1 in exceptions:
                continue
            between = text[e1:s2]
            if not re.fullmatch(r"[ \t]*[-–—]?[ \t]*", between):
                continue  # a comma or full stop between them means it is deliberate
            cut = (e1, e2)
            break
        if cut is None:
            break
        text = text[:cut[0]] + text[cut[1]:]
        total += 1
    return text, total


def _collapse_repeated_phrases(text: str, max_words: int) -> tuple[str, int]:
    """`I want to - I want to go` -> `I want to go`.

    Finds a run of 2..max_words words immediately repeated verbatim and deletes
    the *second* copy, together with whatever punctuation sat between the two.
    The copies are identical, so which one goes makes no difference to the words
    - but keeping the first preserves the sentence's opening capital, exactly as
    in `_collapse_doubled_words`.

    Longest run first, so a 4-word restart is not mistaken for two 2-word ones.
    """
    total = 0
    for _ in range(_MAX_PASSES):
        spans = _word_spans(text)
        cut = None
        for n in range(min(max_words, len(spans) // 2), 1, -1):
            for i in range(len(spans) - 2 * n + 1):
                first = [w for w, _, _ in spans[i:i + n]]
                second = [w for w, _, _ in spans[i + n:i + 2 * n]]
                if first != second:
                    continue
                gap = text[spans[i + n - 1][2]:spans[i + n][1]]
                # Only a restart if nothing but whitespace/dashes/commas separates
                # the two copies. A full stop means they are separate sentences.
                if not re.fullmatch(r"[ \t]*[,;:\-–—…]*[ \t]*", gap):
                    continue
                cut = (spans[i + n - 1][2], spans[i + 2 * n - 1][2])
                break
            if cut:
                break
        if cut is None:
            break
        text = text[:cut[0]] + text[cut[1]:]
        total += 1
    return text, total


def _repair_punctuation(text: str) -> str:
    """Tidy punctuation that a deletion left dangling.

    Only ever removes characters or whitespace. Never inserts a mark that was
    not already in the text.
    """
    # ", ," or ",." left by removing the word between two marks.
    text = re.sub(r"([,;:])[ \t]*(?=[,;:.!?])", "", text)
    # ". ." left by removing a filler that stood as its own sentence ("...I had.
    # Um." -> "...I had. ."). The SECOND mark is the one dropped, so "Really?
    # Um." keeps its question mark - swapping a "?" for a "." would be
    # re-punctuating, which this pass does not do. Whitespace between the two is
    # required, which is what keeps a real ellipsis ("...") intact.
    text = re.sub(r"([.!?])[ \t]+[.!?]+", r"\1", text)
    # The same thing at the very start ("Um. That is all." -> ". That is all."),
    # matching what the line below already does for a leading comma.
    text = re.sub(r"^[ \t]*[.!?]+[ \t]*", "", text)
    # Space before a closing mark.
    text = re.sub(r"[ \t]+([,;:.!?])", r"\1", text)
    # A mark at the very start, or right after an opening bracket/quote.
    text = re.sub(r"^[ \t]*[,;:]+[ \t]*", "", text)
    text = re.sub(r"([\[(\"“])[ \t]*[,;:]+[ \t]*", r"\1", text)
    # Repeated spaces introduced by removals.
    text = re.sub(r"[ \t]{2,}", " ", text)
    # A dangling dash at the end of a line, left by a removed restart.
    text = re.sub(r"[ \t]*[-–—]+[ \t]*$", "", text)
    return text


_SENTENCE_END_RE = re.compile(r"[.!?]")


def _recapitalize(text: str, original: str) -> str:
    """Repair a capital that a deletion destroyed - and nothing else.

    A word is capitalised only when it starts a sentence in the cleaned text but
    did NOT start one in the original, which means the words that used to be in
    front of it ("Um, so we should go" -> "so we should go") were removed. If
    Whisper chose to write a sentence in lowercase, that choice is left alone:
    this pass repairs its own damage, it does not re-case the transcript.

    Since the cleaned words are a subsequence of the original words, the two can
    be aligned greedily, which is what tells us whether a word was sentence-
    initial before.
    """
    if not text:
        return text

    out_words = _word_spans(text)
    src_words = _word_spans(original)
    if not out_words:
        return text

    # Greedy alignment: out_words[j] corresponds to src_words[mapping[j]].
    mapping: list[int] = []
    i = 0
    for word, _, _ in out_words:
        while i < len(src_words) and src_words[i][0] != word:
            i += 1
        if i >= len(src_words):
            return text  # not a subsequence; the safety check will reject anyway
        mapping.append(i)
        i += 1

    src_initial = _sentence_initial_flags(original, src_words)
    out_initial = _sentence_initial_flags(text, out_words)

    result = list(text)
    for j, (_, start, _) in enumerate(out_words):
        if not out_initial[j] or src_initial[mapping[j]]:
            continue
        if result[start].islower():
            result[start] = result[start].upper()
    return "".join(result)


def _sentence_initial_flags(text: str, spans: list[tuple[str, int, int]]) -> list[bool]:
    """For each word, whether only whitespace and opening marks separate it from
    the start of the text or from the previous sentence-ending punctuation."""
    flags = []
    prev_end = 0
    for k, (_, start, end) in enumerate(spans):
        between = text[prev_end:start]
        flags.append(k == 0 or bool(_SENTENCE_END_RE.search(between)))
        prev_end = end
    return flags
