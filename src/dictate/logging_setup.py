"""Logging.

The console stays readable - it is what the product owner looks at - so anything
below WARNING goes only to the log file when one is configured. The log file is
where whisper.cpp's own output ends up, which is the first thing to ask for when
something goes wrong on a machine nobody here can reach.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def configure(level: str = "INFO", file: str = "", *, quiet_console: bool = True) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    # ``pythonw.exe`` has no stderr. The autostart entry normally replaces it
    # with its durable log before this function is reached; this guard keeps a
    # future caller from installing a StreamHandler whose first emit raises on
    # a ``None`` stream.
    if sys.stderr is not None:
        console = logging.StreamHandler(sys.stderr)
        console.setLevel(
            logging.WARNING if quiet_console else getattr(logging, level.upper()))
        console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        root.addHandler(console)

    if file:
        path = Path(file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        rotating = logging.handlers.RotatingFileHandler(
            path, maxBytes=2_000_000, backupCount=3, encoding="utf-8",
        )
        rotating.setLevel(getattr(logging, level.upper(), logging.INFO))
        rotating.setFormatter(logging.Formatter(FORMAT))
        root.addHandler(rotating)

    # Without either destination, logging must be silent rather than becoming
    # the failure. This is only a last guard: the logon bootstrap supplies a
    # real stream, and an interactive run supplies stderr.
    if not root.handlers:
        root.addHandler(logging.NullHandler())
