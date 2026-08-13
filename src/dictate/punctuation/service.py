"""The spoken-punctuation rules as a live, editable file.

Exactly the shape of `cleanup/service.py`, and for the same reasons: the rules
are data the product owner edits, so the file is re-read when its modification
time changes rather than making him restart to try a change, and a broken edit
does not take the app down - the last good rule set stays in use and he is told
what is wrong with the new one.

Disabled, it is the identity function. `[punctuation] enabled = false` is the
default, and with it the text that gets pasted is byte for byte what it was
before this feature existed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from ..errors import ConfigError
from . import rules as rules_mod
from .engine import PunctuationResult, apply

log = logging.getLogger(__name__)


class PunctuationService:
    """Callable: `service(text) -> PunctuationResult`."""

    def __init__(self, path: Path | None, *, enabled: bool = False,
                 notify: Callable[[str, str], None] | None = None) -> None:
        self.path = Path(path) if path else None
        self.enabled = enabled
        self.notify = notify or (lambda level, msg: None)
        self._rules: rules_mod.PunctuationRules | None = None
        self._mtime: float | None = None

    def load(self) -> None:
        """Load once, eagerly, so a bad rules file is reported at startup."""
        if not self.enabled or self.path is None:
            return
        self._rules = rules_mod.load(self.path)
        self._mtime = self._stat()
        phrases = sum(len(m.say) for m in self._rules.marks)
        log.info("spoken punctuation loaded from %s (%d marks, %d phrases)",
                 self.path, len(self._rules.marks), phrases)

    def _stat(self) -> float | None:
        try:
            return self.path.stat().st_mtime if self.path else None
        except OSError:
            return None

    def _current(self) -> rules_mod.PunctuationRules | None:
        if not self.enabled or self.path is None:
            return None
        mtime = self._stat()
        if mtime is not None and mtime != self._mtime:
            try:
                self._rules = rules_mod.load(self.path)
                self._mtime = mtime
                log.info("spoken punctuation rules reloaded from %s", self.path)
                self.notify("info", f"Spoken punctuation reloaded from {self.path.name}.")
            except ConfigError as exc:
                self._mtime = mtime  # do not re-report the same broken file each time
                log.error("spoken punctuation not reloaded: %s", exc.report())
                self.notify(
                    "error",
                    f"Your edit to {self.path.name} could not be read, so dictate "
                    f"is still using the previous spoken punctuation rules.\n"
                    f"{exc.report()}",
                )
        return self._rules

    def __call__(self, text: str) -> PunctuationResult:
        rules = self._current()
        if rules is None:
            return PunctuationResult(text=text, original=text)
        return apply(text, rules)
