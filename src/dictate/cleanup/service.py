"""The cleanup rules as a live, editable file.

The rules are data the product owner edits, so the app re-reads the file when
its modification time changes rather than making him restart to try a change.
A broken edit does not take the app down: the last good rule set stays in use and
he is told what is wrong with the new one.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from ..errors import ConfigError
from . import rules as rules_mod
from .engine import CleanResult, clean

log = logging.getLogger(__name__)


class CleanupService:
    """Callable: `service(text) -> CleanResult`."""

    def __init__(self, path: Path | None, *, enabled: bool = True,
                 notify: Callable[[str, str], None] | None = None) -> None:
        self.path = Path(path) if path else None
        self.enabled = enabled
        self.notify = notify or (lambda level, msg: None)
        self._rules: rules_mod.CleanupRules | None = None
        self._mtime: float | None = None

    def load(self) -> None:
        """Load once, eagerly, so a bad rules file is reported at startup."""
        if not self.enabled or self.path is None:
            return
        self._rules = rules_mod.load(self.path)
        self._mtime = self._stat()
        log.info("cleanup rules loaded from %s (%d fillers, %d phrases, %d patterns)",
                 self.path, len(self._rules.fillers), len(self._rules.filler_phrases),
                 len(self._rules.deletions))

    def _stat(self) -> float | None:
        try:
            return self.path.stat().st_mtime if self.path else None
        except OSError:
            return None

    def _current(self) -> rules_mod.CleanupRules | None:
        if not self.enabled or self.path is None:
            return None
        mtime = self._stat()
        if mtime is not None and mtime != self._mtime:
            try:
                self._rules = rules_mod.load(self.path)
                self._mtime = mtime
                log.info("cleanup rules reloaded from %s", self.path)
                self.notify("info", f"Cleanup rules reloaded from {self.path.name}.")
            except ConfigError as exc:
                self._mtime = mtime  # do not re-report the same broken file each time
                log.error("cleanup rules not reloaded: %s", exc.report())
                self.notify(
                    "error",
                    f"Your edit to {self.path.name} could not be read, so dictate "
                    f"is still using the previous rules.\n{exc.report()}",
                )
        return self._rules

    def __call__(self, text: str) -> CleanResult:
        rules = self._current()
        if rules is None:
            return CleanResult(text=text.strip(), original=text)
        return clean(text, rules)
