"""The spoken-punctuation rule set: a data file the user can edit.

The cleanup pass next door can only ever DELETE words, enforced by a schema with
no replacement field. This stage exists because spoken punctuation is a
*substitution*, which that schema is built to make impossible - so it is a
separate stage with a separate, narrower guarantee of its own:

    A mark may only ever insert punctuation and whitespace. Never a letter,
    never a digit.

That is enforced here, at load time, on `insert` - including on a rule the user
wrote himself (`_check_insert`). `engine.apply()` then re-checks the guarantee on
the actual output with the very same subsequence check the cleanup pass uses, so
this stage cannot put a word into a document either.

The two guarantees are different on purpose:

    cleanup      may delete words, may not add anything
    punctuation  may delete the words of a mark phrase, may add only marks

Neither can invent a word, which is the property that matters.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import ConfigError

#: How a mark sits against the words either side of it.
#:
#:   after   the mark goes on the END of the word before it, and a space
#:           follows it:  "hello, world"                        . , ? ! : ;
#:   close   like `after`, but it closes a pair rather than ending a clause,
#:           so it does NOT take a full stop standing after it with it:
#:           'he said "hello there".'                        ) closing quote
#:   before  the mark goes in FRONT of the word after it, with a space before
#:           it:  'he said "hello'                            ( opening quote
#:   around  a space on both sides:  "go - if we can"                   -
#:   break   a line break: no space either side, and the next word starts a
#:           new line.                                              newline
SPACINGS = ("after", "close", "before", "around", "break")

#: What `insert` is allowed to contain. Punctuation, symbols and whitespace, and
#: nothing that could be part of a word. This is the schema half of the
#: guarantee; `engine.apply()` is the other half.
_INSERT_ALLOWED = re.compile(r"[^\w]*\Z", re.UNICODE)

#: A `say` phrase is matched word by word, so this is what a word looks like
#: here. Same shape as the cleanup engine's, deliberately.
_PHRASE_WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)*\Z", re.UNICODE)


@dataclass(frozen=True)
class Mark:
    """One spoken mark. `say` holds the phrases that produce `insert`."""

    name: str
    #: Each phrase as a tuple of lowercased words: ("question", "mark").
    say: tuple[tuple[str, ...], ...]
    insert: str
    spacing: str = "after"
    #: Capitalise the next word, the way Whisper would have after a full stop.
    capitalize_next: bool = False
    #: Extra words that mean "this one was spoken as a word", on top of the
    #: file's shared `guard_words`.
    guard_words: tuple[str, ...] = ()
    comment: str = ""


@dataclass
class PunctuationRules:
    marks: list[Mark] = field(default_factory=list)
    #: Say this in front of a mark phrase to get the words themselves:
    #: "literal comma" -> "comma". Empty string turns the escape off.
    escape_word: str = "literal"
    #: A mark phrase directly after one of these is a WORD, not a mark:
    #: "the comma goes here". See `engine._is_guarded` for the whole rule.
    guard_words: list[str] = field(
        default_factory=lambda: ["a", "an", "the", "this", "that", "these", "those"]
    )

    def phrases(self) -> list[tuple[tuple[str, ...], Mark]]:
        """Every (phrase, mark) pair, longest phrase first.

        Longest first so "question mark" is never mistaken for a bare "mark",
        and "new paragraph" never for "new line".
        """
        pairs = [(p, m) for m in self.marks for p in m.say]
        pairs.sort(key=lambda pair: len(pair[0]), reverse=True)
        return pairs


DEFAULT_RULES_TOML = "voice-punctuation.toml"

_KNOWN_TOP = {"escape_word", "guard_words", "marks"}
_KNOWN_MARK = {"name", "say", "insert", "spacing", "capitalize_next",
               "guard_words", "comment"}


def _as_str_list(name: str, value: Any, path: Path) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(
            f"{path}: '{name}' must be a list of quoted strings.",
            f'Write it as {name} = ["one", "two"].',
        )
    return value


def _check_insert(name: str, insert: str, path: Path) -> str:
    """The guarantee, in the schema: a mark inserts punctuation, never a word."""
    if not insert:
        raise ConfigError(
            f"{path}: mark {name!r} has an empty 'insert'.",
            "Give it the character to insert, for example insert = \",\". "
            "To stop a mark being recognised at all, delete its [[marks]] block.",
        )
    if not _INSERT_ALLOWED.fullmatch(insert):
        raise ConfigError(
            f"{path}: mark {name!r} would insert {insert!r}, which contains "
            f"letters or digits.",
            "A mark can only ever insert punctuation and whitespace - that is "
            "what stops this file being able to put words you did not say into "
            "your document. Use a punctuation character, \"\\n\" for a line "
            "break, or delete the rule.",
        )
    return insert


def _check_phrase(name: str, phrase: str, path: Path) -> tuple[str, ...]:
    words = phrase.lower().split()
    if not words:
        raise ConfigError(
            f"{path}: mark {name!r} has an empty phrase in 'say'.",
            'Every entry in say must be something you can speak, e.g. "question mark".',
        )
    for word in words:
        if not _PHRASE_WORD.fullmatch(word):
            raise ConfigError(
                f"{path}: mark {name!r} has {phrase!r} in 'say', and {word!r} is "
                f"not a word dictate can match.",
                "A spoken phrase is matched word by word against what Whisper "
                "wrote, so it can only contain letters, digits and apostrophes - "
                "no punctuation and no regular expressions.",
            )
    return tuple(words)


def from_mapping(raw: dict[str, Any], path: Path) -> PunctuationRules:
    unknown = sorted(set(raw) - _KNOWN_TOP)
    if unknown:
        raise ConfigError(
            f"{path}: unknown setting(s): {', '.join(unknown)}.",
            "Valid settings: " + ", ".join(sorted(_KNOWN_TOP)) + ". Remember that "
            "in TOML a plain setting written AFTER a [[marks]] block belongs to "
            "that block, not to the file.",
        )

    rules = PunctuationRules()
    if "escape_word" in raw:
        value = raw["escape_word"]
        if not isinstance(value, str):
            raise ConfigError(f"{path}: 'escape_word' must be a quoted word.",
                              'Write escape_word = "literal", or "" to turn it off.')
        if value.strip() and len(value.split()) != 1:
            raise ConfigError(
                f"{path}: 'escape_word' is {value!r}, which is more than one word.",
                "It has to be a single word you say in front of a mark, e.g. "
                '"literal". Use "" to turn the escape off.',
            )
        rules.escape_word = value.strip().lower()
    if "guard_words" in raw:
        rules.guard_words = [w.strip().lower()
                             for w in _as_str_list("guard_words", raw["guard_words"], path)]

    seen: dict[tuple[str, ...], str] = {}
    for i, item in enumerate(raw.get("marks", []) or []):
        if not isinstance(item, dict):
            raise ConfigError(f"{path}: [[marks]] entry {i + 1} is not a table.",
                              "Each mark needs a name, a say list and an insert.")
        extra = sorted(set(item) - _KNOWN_MARK)
        if extra:
            raise ConfigError(
                f"{path}: [[marks]] entry {i + 1} has unknown field(s): {', '.join(extra)}.",
                "A mark has only: " + ", ".join(sorted(_KNOWN_MARK)) + ".",
            )
        name = str(item.get("name") or f"mark #{i + 1}")
        insert = item.get("insert")
        if not isinstance(insert, str):
            raise ConfigError(f"{path}: mark {name!r} has no 'insert'.",
                              'Give it insert = "," (the character to put in).')
        insert = _check_insert(name, insert, path)

        say_raw = item.get("say")
        if say_raw is None:
            raise ConfigError(f"{path}: mark {name!r} has no 'say'.",
                              'Give it say = ["comma"] - the words you speak for it.')
        say = tuple(_check_phrase(name, p, path)
                    for p in _as_str_list("say", say_raw, path))
        if not say:
            raise ConfigError(f"{path}: mark {name!r} has an empty 'say' list.",
                              'Give it at least one phrase, e.g. say = ["comma"].')
        for phrase in say:
            if phrase in seen:
                raise ConfigError(
                    f"{path}: {' '.join(phrase)!r} is in 'say' for both "
                    f"{seen[phrase]!r} and {name!r}.",
                    "One spoken phrase can only produce one mark. Delete it from "
                    "whichever of the two you did not mean.",
                )
            seen[phrase] = name

        spacing = item.get("spacing", "after")
        if spacing not in SPACINGS:
            raise ConfigError(
                f"{path}: mark {name!r} has spacing = {spacing!r}.",
                "Valid values: " + ", ".join(SPACINGS) + ". 'after' puts the mark "
                "on the end of the word before it, which is what a comma does.",
            )
        cap = item.get("capitalize_next", False)
        if not isinstance(cap, bool):
            raise ConfigError(f"{path}: mark {name!r} has a non-true/false "
                              f"'capitalize_next'.",
                              "Write capitalize_next = true or = false.")
        guards = tuple(w.strip().lower() for w in
                       _as_str_list("guard_words", item.get("guard_words", []), path))
        rules.marks.append(Mark(
            name=name, say=say, insert=insert, spacing=spacing,
            capitalize_next=cap, guard_words=guards,
            comment=str(item.get("comment", "")),
        ))
    return rules


def load(path: str | Path) -> PunctuationRules:
    p = Path(path)
    if not p.exists():
        raise ConfigError(
            f"Spoken punctuation rules file not found: {p}",
            "Copy config/voice-punctuation.toml next to your dictate.toml, or set "
            "[punctuation] enabled = false to leave spoken marks as words.",
        )
    try:
        # utf-8-sig for the same reason as the cleanup rules: this file is meant
        # to be edited, and Notepad writes a byte order mark.
        raw = tomllib.loads(p.read_text(encoding="utf-8-sig"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{p} is not valid TOML: {exc}",
                          "Check for a missing quote or bracket on the line above.") from exc
    except OSError as exc:
        raise ConfigError(f"Could not read {p}: {exc}", "Check the file is readable.") from exc
    return from_mapping(raw, p)
