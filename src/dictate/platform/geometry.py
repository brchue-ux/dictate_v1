"""Overlay placement arithmetic.

Split out from the Tk code on purpose: this is the part of the overlay that can
be wrong in a way nobody would notice without a screen, and it is pure
arithmetic, so it gets tested (tests/test_geometry.py) even though the window
itself cannot be.
"""

from __future__ import annotations


def overlay_geometry(
    position: str,
    screen_w: int,
    screen_h: int,
    win_w: int,
    win_h: int,
    margin: int,
) -> tuple[int, int]:
    """Top-left corner for a `win_w` x `win_h` overlay on a `screen_w` x
    `screen_h` screen. Always fully on screen, even if the caller asks for a
    window wider than the display."""
    win_w = max(1, min(win_w, screen_w))
    win_h = max(1, min(win_h, screen_h))
    vertical, _, horizontal = position.partition("-")

    if horizontal == "left":
        x = margin
    elif horizontal == "right":
        x = screen_w - win_w - margin
    else:
        x = (screen_w - win_w) // 2

    y = margin if vertical == "top" else screen_h - win_h - margin

    x = max(0, min(x, screen_w - win_w))
    y = max(0, min(y, screen_h - win_h))
    return x, y


def wrap_width_chars(pixel_width: int, font_size: int) -> int:
    """Rough character count that fits on one line.

    Tk needs a wraplength in pixels, but the caption text is also trimmed by
    character count upstream, and the two want to roughly agree. Average glyph
    advance for a UI font is a little over half its point size.
    """
    per_char = max(1.0, font_size * 0.58)
    return max(10, int(pixel_width / per_char))
