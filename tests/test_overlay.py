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

import re
import unittest
from pathlib import Path

from dictate import config as config_mod
from dictate.errors import ConfigError
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


def layout(work=DESKTOP, scale=1.0, **kw):
    """A slab planned with the shipped defaults unless a test says otherwise."""
    cfg = config_mod.OverlayConfig()
    args = dict(
        position=cfg.position, work=work, scale=scale,
        max_width_px=cfg.max_width_px, margin_px=cfg.margin_px,
        edge_px=cfg.edge_px, padding_px=cfg.padding_px, font_size=cfg.font_size,
        status_size=cfg.font_size - 5, lines=cfg.lines,
        # Fira Code: 0.6154 em advance, 1.2308 em line spacing, from the font's
        # own hmtx and hhea tables. The real overlay measures these from Tk.
        status_width_px=round(len("listening") * 0.6154 * 13 * 96 / 72 * scale),
        line_height_px=round(1.2308 * cfg.font_size * 96 / 72 * scale),
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
        slab = layout()
        self.assertEqual(slab.width, 1080)
        self.assertGreater(slab.height, 100)
        self.assertLess(slab.height, 200)
        self.assertEqual(slab.edge, 8)
        self.assertEqual(slab.plinth, 16)
        self.assertGreater(slab.text_width, 700)

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
                         + round(len("listening") * 0.6154 * 13 * 96 / 72)
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
        self.assertEqual(big.plinth, round(base.plinth * 1.5))
        self.assertEqual(big.pad_x, round(base.pad_x * 1.5))
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

    def test_font_pixel_sizes_come_from_points_and_the_monitor_dpi(self):
        slab = layout(scale=1.0)
        self.assertEqual(slab.caption_px, round(18 * 96 / 72))
        self.assertGreater(slab.caption_px, slab.status_px)

    def test_scaled_never_rounds_a_real_measurement_away(self):
        self.assertEqual(scaled(8, 1.0), 8)
        self.assertEqual(scaled(8, 1.5), 12)
        self.assertEqual(scaled(1, 0.01), 1)     # never zero


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
        known = {f.name for f in __import__("dataclasses").fields(
            config_mod.OverlayConfig)}
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
