"""The icon in the notification area: what it says and what it offers.

**Why there is one at all.** `dictate autostart enable` is the way this is meant
to be used, and it starts dictate under `pythonw.exe` - no console, no window,
nothing on screen at all until you speak. A command you cannot see is a command
you will not remember, and the product owner said so in as many words: "there is
no easy way I see to stop it beyond powershell commands I wont remember". So the
running copy carries one visible thing that says what it is doing and offers the
actions worth having: stop it, start it again, and keep it up to date.

It is not a second way of doing anything. Every item here does exactly what the
matching command does - Stop is `dictate stop`, Restart is `dictate stop`
followed by `dictate run`, the two update items are `dictate update --check` and
`dictate update` - and each menu item says the command out loud, so the tray
teaches the commands rather than replacing them.

This module is the whole of the decision-making: what the states are, what each
one is called, which items the menu has, and the bytes of the icon itself. All
of it is plain Python and tested in `tests/test_tray.py`. The Win32 part - one
message-only window, `Shell_NotifyIconW`, and a popup menu - is in
`platform/windows/tray.py` and is as small as it can be, because nobody here can
run it.
"""

from __future__ import annotations

import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

#: The menu item keys. The Win32 side maps these to command ids and back; it
#: knows nothing else about what the menu means.
STOP = "stop"
RESTART = "restart"
CHECK = "check"
UPDATE = "update"
LOG = "log"
STATUS = "status"


class TrayStatus(Enum):
    """What dictate is doing, in the only terms worth putting in a tooltip."""

    STARTING = "starting"
    READY = "ready"            # waiting for the hotkey, model resident
    RESTING = "resting"        # waiting for the hotkey, model unloaded
    LISTENING = "listening"    # the hotkey is down
    WORKING = "working"        # transcribing
    STOPPING = "stopping"
    ERROR = "error"


#: The colour of the dot, per state. Read as a traffic light out of the corner
#: of an eye: gold while it is listening to you, grey while it is not, red when
#: something needs reading. They are the overlay's own colours, so the two
#: visible parts of dictate look like the same product.
STATE_COLOURS = {
    TrayStatus.STARTING: "#7e8794",
    TrayStatus.READY: "#c7ccd6",
    TrayStatus.RESTING: "#7e8794",
    TrayStatus.LISTENING: "#c8a45c",
    TrayStatus.WORKING: "#c8a45c",
    TrayStatus.STOPPING: "#7e8794",
    TrayStatus.ERROR: "#c9705c",
}

_HEADLINE = {
    TrayStatus.STARTING: "starting…",
    TrayStatus.READY: "ready",
    TrayStatus.RESTING: "ready",
    TrayStatus.LISTENING: "listening…",
    TrayStatus.WORKING: "transcribing…",
    TrayStatus.STOPPING: "stopping…",
    TrayStatus.ERROR: "something went wrong",
}


@dataclass(frozen=True)
class TrayState:
    """Everything the icon shows, in one value the app can hand over."""

    status: TrayStatus = TrayStatus.STARTING
    #: The hotkey, in the words `dictate doctor` uses.
    hotkey: str = ""
    #: Whether the transcription model is in the graphics card right now.
    model_resident: bool = False
    #: One line about what went wrong, when status is ERROR.
    detail: str = ""
    #: An update this copy started is running in a window of its own, and this
    #: copy is what it is about to stop and replace. Starting a second one is
    #: the one thing that could hurt, so while this is true the menu will not
    #: offer to.
    updating: bool = False

    @property
    def colour(self) -> str:
        return STATE_COLOURS[self.status]


#: Windows truncates a tooltip at 127 characters and shows nothing at all if it
#: is longer, so this is enforced here rather than discovered on his machine.
TOOLTIP_MAX = 127


def tooltip(state: TrayState) -> str:
    """The hover text. First line what it is doing, second how to talk to it."""
    lines = [f"dictate - {_HEADLINE[state.status]}"]
    if state.status is TrayStatus.ERROR and state.detail:
        lines.append(state.detail)
    elif state.hotkey and state.status in (TrayStatus.READY, TrayStatus.RESTING):
        lines.append(f"hold {state.hotkey} and speak")
    elif state.status is TrayStatus.RESTING:
        lines.append("the graphics card has its memory back")
    text = "\n".join(lines)
    if len(text) > TOOLTIP_MAX:
        text = text[:TOOLTIP_MAX - 1].rstrip() + "…"
    return text


@dataclass(frozen=True)
class MenuItem:
    """One line of the menu. `command` is what the same thing is called when
    typed, and it is shown, because the point is that he ends up knowing it."""

    key: str
    label: str
    command: str = ""
    enabled: bool = True
    default: bool = False
    separator_after: bool = False

    @property
    def text(self) -> str:
        return f"{self.label}\t{self.command}" if self.command else self.label


def status_line(state: TrayState) -> str:
    """The first item: not clickable, there to answer "is it even running?"."""
    text = f"dictate - {_HEADLINE[state.status]}"
    if state.status not in (TrayStatus.ERROR, TrayStatus.STOPPING):
        text += (" (model loaded)" if state.model_resident
                 else " (model unloaded, loads when you press the hotkey)")
    return text


def menu(state: TrayState) -> list[MenuItem]:
    """What right-clicking the icon offers.

    Six lines, in three groups: what it is doing, the two that change whether it
    is running, and the two that change which version it is. None of them needs
    him to have worked out what went wrong first - Stop clears a stuck copy as
    well as a healthy one, because `dictate stop` does.

    **Check for updates changes nothing, ever**, which is why it is offered even
    while an update is already running: it is a report and cannot make anything
    worse. **Update now** is the one item that can, and there is exactly one
    thing it must not do, which is start twice - the second copy would race the
    first over the same folder. So it goes grey the moment one is running.
    """
    return [
        MenuItem(STATUS, status_line(state), enabled=False, separator_after=True),
        MenuItem(STOP, "Stop dictate", "dictate stop", default=True),
        MenuItem(RESTART, "Restart dictate", "dictate stop, dictate run",
                 separator_after=True),
        MenuItem(CHECK, "Check for updates", "dictate update --check"),
        MenuItem(UPDATE, "Update now", "dictate update",
                 enabled=not state.updating, separator_after=True),
        MenuItem(LOG, "Open the log folder"),
    ]


@dataclass
class TrayActions:
    """What the menu items do. Supplied by `app.Application`; the Win32 side
    only ever calls these, so nothing about the application leaks into it."""

    stop: Callable[[], None]
    restart: Callable[[], None]
    open_log: Callable[[], None]
    check_updates: Callable[[], None]
    update_now: Callable[[], None]
    handlers: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.handlers = {STOP: self.stop, RESTART: self.restart,
                         CHECK: self.check_updates, UPDATE: self.update_now,
                         LOG: self.open_log}

    def invoke(self, key: str) -> bool:
        """Run the action for `key`. False if there is nothing to run, which is
        the right answer for the status line and for a stale menu id."""
        handler = self.handlers.get(key)
        if handler is None:
            return False
        handler()
        return True


# ---------------------------------------------------------------------------
# The icon itself
#
# Drawn here, in bytes, rather than shipped as a file: the colour is the status,
# so it has to change while dictate is running, and a .ico in the repository
# would be a binary nobody can review or test. Windows loads it with
# LoadImageW(..., LR_LOADFROMFILE) from the state directory.
# ---------------------------------------------------------------------------


def parse_colour(value: str) -> tuple[int, int, int]:
    """`#rrggbb` -> (r, g, b). The config's colours are already validated by
    `config.validate`, so anything unreadable here is a programming error."""
    text = value.lstrip("#")
    if len(text) != 6:
        raise ValueError(f"{value!r} is not a #rrggbb colour")
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def _disc(size: int) -> list[list[float]]:
    """Coverage of a filled circle, 0.0 to 1.0, sampled 3x3 per pixel so the
    edge is smooth at 16 pixels instead of a staircase."""
    radius = size / 2.0 - 0.6
    centre = size / 2.0
    rows = []
    for y in range(size):
        row = []
        for x in range(size):
            hits = 0
            for sy in range(3):
                for sx in range(3):
                    px = x + (sx + 0.5) / 3.0
                    py = y + (sy + 0.5) / 3.0
                    if (px - centre) ** 2 + (py - centre) ** 2 <= radius ** 2:
                        hits += 1
            row.append(hits / 9.0)
        rows.append(row)
    return rows


def ico_bytes(colour: str, size: int = 16) -> bytes:
    """A single-image .ico of a filled dot in `colour`, 32-bit with alpha.

    The layout is the one Windows documents: an ICONDIR, one ICONDIRENTRY, then
    a BITMAPINFOHEADER whose height is doubled (colour rows plus mask rows), the
    BGRA pixels bottom-up, and a 1-bit AND mask. The mask is all zeroes because
    the alpha channel already carries the shape, and every 32-bit icon Windows
    ships does the same.
    """
    red, green, blue = parse_colour(colour)
    coverage = _disc(size)

    pixels = bytearray()
    for y in range(size - 1, -1, -1):          # bottom-up, as the format wants
        for x in range(size):
            alpha = int(round(coverage[y][x] * 255))
            pixels += bytes((blue, green, red, alpha))

    mask_row = (size + 31) // 32 * 4           # 1 bit per pixel, 4-byte aligned
    mask = bytes(mask_row * size)

    header = struct.pack(
        "<IiiHHIIiiII",
        40,                 # biSize
        size, size * 2,     # biWidth, biHeight (colour + mask)
        1, 32,              # biPlanes, biBitCount
        0, len(pixels) + len(mask),
        0, 0, 0, 0,
    )
    image = header + bytes(pixels) + mask
    directory = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack(
        "<BBBBHHII",
        size if size < 256 else 0, size if size < 256 else 0,
        0, 0, 1, 32, len(image), len(directory) + 16,
    )
    return directory + entry + image
