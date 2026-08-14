"""The dictation history: what he said, kept so he can look back over it.

**What it is for, which is what bounds it.** He asked to "keep a history just for
review". So it is a thing to open and read - not analytics, not telemetry, and
not a machine format. Nothing in here is sent anywhere; there is no code in
dictate that could. Everything it decides is here and is plain Python, so it is
tested off Windows like `recovery.py` and `tray.py`; the only platform call
involved is the one that opens the file for him (`app._open_history`).

**What is kept, and what is deliberately not.**

* **The text that was pasted.** The whole point.
* **When, and how long he spoke for.** The timestamp is how he finds an entry
  again; the duration is what tells him which one was the long one, and it is
  the only thing that explains an entry cut short by `[audio] max_utterance_s`.
* **What Whisper heard, before dictate changed anything - but only when it did
  change something.** Two stages can: the cleanup rules delete, and spoken
  punctuation substitutes. That pair is the evidence for "it ate a word" and for
  what a spoken mark turned into, which is the thing most worth being able to
  argue with. When neither stage touched the sentence the line would be the same
  sentence twice, so it is not written.
* **Whether delivering it pressed Return** - and only when it did. Return
  submits in a terminal, a chat box and most search fields, so a dictation that
  contained one may have run a command; that is a thing to be able to look up
  afterwards rather than only to catch happening. It takes `[paste] line_breaks
  = "return"` to get one at all, so ordinarily this line never appears.
* **Not how long transcription took.** That answers a developer's question, not
  his, and the log already carries it.
* **Whether it was pasted at all - and only when it was not.** dictate refuses
  to paste into a window he did not dictate into, so a dictation he spoke while
  clicking away from the window he started in is delivered nowhere
  (`delivery.py`). Those words still exist, and this file is where they keep
  existing after the clipboard has moved on: the entry says, in his words, that
  it was not pasted and why. Every line in this file is still text he said; what
  changed is that "it landed somewhere" is no longer the price of admission,
  because losing a sentence silently is worse than recording one that went
  nowhere.
* **Not the window it was pasted into, and not a failed dictation.** A
  transcription that failed has no words to keep. And a file that recorded where
  he was typing would be a record of his day rather than of his words - which is
  why a held entry names no window either.

**The shape of the file.** Newest first, so opening it shows the last thing he
said rather than the first. That means the whole file is rewritten each time,
which is why it is bounded by entry count rather than by age: `[history] keep`
entries, oldest dropping off the end, written to a temporary file and moved into
place so an interrupted write cannot leave a half file behind.

Every line of every entry is indented, and entries are separated by a rule of a
character no keyboard produces, so splitting the file back into entries is exact
however many box characters or blank lines a dictation contains.
"""

from __future__ import annotations

import logging
import os
import re
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path

from . import instance

log = logging.getLogger(__name__)

#: The file, when `[history] file` is empty: beside his config, which is where
#: he would look for it.
DEFAULT_NAME = "history.txt"

#: The line between entries: a full-width rule, matched only at the start of a
#: line.
RULE = "=" * 74

#: Every line of a dictation is indented, which is what makes that split exact.
#: No line of his text can start at column zero, so none of it can ever look
#: like a rule - not a row of equals signs, not a blank line, not anything.
INDENT = "  "

#: Where the text is wrapped. Notepad opens with word wrap off, and a dictation
#: is one paragraph however long it is, so an unwrapped entry would be a single
#: line running off the side of the window. Only line breaks are added - no word
#: is changed.
WRAP_AT = 76

_RULE_RE = re.compile(r"(?m)^" + re.escape(RULE) + r"$")


@dataclass(frozen=True)
class Entry:
    """One dictation, as it will be read back."""

    when: float
    spoke_s: float
    text: str
    #: What Whisper produced, before the cleanup rules and spoken punctuation
    #: ran. Empty when neither changed it, because then it is the same sentence
    #: twice.
    raw: str = ""
    #: How many Return keypresses delivering it involved. Zero unless he has set
    #: `[paste] line_breaks = "return"`, and said out loud when it is not,
    #: because a Return is the one thing in a paste that can DO something.
    returns: int = 0
    #: False when dictate pasted this nowhere - he had moved to another window
    #: by the time it was ready, so this entry is the copy he gets it back from.
    #: Defaulted to True so that an entry written by anything that does not know
    #: about holding is an ordinary one, which is what every entry was before.
    delivered: bool = True

    @classmethod
    def of(cls, text: str, *, raw: str = "", spoke_s: float = 0.0,
           returns: int = 0, delivered: bool = True,
           when: float | None = None) -> Entry:
        text = text.strip()
        raw = raw.strip()
        return cls(
            when=time.time() if when is None else when,
            spoke_s=max(0.0, spoke_s),
            text=text,
            raw=raw if raw and raw != text else "",
            returns=max(0, returns),
            delivered=delivered,
        )

    def render(self) -> str:
        """The entry as it appears in the file, rule line included."""
        stamp = time.strftime("%a %d %b %Y, %H:%M:%S", time.localtime(self.when))
        parts = [RULE, f"{stamp}   ({self.spoke_s:.1f}s of speaking)", "",
                 _wrap(self.text)]
        if not self.delivered:
            # Under the words rather than above them: what he came here for is
            # the sentence, and the note is why it is here to be found at all.
            parts += ["", _wrap(self.note_about_holding())]
        if self.raw:
            parts += ["", _wrap(self.raw, first="as Whisper heard it: ")]
        if self.returns:
            parts += ["", _wrap(self.note_about_returns())]
        return "\n".join(parts) + "\n\n"

    def note_about_holding(self) -> str:
        """Why an entry exists for something that was never pasted."""
        return ("dictate did not paste this anywhere: you had moved to another "
                "window by the time it was ready, and dictate only pastes into "
                "the window you pressed the hotkey in. It is kept here so you "
                "can copy it out.")

    def note_about_returns(self) -> str:
        """Said in what it did, not in what it was: he is not looking for the
        word "keypress", he is looking for why something ran."""
        many = self.returns > 1
        return (f"dictate pressed Return {self.returns} times while pasting this"
                if many else "dictate pressed Return once while pasting this") + \
            (" - in a terminal or a chat box that submits. Set [paste] "
             'line_breaks = "space" in your dictate.toml to have line breaks '
             "pasted as a space instead.")


def _wrap(text: str, *, first: str = "") -> str:
    """`text` indented under the rule, wrapped, with its own line breaks kept."""
    out: list[str] = []
    for index, line in enumerate(text.splitlines() or [""]):
        prefix = first if index == 0 else ""
        hang = INDENT + " " * len(prefix)
        if not line.strip():
            out.append(INDENT.rstrip())
            continue
        out.append(textwrap.fill(
            line, width=WRAP_AT, initial_indent=INDENT + prefix,
            subsequent_indent=hang, break_long_words=False,
            break_on_hyphens=False))
    return "\n".join(out)


def header(keep: int) -> str:
    """The top of the file. He is not a developer: it says what this is, that it
    stays here, and both ways to get rid of it, before any of his words."""
    return (
        "This is what you have dictated, newest first.\n"
        "\n"
        f"It is here for you to look back over. It keeps the last {keep} "
        "dictations and\n"
        "the older ones drop off the end. It stays on this computer - nothing "
        "about it\n"
        "is sent anywhere, and nothing reads it but you.\n"
        "\n"
        "  delete it        dictate history --delete\n"
        "                   (or right-click the dictate icon by the clock)\n"
        f"  keep more/fewer  [history] keep = {keep}, in your dictate.toml\n"
        "  stop keeping it  [history] enabled = false, in the same file\n"
        "\n"
    )


def entries_in(raw: str) -> list[str]:
    """The rendered entries in a history file, newest first.

    Split on the rule lines rather than parsed: what is being kept is his text,
    exactly as it was written, and re-formatting it on every rewrite would mean
    the file was only ever as good as the parser.
    """
    starts = [match.start() for match in _RULE_RE.finditer(raw)]
    return [raw[a:b] for a, b in zip(starts, starts[1:] + [len(raw)])]


def path_for(cfg) -> Path:
    """Where this config's history lives.

    Beside his `dictate.toml`, so it is in the folder he already knows. With no
    config file there is nowhere obvious to put it beside, so it goes where
    dictate keeps the rest of its own state.
    """
    if cfg.history.file:
        return cfg.resolve(cfg.history.file)
    if cfg.source_path is not None:
        return Path(cfg.source_path).parent / DEFAULT_NAME
    return instance.state_dir() / DEFAULT_NAME


class HistoryStore:
    """Reads and writes that file. Never raises: a history that cannot be
    written is worth one message and nothing else - it must not cost him a
    dictation."""

    def __init__(self, path: Path, *, keep: int = 200, enabled: bool = True,
                 notify=None) -> None:
        self.path = Path(path)
        self.keep = keep
        self.enabled = enabled and keep > 0
        self._notify = notify or (lambda level, message: None)
        #: Said once. A history that cannot be written is not a reason to
        #: interrupt him again every time he speaks.
        self._complained = False

    @property
    def describe(self) -> str:
        if not self.enabled:
            return "off ([history] enabled = false)"
        return f"the last {self.keep} dictations, in {self.path}"

    def record(self, text: str, *, raw: str = "", spoke_s: float = 0.0,
               returns: int = 0, delivered: bool = True) -> bool:
        """Add one dictation. True if it was written.

        Called from the finalise worker once the dictation has an outcome:
        delivered, or held because there was no window it was allowed to go into.
        The return value matters on the second path - it is what lets the message
        say "your words are in your dictation history" only when they are.

        `[history] enabled = false` still means nothing is written, including a
        held one. Turning the record of everything he says off has to mean off;
        a held dictation is then on the clipboard and nowhere else, and the
        message he is shown says exactly that.
        """
        if not self.enabled or not text.strip():
            return False
        entry = Entry.of(text, raw=raw, spoke_s=spoke_s, returns=returns,
                         delivered=delivered)
        try:
            existing = entries_in(self._read())
            self._write(header(self.keep)
                        + entry.render()
                        + "".join(existing[:max(0, self.keep - 1)]))
        except OSError as exc:
            self._complain(f"dictate could not write the dictation history at "
                           f"{self.path}: {exc}")
            return False
        return True

    def count(self) -> int:
        """How many dictations are in the file right now."""
        try:
            return len(entries_in(self._read()))
        except OSError:
            return 0

    def delete(self) -> bool:
        """Remove the history, completely. True if there was one to remove.

        The whole file, not an entry: this is the one obvious step, and what he
        asked to be able to do is get rid of it.
        """
        removed = False
        for path in (self.path, self._temp_path()):
            try:
                path.unlink()
                removed = True
            except FileNotFoundError:
                pass
            except OSError as exc:
                self._complain(f"dictate could not delete the dictation history "
                               f"at {path}: {exc}")
                return False
        if removed:
            log.info("the dictation history at %s was deleted", self.path)
        return removed

    # -- the file --------------------------------------------------------

    def _read(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def _temp_path(self) -> Path:
        return self.path.with_name(self.path.name + ".writing")

    def _write(self, text: str) -> None:
        """Write the whole file, atomically.

        `os.replace` is a rename, so the history is either the old one or the
        new one and never half of either - including when the machine goes off
        mid-write.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self._temp_path()
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, self.path)

    def _complain(self, message: str) -> None:
        log.warning("%s", message)
        if self._complained:
            return
        self._complained = True
        self._notify("warning", message + "\nYour dictation is unaffected. Turn "
                                          "the history off with [history] "
                                          "enabled = false if this keeps "
                                          "happening.")
