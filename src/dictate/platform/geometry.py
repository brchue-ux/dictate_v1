"""Overlay placement and internal layout arithmetic.

Split out from the Tk code on purpose: this is the part of the overlay that can
be wrong in a way nobody would notice without a screen, and it is pure
arithmetic, so it gets tested (tests/test_units.py, tests/test_overlay.py) even
though the window itself cannot be.

Two things live here.

* **Placement** - which rectangle the window occupies. This is per *monitor*,
  not per screen: the captions have to appear on the display the user is
  actually working on, so the caller passes the work area of the chosen monitor
  in virtual-desktop coordinates and everything below is relative to that.
* **Layout** - the slab's own internal measurements at a given DPI. Every value
  the config carries is expressed at 100% scaling and multiplied here, so a
  second monitor at 150% gets a caption that is the same physical size rather
  than one that is two thirds as big.

The size knobs (`[overlay] size`, `text_size`, `panel_size` - see
`overlay_size.py`) are a second, independent multiplication applied in the same
place. Keep them that way round: the DPI scaling is what the display demands and
the size knobs are what he chose, and folding either into the other would make
"the same size on every screen" untrue the first time he changed his mind.
"""

from __future__ import annotations

from dataclasses import dataclass

#: A monitor's work area, in virtual-desktop pixels: left, top, right, bottom.
#: Right and bottom are exclusive, which is what GetMonitorInfo's RECT gives.
Rect = tuple[int, int, int, int]

#: The overlay never takes more than this share of the display's working width,
#: however wide `max_width_px` is set. A caption spanning a whole monitor stops
#: being a caption; on a high-DPI display, where `max_width_px` is multiplied up,
#: this is the clamp that actually binds.
MAX_WIDTH_SHARE = 0.72

#: Vertical padding as a fraction of horizontal. Text wants more room at the
#: sides than above and below to read as seated rather than boxed in.
VERTICAL_PADDING_SHARE = 1.0

#: The state word's size as a share of the caption's. It used to be "five points
#: smaller", and a constant subtraction stops being a proportion the moment the
#: panel is resized - at half size it would have eaten most of the difference.
#: 0.72 is what 13pt was to 18pt, so the shipped panel is unchanged by this.
STATUS_SIZE_SHARE = 0.72

#: The state word never shrinks below this, in points. It is a word, not a
#: decoration, and at the smallest size the proportion alone would put it under
#: 9 pixels. The colour bar beside it carries the state anyway.
MIN_STATUS_POINTS = 7.0

#: Padding, as a share of the caption's pixel size, below which the words start
#: to look crammed against the shoulder. In the matched design padding is about
#: 1.08x the type, so this never binds; it exists for the case the size knobs
#: were split for - a box deliberately smaller than the type in it - where it
#: stops "smaller box" from turning into "no breathing room".
MIN_PADDING_SHARE = 0.75


def scaled(value: float, scale: float) -> int:
    """A 100%-scaling pixel value at this monitor's scaling, never below 1."""
    return max(1, round(value * scale))


def caption_points(font_size: float, text_size: float) -> float:
    """The caption's point size once the text knob has been applied.

    `text_size` is the multiplier from `overlay_size.SIZES`; `font_size` is what
    the config says, which describes the panel at `huge`. This is deliberately
    separate from the DPI scaling below: one is a choice, the other is the
    display.
    """
    return font_size * text_size


def status_points(font_size: float, text_size: float) -> float:
    """The state word's point size, in proportion to the caption's."""
    return max(MIN_STATUS_POINTS,
               caption_points(font_size, text_size) * STATUS_SIZE_SHARE)


def points_to_px(points: float, scale: float, minimum: int) -> int:
    """Points at this monitor's scaling, as Tk's negative (pixel) font size.

    There is exactly one of these because the overlay has to configure the real
    fonts at these sizes *before* `plan_slab` can be told how wide the state
    word measures - and two copies of the arithmetic would be two answers.
    """
    return max(minimum, round(points * 96 / 72 * scale))


def caption_px(font_size: float, text_size: float, scale: float) -> int:
    return points_to_px(caption_points(font_size, text_size), scale, 6)


def status_px(font_size: float, text_size: float, scale: float) -> int:
    return points_to_px(status_points(font_size, text_size), scale, 5)


def place_in_rect(
    position: str,
    work: Rect,
    win_w: int,
    win_h: int,
    margin: int,
) -> tuple[int, int]:
    """Top-left corner for a `win_w` x `win_h` window inside `work`.

    Always fully inside the work area, even if the caller asks for a window
    bigger than the monitor. The work area excludes the taskbar, so a
    bottom-centred overlay sits above it rather than under it.
    """
    left, top, right, bottom = work
    area_w = max(1, right - left)
    area_h = max(1, bottom - top)
    win_w = max(1, min(win_w, area_w))
    win_h = max(1, min(win_h, area_h))
    vertical, _, horizontal = position.partition("-")

    if horizontal == "left":
        x = margin
    elif horizontal == "right":
        x = area_w - win_w - margin
    else:
        x = (area_w - win_w) // 2

    y = margin if vertical == "top" else area_h - win_h - margin

    x = max(0, min(x, area_w - win_w))
    y = max(0, min(y, area_h - win_h))
    return left + x, top + y


def overlay_geometry(
    position: str,
    screen_w: int,
    screen_h: int,
    win_w: int,
    win_h: int,
    margin: int,
) -> tuple[int, int]:
    """Placement on a single screen whose origin is (0, 0).

    The whole-screen form of `place_in_rect`, kept because it is the simplest
    statement of the rule and the one the placement tests are written against.
    """
    return place_in_rect(position, (0, 0, screen_w, screen_h), win_w, win_h, margin)


@dataclass(frozen=True)
class SlabLayout:
    """Every measurement the overlay needs for one appearance, in real pixels.

    Computed once per appearance and then left alone - the window does not
    resize or move while captions are arriving, which is the single biggest
    thing that made the old overlay restless in peripheral vision.
    """

    x: int
    y: int
    width: int
    height: int
    #: The unlit shoulder around the lit face: sides and top.
    edge: int
    #: The thicker band along the bottom. The slab sits on it.
    plinth: int
    #: Space between the face's inner edge and anything drawn on it.
    pad_x: int
    pad_y: int
    #: The state bar - a solid block of colour, the full height of the face.
    bar: int
    #: Distance from the face's left edge to where caption text starts. Fixed for
    #: the whole appearance, so the words never shift when the state word does.
    gutter: int
    #: Width available to caption text.
    text_width: int
    #: Font sizes in PIXELS, not points: Tk's negative-size form, so that DPI
    #: scaling is done here rather than inferred by Tk from the primary display.
    caption_px: int
    status_px: int
    line_height: int
    lines: int
    scale: float

    @property
    def face_height(self) -> int:
        return self.height - self.edge - self.plinth


def plan_slab(
    *,
    position: str,
    work: Rect,
    scale: float,
    max_width_px: int,
    margin_px: int,
    edge_px: int,
    padding_px: int,
    font_size: int,
    lines: int,
    status_width_px: int,
    line_height_px: int,
    text_size: float = 1.0,
    panel_size: float = 1.0,
    char_width_px: int = 0,
    max_chars: int = 0,
) -> SlabLayout:
    """Lay the slab out for one monitor.

    Three multiplications, and they are not the same thing.

    * `text_size` is the type knob - it sizes the caption and the state word,
      and through the line height it decides most of the panel's height.
    * `panel_size` is the box knob - the width, the screen margin, the padding
      and the shoulder. Set from the same ladder, and equal to `text_size`
      unless he has deliberately split them (see `overlay_size.py`), because a
      panel whose type shrank on its own is a small line of text floating in a
      large slab.
    * `scale` is that monitor's DPI over 96. It is not a choice at all: it is
      what keeps a panel the same physical size on a display set to 150%, and
      it is applied per appearance, per monitor, exactly as it always was.

    **Where the two knobs are split, two floors keep the result coherent**:
    padding never falls below `MIN_PADDING_SHARE` of the type it surrounds, and
    the box is never so narrow that the `max_chars` the caption path will send
    have nowhere to go. Neither binds when the knobs agree.

    `status_width_px` is the measured width of the widest state word **in the
    status font at this size and scale** - measured rather than estimated
    because the gutter must not clip, and the caller is the only one that can
    measure a real font. `line_height_px` is likewise the font's own line
    spacing, and `char_width_px` its own average advance; a caller with no way
    to measure passes 0 and gets no width floor rather than a guessed one.
    """
    left, top, right, bottom = work
    area_w = max(1, right - left)

    caption = caption_px(font_size, text_size, scale)
    status = status_px(font_size, text_size, scale)
    line_height = max(caption, line_height_px)
    lines = max(1, lines)

    edge = scaled(edge_px * panel_size, scale)
    plinth = edge * 2
    pad_x = max(scaled(padding_px * panel_size, scale),
                round(caption * MIN_PADDING_SHARE))
    pad_y = max(1, round(pad_x * VERTICAL_PADDING_SHARE))
    bar = edge
    margin = scaled(margin_px * panel_size, scale)

    gutter = bar + pad_x + max(0, status_width_px) + pad_x
    width = min(scaled(max_width_px * panel_size, scale),
                max(1, area_w - 2 * margin),
                max(1, round(area_w * MAX_WIDTH_SHARE)))
    if char_width_px > 0 and max_chars > 0:
        # The box has to be able to show the caption tail it will be sent. The
        # reserved lines are what they are, so a panel narrowed under a large
        # caption wraps after a few words and silently clips the rest - and what
        # is clipped is the NEWEST words, off the bottom, while he is still
        # speaking. So the words win over the box - but never off the monitor,
        # and never wider than the clamp that stops a caption spanning a
        # display. At matched sizes `max_chars` fits with room to spare, so this
        # only ever binds when the two knobs have been pulled apart.
        per_line = -(-max_chars // lines)                              # ceiling
        needed = 2 * edge + gutter + pad_x + per_line * char_width_px
        width = max(width, min(needed, max(1, area_w - 2 * margin),
                               max(1, round(area_w * MAX_WIDTH_SHARE))))
    height = edge + pad_y + lines * line_height + pad_y + plinth

    # A monitor narrow enough that the gutter alone would eat the window still
    # has to show words; the text column is never allowed below one character.
    text_width = max(caption, width - 2 * edge - gutter - pad_x)

    x, y = place_in_rect(position, work, width, height, margin)
    return SlabLayout(
        x=x, y=y, width=width, height=height, edge=edge, plinth=plinth,
        pad_x=pad_x, pad_y=pad_y, bar=bar, gutter=gutter, text_width=text_width,
        caption_px=caption, status_px=status, line_height=line_height,
        lines=lines, scale=scale,
    )


def wrap_width_chars(pixel_width: int, font_size: int) -> int:
    """Rough character count that fits on one line.

    Tk needs a wraplength in pixels, but the caption text is also trimmed by
    character count upstream, and the two want to roughly agree. Average glyph
    advance for a UI font is a little over half its point size.
    """
    per_char = max(1.0, font_size * 0.58)
    return max(10, int(pixel_width / per_char))
