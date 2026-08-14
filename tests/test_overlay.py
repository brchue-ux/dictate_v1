"""The overlay's decisions, minus the window.

Nobody working on this has Windows, so the Tk window itself cannot be run here.
What CAN be run is everything that decides how it looks and behaves - the fade
curve, the font fallback, the per-monitor layout arithmetic - because all three
were deliberately factored out of the Tk code into pure functions.

The last class in this file is different in kind: it reads the overlay's source
and holds the look's own rules in place, because those rules ("nothing snaps",
"nothing moves while it is being read", "no hairlines") are exactly the sort of
thing that quietly rots when someone changes one line, and none of them can be
observed from here any other way.
"""

from __future__ import annotations

import dataclasses
import re
import unittest
from pathlib import Path

from dictate import config as config_mod, overlay_size
from dictate.errors import ConfigError
from dictate.platform import geometry
from dictate.platform.fade import MIN_FADE_MS, Fade, ease_in_out_cubic, steps
from dictate.platform.fonts import FALLBACKS, choose_font, normalise
from dictate.platform.geometry import (
    MAX_WIDTH_SHARE, overlay_geometry, place_in_rect, plan_slab, scaled,
)

REPO = Path(__file__).resolve().parent.parent
OVERLAY_PATH = REPO / "src" / "dictate" / "platform" / "windows" / "overlay.py"
OVERLAY_SRC = OVERLAY_PATH.read_text(encoding="utf-8")


def code_only(source: str) -> str:
    """`source` with every comment and string literal blanked out in place.

    The overlay file discusses the calls it must never make, at length and on
    purpose. A check that cannot tell an explanation from a call is a check that
    fires on a comment and gets deleted rather than obeyed. Blanking in place
    rather than dropping the tokens keeps every line number and every offset, so
    the "which function is this in" splits below still work.
    """
    import io
    import tokenize

    lines = source.splitlines(keepends=True)
    for kind, _, (r1, c1), (r2, c2), _ in tokenize.generate_tokens(
            io.StringIO(source).readline):
        if kind not in (tokenize.COMMENT, tokenize.STRING):
            continue
        for row in range(r1 - 1, r2):
            line = lines[row]
            start = c1 if row == r1 - 1 else 0
            end = c2 if row == r2 - 1 else len(line.rstrip("\n"))
            lines[row] = (line[:start] + " " * (end - start) + line[end:])
    return "".join(lines)


OVERLAY_CODE = code_only(OVERLAY_SRC)

#: A representative laptop, a 1440p desktop, and a second monitor at 150% sitting
#: to the right of the first - the mixed-DPI case that the position has to get
#: right, because it is the one where a wrong answer puts captions on the wrong
#: screen entirely.
LAPTOP = (0, 0, 1366, 728)
DESKTOP = (0, 0, 2560, 1400)
SECOND_150 = (2560, 0, 4480, 1040)


#: Fira Code: 0.6154 em advance, 1.2308 em line spacing, from the font's own
#: hmtx and hhea tables. The real overlay measures these from Tk rather than
#: assuming them, which is what lets the family be changed without anything else
#: changing; here they stand in for that measurement.
ADVANCE = 0.6154
LINESPACE = 1.2308


def layout(work=DESKTOP, scale=1.0, size=None, text=None, panel=None, **kw):
    """A slab planned with the shipped defaults unless a test says otherwise.

    `size`, `text` and `panel` are size *names*, as they are written in the
    config; the font metrics below follow whichever one is in force, exactly as
    the overlay's own measurement does.
    """
    cfg = config_mod.OverlayConfig()
    text_name, panel_name = overlay_size.effective(
        size or cfg.size, text or cfg.text_size, panel or cfg.panel_size)
    text_size = overlay_size.multiplier(text_name)
    panel_size = overlay_size.multiplier(panel_name)
    font_size = kw.pop("font_size", cfg.font_size)
    caption = geometry.caption_px(font_size, text_size, scale)
    status = geometry.status_px(font_size, text_size, scale)
    args = dict(
        position=cfg.position, work=work, scale=scale,
        max_width_px=cfg.max_width_px, margin_px=cfg.margin_px,
        edge_px=cfg.edge_px, padding_px=cfg.padding_px, font_size=font_size,
        lines=cfg.lines,
        status_width_px=round(len("listening") * ADVANCE * status),
        line_height_px=round(LINESPACE * caption),
        char_width_px=round(ADVANCE * caption),
        max_chars=cfg.max_chars,
        text_size=text_size, panel_size=panel_size,
    )
    args.update(kw)
    return plan_slab(**args)


class FadeCurve(unittest.TestCase):
    def test_it_is_not_a_linear_ramp(self):
        """A linear fade reads as a wipe. The look refuses one."""
        self.assertAlmostEqual(ease_in_out_cubic(0.0), 0.0)
        self.assertAlmostEqual(ease_in_out_cubic(1.0), 1.0)
        self.assertAlmostEqual(ease_in_out_cubic(0.5), 0.5)
        # Slow start, slow settle: the quarter points sit well inside linear.
        self.assertLess(ease_in_out_cubic(0.25), 0.25)
        self.assertGreater(ease_in_out_cubic(0.75), 0.75)

    def test_it_is_monotonic(self):
        values = [ease_in_out_cubic(i / 50) for i in range(51)]
        self.assertEqual(values, sorted(values))

    def test_it_clamps_rather_than_overshooting(self):
        self.assertEqual(ease_in_out_cubic(-3.0), 0.0)
        self.assertEqual(ease_in_out_cubic(9.0), 1.0)

    def test_step_count(self):
        self.assertEqual(steps(260, 16), 16)
        self.assertEqual(steps(420, 16), 26)
        self.assertEqual(steps(5, 16), 1)      # never zero, never a divide by zero
        with self.assertRaises(ValueError):
            steps(100, 0)


class FadeRun(unittest.TestCase):
    def test_a_fade_in_reaches_full_opacity_and_stops(self):
        fade = Fade(step_ms=16)
        fade.to(1.0, 260)
        frames = 0
        while not fade.done and frames < 500:
            fade.advance()
            frames += 1
        self.assertTrue(fade.done)
        self.assertAlmostEqual(fade.alpha, 1.0)
        self.assertEqual(frames, steps(260, 16))

    def test_every_frame_is_inside_the_range(self):
        fade = Fade(step_ms=16)
        fade.to(1.0, 260)
        while not fade.done:
            alpha = fade.advance()
            self.assertGreaterEqual(alpha, 0.0)
            self.assertLessEqual(alpha, 1.0)

    def test_the_fade_out_takes_longer_than_the_fade_in(self):
        """Settled: an object is slower to leave than to arrive, and the fade out
        runs after the paste, so the extra time costs nothing."""
        cfg = config_mod.OverlayConfig()
        self.assertGreater(cfg.fade_out_ms, cfg.fade_in_ms)

    def test_a_reversal_resumes_from_where_it_actually_is(self):
        """Pressing the hotkey again mid-fade-out must not flash back to zero."""
        fade = Fade(step_ms=16)
        fade.jump_to(1.0)
        fade.to(0.0, 420)
        for _ in range(6):
            fade.advance()
        midway = fade.alpha
        self.assertGreater(midway, 0.0)
        self.assertLess(midway, 1.0)

        fade.to(1.0, 260)
        self.assertAlmostEqual(fade.alpha, midway)   # no jump on redirection
        first = fade.advance()
        self.assertGreater(first, midway)            # and it goes back up

    def test_a_short_reversal_takes_proportionally_less_time(self):
        near_top, from_zero = Fade(step_ms=16), Fade(step_ms=16)
        near_top.jump_to(0.9)
        near_top.to(1.0, 260)
        from_zero.jump_to(0.0)
        from_zero.to(1.0, 260)
        self.assertLess(near_top._steps, from_zero._steps)

    def test_jump_to_lands_immediately_and_is_finished(self):
        fade = Fade(step_ms=16)
        fade.to(1.0, 260)
        fade.jump_to(0.0)
        self.assertEqual(fade.alpha, 0.0)
        self.assertTrue(fade.done)

    def test_a_zero_duration_fade_lands_at_once(self):
        """`fade = false` and shutdown both go through this path."""
        fade = Fade(step_ms=16)
        fade.to(1.0, 0)
        self.assertEqual(fade.alpha, 1.0)
        self.assertTrue(fade.done)

    def test_aiming_where_it_already_is_does_nothing(self):
        fade = Fade(step_ms=16)
        fade.jump_to(1.0)
        fade.to(1.0, 260)
        self.assertTrue(fade.done)
        self.assertEqual(fade.alpha, 1.0)


class FontResolution(unittest.TestCase):
    """Tk substitutes a missing family silently. This is what notices."""

    def resolver(self, installed):
        installed = {normalise(f): f for f in installed}

        def resolve(family: str) -> str:
            return installed.get(normalise(family), "MS Sans Serif")
        return resolve

    def test_an_installed_family_is_used_and_nothing_is_said(self):
        choice = choose_font("Fira Code", self.resolver(["Fira Code", "Consolas"]))
        self.assertEqual(choice.family, "Fira Code")
        self.assertTrue(choice.resolved)
        self.assertEqual(choice.report(), "")

    def test_spelling_and_spacing_differences_are_not_a_substitution(self):
        for spelling in ("firacode", "FIRA CODE", "Fira-Code"):
            with self.subTest(spelling=spelling):
                choice = choose_font("Fira Code", self.resolver([spelling]))
                self.assertTrue(choice.resolved)

    def test_a_missing_family_falls_back_and_says_so(self):
        choice = choose_font("Fira Code", self.resolver(["Consolas", "Segoe UI"]))
        self.assertEqual(choice.family, "Consolas")
        self.assertFalse(choice.resolved)
        report = choice.report()
        self.assertIn("Fira Code", report)
        self.assertIn("Consolas", report)
        self.assertIn("not installed", report)

    def test_the_fallback_is_monospace_first(self):
        self.assertEqual(FALLBACKS[0], "Consolas")

    def test_it_skips_a_dead_fallback_for_the_next_one(self):
        choice = choose_font("Fira Code", self.resolver(["Segoe UI"]))
        self.assertEqual(choice.family, "Segoe UI")

    def test_when_nothing_resolves_it_still_returns_something_usable(self):
        choice = choose_font("Fira Code", self.resolver([]))
        self.assertEqual(choice.family, "Fira Code")
        self.assertFalse(choice.resolved)
        self.assertIn("no fallback resolved", choice.report())

    def test_a_resolver_that_raises_is_treated_as_not_installed(self):
        def explode(family):
            raise RuntimeError("Tk is unhappy")

        choice = choose_font("Fira Code", explode)
        self.assertFalse(choice.resolved)
        self.assertTrue(choice.report())

    def test_an_empty_family_falls_back_rather_than_asking_for_nothing(self):
        choice = choose_font("   ", self.resolver(["Consolas"]))
        self.assertEqual(choice.family, "Consolas")

    def test_the_shipped_default_is_the_font_he_asked_for(self):
        self.assertEqual(config_mod.OverlayConfig().font_family, "Fira Code")


class Placement(unittest.TestCase):
    def test_a_second_monitor_puts_the_overlay_on_that_monitor(self):
        """The whole point of following focus: the captions have to land on the
        screen he is working on, not always at x=0."""
        x, y = place_in_rect("bottom-center", SECOND_150, 1080, 140, 64)
        self.assertGreaterEqual(x, SECOND_150[0])
        self.assertLessEqual(x + 1080, SECOND_150[2])
        self.assertEqual(y, SECOND_150[3] - 140 - 64)

    def test_a_monitor_left_of_the_primary_has_negative_coordinates(self):
        left_monitor = (-1920, 0, 0, 1040)
        x, _ = place_in_rect("bottom-center", left_monitor, 1080, 140, 64)
        self.assertLess(x, 0)
        self.assertGreaterEqual(x, -1920)

    def test_the_work_area_is_what_it_sits_in_so_it_clears_the_taskbar(self):
        # A work area 40px shorter than the monitor: the overlay's bottom edge
        # must respect the work area, not the monitor.
        _, y = place_in_rect("bottom-center", (0, 0, 1920, 1040), 1080, 140, 64)
        self.assertEqual(y, 1040 - 140 - 64)

    def test_the_single_screen_form_still_agrees_with_the_rect_form(self):
        self.assertEqual(overlay_geometry("bottom-center", 1920, 1080, 400, 100, 50),
                         place_in_rect("bottom-center", (0, 0, 1920, 1080),
                                       400, 100, 50))


class SlabLayoutArithmetic(unittest.TestCase):
    def test_the_defaults_land_on_a_sane_slab(self):
        """The shipped panel: `compact`, which is five eighths of the pixel
        values in the config - those describe it at `huge`."""
        slab = layout()
        self.assertEqual(slab.width, 675)
        self.assertGreater(slab.height, 60)
        self.assertLess(slab.height, 120)
        self.assertEqual(slab.edge, 5)
        self.assertEqual(slab.plinth, 10)
        self.assertGreater(slab.text_width, 500)

    def test_the_panel_as_it_used_to_ship_is_still_one_word_away(self):
        """`huge` is the old default exactly, so "put it back" is one word and
        the pixel values in the config still mean what they say."""
        slab = layout(size="huge")
        self.assertEqual(slab.width, 1080)
        self.assertEqual(slab.edge, 8)
        self.assertEqual(slab.plinth, 16)
        self.assertEqual(slab.caption_px, round(18 * 96 / 72))

    def test_the_face_plus_the_shoulders_is_exactly_the_window(self):
        slab = layout()
        self.assertEqual(slab.edge + slab.face_height + slab.plinth, slab.height)

    def test_the_text_column_ends_a_padding_short_of_the_face(self):
        slab = layout()
        self.assertEqual(slab.gutter + slab.text_width + slab.pad_x,
                         slab.width - 2 * slab.edge)

    def test_the_gutter_holds_the_bar_the_status_word_and_two_paddings(self):
        slab = layout()
        self.assertEqual(slab.gutter, slab.bar + slab.pad_x
                         + round(len("listening") * ADVANCE * slab.status_px)
                         + slab.pad_x)

    def test_nothing_structural_is_a_hairline(self):
        """The look refuses 1px lines. Every division is real thickness."""
        slab = layout()
        for name in ("edge", "plinth", "bar", "pad_x", "pad_y"):
            with self.subTest(part=name):
                self.assertGreaterEqual(getattr(slab, name), 4)

    def test_150_percent_scaling_makes_everything_bigger_not_smaller(self):
        """The failure this prevents: a caption two thirds the size on a scaled
        second monitor, because the pixel values were read as physical."""
        base = layout(scale=1.0)
        big = layout(work=SECOND_150, scale=1.5)
        self.assertEqual(big.edge, round(base.edge * 1.5))
        # A pixel either way: each value is rounded from the real number at this
        # scale, not from the rounded one, which is the whole point of doing the
        # multiplication on the config value rather than on the laid-out slab.
        self.assertAlmostEqual(big.plinth, base.plinth * 1.5, delta=1)
        self.assertAlmostEqual(big.pad_x, base.pad_x * 1.5, delta=1)
        self.assertAlmostEqual(big.caption_px / base.caption_px, 1.5, places=1)
        self.assertGreater(big.height, base.height)

    def test_a_scaled_monitor_does_not_get_a_slab_that_spans_it(self):
        """Scaling multiplies the width too, so without this clamp a 150% display
        gets a caption running nearly edge to edge."""
        slab = layout(work=SECOND_150, scale=1.5)
        area = SECOND_150[2] - SECOND_150[0]
        self.assertLessEqual(slab.width, round(area * MAX_WIDTH_SHARE))
        self.assertGreaterEqual(slab.x, SECOND_150[0])
        self.assertLessEqual(slab.x + slab.width, SECOND_150[2])

    def test_it_fits_on_a_small_laptop_screen(self):
        slab = layout(work=LAPTOP)
        self.assertLessEqual(slab.width, LAPTOP[2])
        self.assertLessEqual(slab.x + slab.width, LAPTOP[2])
        self.assertLessEqual(slab.y + slab.height, LAPTOP[3])
        self.assertGreaterEqual(slab.text_width, slab.caption_px)

    def test_a_absurdly_narrow_monitor_still_leaves_room_for_words(self):
        slab = layout(work=(0, 0, 320, 240))
        self.assertGreaterEqual(slab.text_width, slab.caption_px)
        self.assertGreaterEqual(slab.x, 0)
        self.assertLessEqual(slab.x + slab.width, 320)

    def test_the_reserved_height_follows_the_line_count(self):
        two = layout(lines=2)
        three = layout(lines=3)
        self.assertEqual(three.height - two.height, two.line_height)

    def test_font_pixel_sizes_come_from_points_the_size_and_the_monitor_dpi(self):
        slab = layout(scale=1.0)
        self.assertEqual(slab.caption_px, round(18 * 0.625 * 96 / 72))
        self.assertGreater(slab.caption_px, slab.status_px)

    def test_scaled_never_rounds_a_real_measurement_away(self):
        self.assertEqual(scaled(8, 1.0), 8)
        self.assertEqual(scaled(8, 1.5), 12)
        self.assertEqual(scaled(1, 0.01), 1)     # never zero


class TheSizeKnobs(unittest.TestCase):
    """One knob that moves everything, and two that pull it apart on purpose.

    None of this can be looked at from here, so what is held instead is the
    thing that would actually go wrong: a panel whose parts stopped being in
    proportion to each other.
    """

    def test_every_rung_is_the_same_design_at_a_different_size(self):
        """The point of one knob. Each measurement keeps its ratio to the type,
        so no size is the one where the padding looks wrong."""
        for name in overlay_size.names():
            with self.subTest(size=name):
                slab = layout(size=name)
                self.assertAlmostEqual(slab.pad_x / slab.caption_px, 1.08, delta=0.12)
                self.assertAlmostEqual(slab.edge / slab.caption_px, 0.33, delta=0.05)
                self.assertAlmostEqual(slab.width / slab.caption_px, 45.0, delta=1.5)

    def test_the_line_holds_about_the_same_words_at_every_size(self):
        """`max_chars` is one number for all of them, so a smaller panel must
        not mean a caption tail that no longer fits on two lines."""
        for name in overlay_size.names():
            with self.subTest(size=name):
                slab = layout(size=name)
                chars = slab.text_width / (slab.caption_px * ADVANCE)
                self.assertAlmostEqual(chars, 59.7, delta=1.5)
                self.assertGreaterEqual(round(chars) * slab.lines,
                                        config_mod.OverlayConfig().max_chars)

    def test_smaller_is_smaller_all_the_way_down_the_ladder(self):
        widths = [layout(size=name).width for name in overlay_size.names()]
        heights = [layout(size=name).height for name in overlay_size.names()]
        self.assertEqual(widths, sorted(widths))
        self.assertEqual(heights, sorted(heights))
        self.assertLess(widths[0] * heights[0], widths[-1] * heights[-1] / 2)

    def test_the_shipped_size_is_about_forty_percent_less_type(self):
        """What he asked for, as a number: the caption text goes from 24 px at
        100% scaling to 15 px."""
        was = layout(size="huge").caption_px
        now = layout().caption_px
        self.assertEqual((was, now), (24, 15))
        self.assertAlmostEqual(1 - now / was, 0.40, delta=0.03)

    def test_the_words_can_be_made_bigger_without_the_box_following(self):
        """The box keeps its own size - its padding, its shoulder, its margin
        off the screen edge - and grows only as far as the bigger words have to
        have. Compare the whole panel at that size: that is the ceiling."""
        both = layout()
        bigger = layout(text="large")
        matched = layout(size="large")
        self.assertGreater(bigger.caption_px, both.caption_px)
        self.assertGreater(bigger.height, both.height)      # the lines grew
        self.assertEqual(bigger.pad_x, both.pad_x)          # the box did not
        self.assertEqual(bigger.edge, both.edge)
        self.assertLess(bigger.width, matched.width)

    def test_the_box_can_be_made_smaller_without_the_words_following(self):
        both = layout()
        smaller = layout(panel="small")
        self.assertEqual(smaller.caption_px, both.caption_px)
        self.assertEqual(smaller.line_height, both.line_height)
        self.assertLess(smaller.width, both.width)
        self.assertLess(smaller.edge, both.edge)
        self.assertLess(smaller.height, both.height)

    def test_a_box_smaller_than_its_type_still_leaves_room_to_breathe(self):
        """The floor that keeps a split pair coherent: padding may not fall
        below a share of the type it surrounds, however small the box knob."""
        slab = layout(text="huge", panel="small")
        self.assertGreaterEqual(slab.pad_x, round(slab.caption_px * 0.75))
        self.assertGreater(slab.pad_x, layout(size="small").pad_x)

    def test_a_box_smaller_than_its_type_still_fits_a_line_of_it(self):
        """The other floor: the words win. A panel narrowed under large type
        would wrap after four words and hide the rest."""
        slab = layout(text="huge", panel="small")
        cfg = config_mod.OverlayConfig()
        per_line = -(-cfg.max_chars // cfg.lines)
        self.assertGreaterEqual(slab.text_width,
                                per_line * round(ADVANCE * slab.caption_px))
        # And no wider than the panel would have been at the text's own size:
        # at matched sizes the caption tail fits with room to spare.
        self.assertLessEqual(slab.width, layout(size="huge").width)

    def test_neither_floor_binds_when_the_knobs_agree(self):
        """They are guard rails for a deliberate mismatch, not part of the
        design - if one of them fired at the shipped size, the numbers in the
        config would no longer be what the panel is."""
        for name in overlay_size.names():
            with self.subTest(size=name):
                cfg = config_mod.OverlayConfig()
                mult = overlay_size.multiplier(name)
                slab = layout(size=name)
                self.assertEqual(slab.pad_x, scaled(cfg.padding_px * mult, 1.0))
                self.assertEqual(slab.width, scaled(cfg.max_width_px * mult, 1.0))

    def test_the_size_knobs_and_the_monitor_scaling_multiply_rather_than_replace(self):
        """The rule the DPI work settled: every pixel value is at 100% scaling
        and gets multiplied by the monitor's. A size knob sits on top of that,
        so a compact panel on a 150% display is 150% of a compact panel."""
        at_100 = layout(size="medium")
        at_150 = layout(size="medium", work=SECOND_150, scale=1.5)
        self.assertEqual(at_150.edge, round(at_100.edge * 1.5))
        self.assertAlmostEqual(at_150.pad_x, at_100.pad_x * 1.5, delta=1)
        self.assertAlmostEqual(at_150.caption_px / at_100.caption_px, 1.5, places=1)

    def test_the_state_word_stays_in_proportion_rather_than_five_points_off(self):
        """It used to be `font_size - 5`, which is a proportion at one size and
        nonsense at half of it."""
        for name in overlay_size.names():
            with self.subTest(size=name):
                slab = layout(size=name)
                self.assertLess(slab.status_px, slab.caption_px)
                self.assertGreaterEqual(slab.status_px, 9)

    def test_a_caller_that_cannot_measure_the_font_gets_no_invented_floor(self):
        """`char_width_px` is measured from the real font or it is not used -
        the one thing it must never be is estimated."""
        narrow = layout(text="huge", panel="small", char_width_px=0)
        self.assertLess(narrow.width, layout(text="huge", panel="small").width)


class OverlayConfigValidation(unittest.TestCase):
    def load(self, **overlay):
        return config_mod.from_mapping({"overlay": overlay})

    def test_the_defaults_validate(self):
        self.assertIsNotNone(config_mod.validate(config_mod.Config()))

    def test_the_panel_is_opaque_by_default(self):
        """Settled: translucency goes muddy over a white page, which is the
        condition this is looked at in most."""
        self.assertEqual(config_mod.OverlayConfig().opacity, 1.0)

    def test_the_old_translucent_behaviour_is_still_reachable(self):
        self.assertEqual(self.load(opacity=0.88).overlay.opacity, 0.88)

    def test_the_fade_can_be_turned_off(self):
        cfg = self.load(fade=False)
        self.assertFalse(cfg.overlay.fade)

    def test_a_fade_short_enough_to_flicker_is_refused_with_a_reason(self):
        with self.assertRaises(ConfigError) as ctx:
            self.load(fade_in_ms=10)
        self.assertIn("flicker", ctx.exception.message)
        self.assertIn(str(MIN_FADE_MS), ctx.exception.remedy)

    def test_a_short_fade_is_fine_once_fading_is_off(self):
        self.assertEqual(self.load(fade=False, fade_in_ms=1).overlay.fade_in_ms, 1)

    def test_a_bad_fade_step_is_refused(self):
        with self.assertRaises(ConfigError):
            self.load(fade_step_ms=0)
        with self.assertRaises(ConfigError):
            self.load(fade_step_ms=5000)

    def test_a_colour_that_is_not_a_colour_is_refused_here_not_inside_tk(self):
        with self.assertRaises(ConfigError) as ctx:
            self.load(accent="brasss")
        self.assertIn("accent", ctx.exception.message)
        self.assertIn("#rrggbb", ctx.exception.remedy)

    def test_every_colour_key_is_checked(self):
        for key in ("background", "foreground", "accent", "edge", "muted", "error"):
            with self.subTest(key=key), self.assertRaises(ConfigError):
                self.load(**{key: "#12345"})

    def test_an_unknown_dpi_mode_is_refused_and_lists_the_real_ones(self):
        with self.assertRaises(ConfigError) as ctx:
            self.load(dpi_awareness="per monitor")
        self.assertIn("per-monitor", ctx.exception.message)

    def test_dpi_scaling_can_be_handed_back_to_windows(self):
        self.assertEqual(self.load(dpi_awareness="off").overlay.dpi_awareness, "off")

    def test_following_the_focused_window_can_be_turned_off(self):
        self.assertFalse(self.load(follow_focus=False).overlay.follow_focus)

    def test_a_silly_line_count_is_refused_with_the_reason(self):
        with self.assertRaises(ConfigError) as ctx:
            self.load(lines=40)
        self.assertIn("taller window all the time", ctx.exception.remedy)

    def test_zero_pixel_measurements_are_refused(self):
        for key in ("margin_px", "max_width_px", "edge_px", "padding_px", "font_size"):
            with self.subTest(key=key), self.assertRaises(ConfigError):
                self.load(**{key: 0})

    def test_the_config_keys_the_example_file_ships_all_still_exist(self):
        """His dictate.toml was written from the example. Removing a key from
        the dataclass would make his existing file fail to load on startup."""
        example = (REPO / "config" / "dictate.example.toml").read_text(
            encoding="utf-8")
        block = example.split("[overlay]", 1)[1].split("\n[", 1)[0]
        keys = set(re.findall(r"^(\w+)\s*=", block, re.MULTILINE))
        known = {f.name for f in dataclasses.fields(config_mod.OverlayConfig)}
        self.assertTrue(keys)
        self.assertEqual(keys - known, set())


class TheLooksOwnRules(unittest.TestCase):
    """The rules the restyle committed to, checked against the source.

    These are not style preferences. Each one is a behaviour that was chosen
    deliberately, that cannot be seen from a machine without Windows, and that
    would be undone by an innocuous-looking edit.
    """

    def test_the_caption_path_never_touches_geometry(self):
        """Rule 1: the window does not move or resize while it is being read.

        Every geometry call must live in the appearance path. If one appears in
        `_apply`, the slab starts twitching every 320 ms again.
        """
        apply_body = OVERLAY_CODE.split("def _apply(", 1)[1].split("\n    def ", 1)[0]
        for banned in ("geometry(", "SetWindowPos", "update_idletasks", ".place("):
            with self.subTest(call=banned):
                self.assertNotIn(banned, apply_body)

    def test_nothing_in_the_file_can_activate_the_window(self):
        """The hard constraint. A prettier overlay that steals focus is a total
        regression, so the restyle is not allowed to have introduced one.

        Checked against the code with the comments and docstrings taken out -
        the file talks about these calls at length, and a check that a *comment*
        mentioning `SetFocus` trips is a check that gets deleted rather than
        obeyed.
        """
        for banned in ("SetForegroundWindow", "SetFocus", "focus_force",
                       "grab_set", "lift", "SetActiveWindow", "BringWindowToTop",
                       "SwitchToThisWindow", "SetWindowRgn"):
            with self.subTest(call=banned):
                self.assertNotIn(banned, OVERLAY_CODE)

    def test_deiconify_is_only_used_in_the_safe_startup_sequence(self):
        """Tk's `deiconify()` goes through SW_SHOW, which activates. It is used
        exactly once, during startup, on a window that is already
        WS_EX_NOACTIVATE and at zero opacity - and never to show a caption."""
        self.assertEqual(OVERLAY_CODE.count("deiconify"), 1)
        run_forever = OVERLAY_CODE.split("def run_forever(", 1)[1].split(
            "\n    def ", 1)[0]
        self.assertIn("deiconify", run_forever)
        styles_first = run_forever.index("_apply_window_styles")
        self.assertLess(styles_first, run_forever.index("deiconify"))

    def test_the_window_is_still_shown_and_placed_without_activating(self):
        self.assertIn("SW_SHOWNOACTIVATE", OVERLAY_SRC)
        self.assertIn("SWP_NOACTIVATE", OVERLAY_SRC)
        self.assertIn("WS_EX_NOACTIVATE", OVERLAY_SRC)
        self.assertIn("WS_EX_TRANSPARENT", OVERLAY_SRC)
        self.assertIn("WS_EX_TOOLWINDOW", OVERLAY_SRC)

    def test_the_loud_failure_when_noactivate_does_not_stick_is_still_there(self):
        self.assertIn("could not be made non-activating", OVERLAY_SRC)
        self.assertIn("pasted into the wrong window", OVERLAY_SRC)

    def test_the_style_is_reasserted_every_time_the_window_appears(self):
        show_body = OVERLAY_CODE.split("def _show(", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("_apply_window_styles()", show_body)

    def test_no_widget_is_given_a_border_or_a_highlight_ring(self):
        """Rule: no hairlines. Tk's defaults for these are 1px and 2px lines."""
        self.assertIn('dict(bd=0, highlightthickness=0, relief="flat")', OVERLAY_SRC)
        self.assertNotIn("relief=\"solid\"", OVERLAY_SRC)
        self.assertNotIn("borderwidth=1", OVERLAY_SRC)

    def test_the_old_cornflower_accent_is_gone_everywhere(self):
        for path in ((REPO / "src").rglob("*.py"),
                     (REPO / "config").glob("*.toml")):
            for file in path:
                with self.subTest(file=file.name):
                    self.assertNotIn("7aa2f7",
                                     file.read_text(encoding="utf-8").lower())

    def test_the_caption_text_is_comfortable_rather_than_maximum_contrast(self):
        """"Easier on the eyes" is a number here, not an adjective: white on near
        black is about 16:1 and glares. This aims at 7-13:1."""
        cfg = config_mod.OverlayConfig()
        ratio = contrast(cfg.foreground, cfg.background)
        self.assertGreater(ratio, 7.0)
        self.assertLess(ratio, 13.0)
        self.assertLess(luminance(cfg.foreground), 0.65)

    def test_every_state_colour_is_readable_on_the_face(self):
        cfg = config_mod.OverlayConfig()
        for key in ("accent", "muted", "error"):
            with self.subTest(colour=key):
                self.assertGreater(contrast(getattr(cfg, key), cfg.background), 3.0)

    def test_the_shoulder_is_darker_than_the_face_so_the_slab_has_an_edge(self):
        """It has to read as an object on a DARK desktop too, where a near-black
        panel on a near-black background has no silhouette at all."""
        cfg = config_mod.OverlayConfig()
        self.assertLess(luminance(cfg.edge), luminance(cfg.background))
        self.assertGreater(luminance(cfg.background) - luminance(cfg.edge), 0.005)

    def test_a_state_change_can_keep_the_words_but_only_what_is_on_screen(self):
        """Constraint 4's new shape. `_apply` may hold the caption through the
        thinking phase - but only text that is visible NOW, so a previous
        utterance's words can never come back up under a new one, and nothing
        outside this file can hand it caption text to display."""
        apply_body = OVERLAY_CODE.split("def _apply(", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("if text is None:", apply_body)
        self.assertIn("self._text if self._visible else \"\"", OVERLAY_SRC)

    def test_collapsing_a_burst_of_updates_does_not_lose_the_last_words(self):
        """Only the newest state in a 30 ms tick is drawn, which is right. But
        if the release arrives in the same tick as the final caption, a state
        that keeps the words has to keep THOSE words - otherwise the panel
        freezes a word or two behind what he actually said."""
        poll = OVERLAY_CODE.split("def _poll(", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("newest_text", poll)
        self.assertIn("if text is None and newest_text is not None:", poll)

    def test_the_words_are_greyed_while_it_thinks_and_read_normally_otherwise(self):
        """He must never take the held caption for the finished text, and the
        error message must stay as readable as it was."""
        colour = OVERLAY_CODE.split("def _text_colour(", 1)[1].split(
            "\n    def ", 1)[0]
        self.assertIn("self.cfg.muted if state is OverlayState.THINKING",
                      colour)
        self.assertIn("else self.cfg.foreground", colour)

    def test_the_greyed_caption_is_still_comfortably_readable(self):
        """Grey enough to read as "not final", not so grey it cannot be read -
        it is on screen for the second he most wants to look at it."""
        cfg = config_mod.OverlayConfig()
        self.assertGreater(contrast(cfg.muted, cfg.background), 3.0)
        self.assertLess(luminance(cfg.muted), luminance(cfg.foreground))

    def test_hiding_the_panel_takes_the_words_with_it(self):
        apply_body = OVERLAY_CODE.split("def _apply(", 1)[1].split("\n    def ", 1)[0]
        # String literals are blanked by `code_only`, so this checks that the
        # assignment is there and that what it assigns is a literal.
        hidden = apply_body.split("OverlayState.HIDDEN", 1)[1].split("return", 1)[0]
        self.assertIn("self._text =", hidden)
        self.assertIn('self._text = ""',
                      OVERLAY_SRC.split("if state is OverlayState.HIDDEN:", 1)[1])

    def test_a_state_arriving_mid_fade_out_turns_the_fade_around(self):
        """The bug this prevents: 'pasted' lingers, the fade out starts, he
        presses the hotkey again - and the new utterance's captions are drawn
        onto a panel that carries on fading to nothing, so he sees the words
        disappear while he is still speaking."""
        apply_body = OVERLAY_CODE.split("def _apply(", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("self._fade.target < self.cfg.opacity", apply_body)
        self.assertIn("_fade_to(self.cfg.opacity, self.cfg.fade_in_ms)", apply_body)

    def test_shutdown_hides_at_once_instead_of_waiting_out_a_fade(self):
        hide_body = OVERLAY_CODE.split("def _hide(", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("_closing.is_set()", hide_body)
        self.assertIn("_hide_now()", hide_body)

    def test_set_state_only_enqueues_so_the_caption_thread_never_waits(self):
        body = OVERLAY_CODE.split("def set_state(", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("self._queue.put(", body)
        for banned in ("Lock", "acquire", "join(", "sleep", "self._root"):
            with self.subTest(call=banned):
                self.assertNotIn(banned, body)


def luminance(colour: str) -> float:
    channels = [int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
              for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(a: str, b: str) -> float:
    la, lb = luminance(a), luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


if __name__ == "__main__":
    unittest.main()
