"""The cleanup rule set: a data file the user can edit.

The rules are deliberately *only able to delete*. There is no "replace with"
field anywhere in the schema, and the engine never substitutes one string for
another. That is a structural guarantee, not a promise: the worst a bad rule can
do is remove something it should have kept, which the user will see immediately.
It can never put a word into a document that was not spoken.

`engine.clean()` then re-checks that guarantee on the actual output (see
`engine.words_are_subsequence`), so even a user-written regex cannot invent text.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import ConfigError


@dataclass(frozen=True)
class Deletion:
    """A guarded regex whose match is deleted. Never replaced with anything."""

    name: str
    pattern: str
    ignorecase: bool = True
    comment: str = ""
    regex: re.Pattern[str] = field(compare=False, repr=False, default=None)  # type: ignore[assignment]


@dataclass
class CleanupRules:
    #: Words removed wherever they stand alone as a whole word.
    fillers: list[str] = field(default_factory=list)
    #: Whole phrases removed wherever they appear, at word boundaries.
    filler_phrases: list[str] = field(default_factory=list)
    #: Guarded regex deletions, applied in order, after the word lists.
    deletions: list[Deletion] = field(default_factory=list)

    collapse_doubled_words: bool = True
    #: Words that are legitimately said twice in a row in English.
    doubled_word_exceptions: list[str] = field(
        default_factory=lambda: ["had", "that", "is", "was", "he", "she", "no"]
    )
    #: "I want to - I want to go" -> "I want to go".
    collapse_repeated_phrases: bool = True
    max_repeat_phrase_words: int = 6

    #: Tidy the punctuation a deletion left dangling (", ," -> ","). Never adds
    #: punctuation that was not already there.
    repair_punctuation: bool = True
    #: Re-capitalise a sentence whose first word was a filler we deleted.
    recapitalize_sentences: bool = True

    _filler_re: re.Pattern[str] | None = field(default=None, repr=False, compare=False)
    _phrase_re: re.Pattern[str] | None = field(default=None, repr=False, compare=False)

    def compile(self) -> "CleanupRules":
        if self.fillers:
            alts = "|".join(re.escape(w.strip()) for w in self.fillers if w.strip())
            # Also eat a comma or ellipsis the filler was carrying, so removing
            # "um," does not leave a stray comma behind.
            self._filler_re = re.compile(
                rf"(?<![\w'])(?:{alts})(?![\w'])[ \t]*(?:[,;]|\.\.\.)?",
                re.IGNORECASE,
            )
        if self.filler_phrases:
            alts = "|".join(
                r"[ \t]+".join(re.escape(part) for part in p.split())
                for p in self.filler_phrases
                if p.strip()
            )
            self._phrase_re = re.compile(
                rf"(?<![\w'])(?:{alts})(?![\w'])[ \t]*(?:[,;]|\.\.\.)?",
                re.IGNORECASE,
            )
        return self


DEFAULT_RULES_TOML = "cleanup-rules.toml"


def _as_str_list(name: str, value: Any, path: Path) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(
            f"{path}: '{name}' must be a list of quoted strings.",
            f'Write it as {name} = ["one", "two"].',
        )
    return value


def from_mapping(raw: dict[str, Any], path: Path) -> CleanupRules:
    known = {
        "fillers",
        "filler_phrases",
        "deletions",
        "collapse_doubled_words",
        "doubled_word_exceptions",
        "collapse_repeated_phrases",
        "max_repeat_phrase_words",
        "repair_punctuation",
        "recapitalize_sentences",
    }
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ConfigError(
            f"{path}: unknown setting(s): {', '.join(unknown)}.",
            "Valid settings: " + ", ".join(sorted(known)) + ".",
        )

    rules = CleanupRules()
    if "fillers" in raw:
        rules.fillers = _as_str_list("fillers", raw["fillers"], path)
    if "filler_phrases" in raw:
        rules.filler_phrases = _as_str_list("filler_phrases", raw["filler_phrases"], path)
    if "doubled_word_exceptions" in raw:
        rules.doubled_word_exceptions = _as_str_list(
            "doubled_word_exceptions", raw["doubled_word_exceptions"], path
        )
    for flag in ("collapse_doubled_words", "collapse_repeated_phrases",
                 "repair_punctuation", "recapitalize_sentences"):
        if flag in raw:
            if not isinstance(raw[flag], bool):
                raise ConfigError(f"{path}: '{flag}' must be true or false.",
                                  f"Write {flag} = true or {flag} = false.")
            setattr(rules, flag, raw[flag])
    if "max_repeat_phrase_words" in raw:
        v = raw["max_repeat_phrase_words"]
        if not isinstance(v, int) or isinstance(v, bool) or not 2 <= v <= 20:
            raise ConfigError(f"{path}: 'max_repeat_phrase_words' must be a number from 2 to 20.",
                              "6 is the default.")
        rules.max_repeat_phrase_words = v

    for i, item in enumerate(raw.get("deletions", []) or []):
        if not isinstance(item, dict):
            raise ConfigError(f"{path}: [[deletions]] entry {i + 1} is not a table.",
                              "Each rule needs a name and a pattern.")
        extra = sorted(set(item) - {"name", "pattern", "ignorecase", "comment"})
        if extra:
            raise ConfigError(
                f"{path}: [[deletions]] entry {i + 1} has unknown field(s): {', '.join(extra)}.",
                "A deletion rule has only: name, pattern, ignorecase, comment. "
                "There is deliberately no 'replace' - rules can only delete.",
            )
        name = item.get("name") or f"deletion #{i + 1}"
        pattern = item.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            raise ConfigError(f"{path}: rule {name!r} has no 'pattern'.",
                              "Give it pattern = \"...\" (a regular expression).")
        ignorecase = bool(item.get("ignorecase", True))
        try:
            regex = re.compile(pattern, re.IGNORECASE if ignorecase else 0)
        except re.error as exc:
            raise ConfigError(
                f"{path}: rule {name!r} has an invalid regular expression: {exc}",
                "Fix the pattern, or delete the rule to carry on without it.",
            ) from exc
        rules.deletions.append(
            Deletion(name=str(name), pattern=pattern, ignorecase=ignorecase,
                     comment=str(item.get("comment", "")), regex=regex)
        )
    return rules.compile()


def load(path: str | Path) -> CleanupRules:
    p = Path(path)
    if not p.exists():
        raise ConfigError(
            f"Cleanup rules file not found: {p}",
            "Copy config/cleanup-rules.toml next to your dictate.toml, or set "
            "[cleanup] enabled = false to paste Whisper's text unchanged.",
        )
    try:
        # utf-8-sig: this file is meant to be edited, and Notepad saves UTF-8
        # with a byte order mark that tomllib would report as a syntax error.
        raw = tomllib.loads(p.read_text(encoding="utf-8-sig"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{p} is not valid TOML: {exc}",
                          "Check for a missing quote or bracket on the line above.") from exc
    except OSError as exc:
        raise ConfigError(f"Could not read {p}: {exc}", "Check the file is readable.") from exc
    return from_mapping(raw, p)
