"""Changing one setting in his config file, and changing nothing else.

**Why this is not "write the config out again".** `dictate.toml` is a file he
was given with comments on every setting explaining what it does, and those
comments are most of what the file is for. Loading it with `tomllib` and dumping
it back would hand him a stripped file with his own settings in a different
order - which is a worse file than the one he had, delivered as a side effect of
changing a hotkey. So this is a text edit: the one line changes and every other
byte of the file, comments and line endings and all, is the byte it was.

**Why there is a file edit at all.** He said he will not hand-edit a config
file. Changing the hotkey from the tray therefore has to write it down for him,
or the change lasts until the next restart and he has been lied to.

Three things are load-bearing and all three are tested (tests/test_config_edit.py):

* **Line endings are preserved, byte for byte.** His file is CRLF, written by
  Windows; reading it with Python's universal newlines and writing it back
  would leave whichever the platform prefers, which turns a one-line change into
  a whole-file change he cannot review. Nothing here decodes newlines.
* **A byte order mark survives.** Notepad and Windows PowerShell both write one,
  `config.load` reads `utf-8-sig` for exactly that reason, and dropping it here
  would be a change to a file he did not ask to change.
* **The write is `os.replace` of a temp file**, like the dictation history, so
  an interrupted one cannot leave him with half a config - which would be a
  dictate that will not start.

It is plain string handling and `pathlib`, so it is tested anywhere.
"""

from __future__ import annotations

import codecs
import os
import re
from pathlib import Path

from .errors import ConfigError


def _section_re(section: str) -> re.Pattern[str]:
    """`[hotkey]`, at the start of a line, comment or spaces after it allowed.

    Not `[[hotkey]]`: that is an array of tables and a different thing, and the
    negative lookahead is what keeps the two apart in a file that has both.
    """
    return re.compile(r"^[ \t]*\[(?!\[)[ \t]*" + re.escape(section)
                      + r"[ \t]*\][ \t]*(#.*)?$")


def _key_re(key: str) -> re.Pattern[str]:
    """`key = <anything>`, capturing the indent and any trailing comment."""
    return re.compile(r"^(?P<indent>[ \t]*)(?P<key>" + re.escape(key)
                      + r")[ \t]*=[ \t]*(?P<value>.*?)"
                      r"(?P<comment>[ \t]*#.*)?$")


def _quote(value: str) -> str:
    """A TOML basic string. The values this writes are hotkey combinations, but
    escaping is not conditional on believing that."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _newline(lines: list[str]) -> str:
    """The line ending the file already uses, so an inserted line matches."""
    for line in lines:
        if line.endswith("\r\n"):
            return "\r\n"
        if line.endswith("\n"):
            return "\n"
    return os.linesep


def set_string(text: str, section: str, key: str, value: str) -> str:
    """Return `text` with `[section] key` set to `value`.

    The key is changed where it already is; a missing key is added to its
    section; a missing section is added at the end of the file. That last one is
    safe in TOML however the rest of the file is arranged, because a table
    header ends the table before it - which is the trap `config/*.toml` warn
    about at the top of every file.
    """
    lines = text.splitlines(keepends=True)
    newline = _newline(lines)
    section_re, key_re = _section_re(section), _key_re(key)

    start = next((i for i, line in enumerate(lines)
                  if section_re.match(line.rstrip("\r\n"))), None)
    if start is None:
        tail = "" if not lines or lines[-1].endswith("\n") else newline
        return (text + tail + newline
                + f"[{section}]{newline}{key} = {_quote(value)}{newline}")

    end = next((i for i in range(start + 1, len(lines))
                if lines[i].lstrip().startswith("[")), len(lines))
    for i in range(start + 1, end):
        match = key_re.match(lines[i].rstrip("\r\n"))
        if not match:
            continue
        ending = lines[i][len(lines[i].rstrip("\r\n")):]
        lines[i] = (f"{match['indent']}{key} = {_quote(value)}"
                    f"{match['comment'] or ''}{ending}")
        return "".join(lines)

    # The section is there and the key is not: put it at the top of the section,
    # where a setting is easiest to find and cannot land inside an array of
    # tables that follows.
    lines.insert(start + 1, f"{key} = {_quote(value)}{newline}")
    return "".join(lines)


def write_string(path: Path, section: str, key: str, value: str) -> None:
    """Set `[section] key` in the file at `path`, in place.

    Raises `ConfigError` - with the file named and the line to type in it - if
    the file cannot be read or written, because the caller's next job is to tell
    him what to do about it.
    """
    path = Path(path)
    try:
        data = path.read_bytes()
        raw = data.decode("utf-8-sig")
    except OSError as exc:
        raise ConfigError(
            f"dictate could not read your config file at {path}: {exc}",
            f"Open it yourself and set {key} = {_quote(value)} under "
            f"[{section}].",
        ) from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(
            f"Your config file at {path} is not text dictate can read: {exc}",
            "It should be a UTF-8 text file. Run `dictate init` to write a "
            "fresh one, or fix the encoding in your editor.",
        ) from exc

    updated = set_string(raw, section, key, value)
    mark = codecs.BOM_UTF8 if data.startswith(codecs.BOM_UTF8) else b""
    temp = path.with_name(path.name + ".writing")
    try:
        temp.write_bytes(mark + updated.encode("utf-8"))
        os.replace(temp, path)
    except OSError as exc:
        try:
            temp.unlink()
        except OSError:
            pass
        raise ConfigError(
            f"dictate could not write your config file at {path}: {exc}",
            f"Open it yourself and set {key} = {_quote(value)} under "
            f"[{section}].",
        ) from exc
