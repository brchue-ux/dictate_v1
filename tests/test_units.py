"""The small pure pieces: audio buffering, WAV encoding, overlay placement,
keystroke planning, hotkey parsing, and the doctor's reporting.

Each of these is a place where a mistake would be invisible without the real
hardware, which is exactly why they were factored out of the platform code.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from dictate.audio.buffer import UtteranceBuffer
from dictate.audio import wav
from dictate.doctor import CheckResult, Status, format_report, worst
from dictate.errors import ConfigError
from dictate.platform.geometry import overlay_geometry, wrap_width_chars
from dictate.platform.hotkey_spec import describe, normalise
from dictate.platform.injection_plan import KeyEvent, chunk, plan_text

SR = 16000


class Buffer(unittest.TestCase):
    def test_accumulates_and_reports_duration(self):
        buf = UtteranceBuffer(SR, 10)
        buf.append(b"\x00\x00" * SR)          # one second
        self.assertEqual(buf.nbytes, SR * 2)
        self.assertAlmostEqual(buf.duration_s, 1.0)
        self.assertEqual(len(buf.pcm()), SR * 2)

    def test_reset_empties_it(self):
        buf = UtteranceBuffer(SR, 10)
        buf.append(b"\x00\x00" * 100)
        buf.reset()
        self.assertEqual(buf.pcm(), b"")
        self.assertEqual(buf.nbytes, 0)
        self.assertFalse(buf.overflowed)

    def test_the_ceiling_truncates_and_flags(self):
        buf = UtteranceBuffer(SR, 0.5)        # 8000 samples = 16000 bytes
        self.assertFalse(buf.append(b"\x00\x00" * SR))
        self.assertTrue(buf.overflowed)
        self.assertEqual(buf.nbytes, 16000)

    def test_appending_after_overflow_is_dropped_not_grown(self):
        buf = UtteranceBuffer(SR, 0.5)
        buf.append(b"\x00\x00" * SR)
        buf.append(b"\x00\x00" * SR)
        self.assertEqual(buf.nbytes, 16000)

    def test_pcm_is_stable_across_calls(self):
        buf = UtteranceBuffer(SR, 10)
        for _ in range(5):
            buf.append(b"\x01\x02")
        self.assertEqual(buf.pcm(), b"\x01\x02" * 5)
        self.assertEqual(buf.pcm(), b"\x01\x02" * 5)

    def test_rejects_nonsense_construction(self):
        with self.assertRaises(ValueError):
            UtteranceBuffer(0, 10)
        with self.assertRaises(ValueError):
            UtteranceBuffer(SR, 0)


class Wav(unittest.TestCase):
    def test_round_trips_through_a_real_riff_container(self):
        pcm = bytes(range(256)) * 8
        data = wav.pcm16_to_wav(pcm, SR)
        self.assertTrue(data.startswith(b"RIFF"))
        self.assertIn(b"WAVE", data[:16])
        back, rate = wav.wav_to_pcm16(data)
        self.assertEqual(back, pcm)
        self.assertEqual(rate, SR)

    def test_a_torn_final_frame_is_truncated_not_written(self):
        data = wav.pcm16_to_wav(b"\x01\x02\x03", SR)     # 3 bytes = 1.5 samples
        back, _ = wav.wav_to_pcm16(data)
        self.assertEqual(back, b"\x01\x02")

    def test_silence_is_the_right_length(self):
        self.assertEqual(len(wav.silence(1.0, SR)), SR * 2)

    def test_pcm_to_float_and_back(self):
        pcm = wav.float32_to_pcm16([0.0, 0.5, -0.5, 1.0, -1.0])
        floats = wav.pcm16_to_float32(pcm)
        self.assertEqual(len(floats), 5)
        self.assertAlmostEqual(floats[0], 0.0)
        self.assertAlmostEqual(floats[1], 0.5, places=4)
        self.assertAlmostEqual(floats[2], -0.5, places=4)

    def test_float_conversion_clips_rather_than_wrapping(self):
        pcm = wav.float32_to_pcm16([5.0, -5.0])
        floats = wav.pcm16_to_float32(pcm)
        self.assertLessEqual(floats[0], 1.0)
        self.assertGreaterEqual(floats[1], -1.0)

    def test_stereo_input_is_downmixed_to_mono(self):
        import io
        import wave as wave_mod

        buf = io.BytesIO()
        with wave_mod.open(buf, "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes(wav.float32_to_pcm16([0.5, -0.5, 0.25, 0.25]))
        pcm, rate = wav.wav_to_pcm16(buf.getvalue())
        self.assertEqual(rate, SR)
        self.assertEqual(len(pcm), 4)      # two frames, one channel

    def test_non_16_bit_audio_is_refused_clearly(self):
        import io
        import wave as wave_mod

        buf = io.BytesIO()
        with wave_mod.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(1)
            w.setframerate(SR)
            w.writeframes(b"\x00" * 10)
        with self.assertRaises(ValueError):
            wav.wav_to_pcm16(buf.getvalue())


class Geometry(unittest.TestCase):
    def test_bottom_center_on_a_1440p_screen(self):
        x, y = overlay_geometry("bottom-center", 2560, 1440, 1100, 90, 90)
        self.assertEqual(x, (2560 - 1100) // 2)
        self.assertEqual(y, 1440 - 90 - 90)

    def test_each_corner(self):
        self.assertEqual(overlay_geometry("top-left", 1920, 1080, 400, 100, 50), (50, 50))
        self.assertEqual(overlay_geometry("top-right", 1920, 1080, 400, 100, 50),
                         (1920 - 400 - 50, 50))
        self.assertEqual(overlay_geometry("bottom-left", 1920, 1080, 400, 100, 50),
                         (50, 1080 - 100 - 50))
        self.assertEqual(overlay_geometry("bottom-right", 1920, 1080, 400, 100, 50),
                         (1920 - 400 - 50, 1080 - 100 - 50))

    def test_a_window_wider_than_the_screen_is_still_fully_on_screen(self):
        x, y = overlay_geometry("bottom-center", 800, 600, 4000, 200, 90)
        self.assertEqual(x, 0)
        self.assertGreaterEqual(y, 0)

    def test_a_huge_margin_does_not_push_it_off_screen(self):
        x, y = overlay_geometry("bottom-right", 1920, 1080, 400, 100, 5000)
        self.assertGreaterEqual(x, 0)
        self.assertGreaterEqual(y, 0)
        self.assertLessEqual(x + 400, 1920)

    def test_wrap_width_is_sane(self):
        self.assertGreater(wrap_width_chars(1100, 20), 40)
        self.assertLess(wrap_width_chars(1100, 20), 200)
        self.assertGreaterEqual(wrap_width_chars(10, 200), 10)


class InjectionPlan(unittest.TestCase):
    def test_a_plain_character_is_a_down_and_an_up(self):
        events = plan_text("a")
        self.assertEqual(events, [KeyEvent("unicode", ord("a"), False),
                                  KeyEvent("unicode", ord("a"), True)])

    def test_every_character_of_a_sentence_is_represented(self):
        text = "Hello, world."
        events = plan_text(text)
        self.assertEqual(len(events), len(text) * 2)

    def test_non_ascii_goes_through_as_unicode(self):
        events = plan_text("é")
        self.assertEqual(events[0].code, ord("é"))

    def test_an_astral_character_becomes_a_surrogate_pair(self):
        events = plan_text("😀")             # U+1F600
        self.assertEqual(len(events), 4)     # two code units, down+up each
        self.assertEqual(events[0].code, 0xD83D)
        self.assertEqual(events[2].code, 0xDE00)

    def test_newline_is_a_return_keypress_not_a_unicode_character(self):
        events = plan_text("\n", allow_return=True)
        self.assertEqual([e.kind for e in events], ["vk", "vk"])
        self.assertEqual(events[0].code, 0x0D)

    def test_crlf_is_one_return_not_two(self):
        self.assertEqual(len(plan_text("\r\n", allow_return=True)), 2)

    def test_tab_is_a_tab_key(self):
        self.assertEqual(plan_text("\t", allow_return=True)[0].code, 0x09)

    def test_by_default_nothing_planned_can_press_a_key(self):
        """The default is not "a Return is unlikely", it is "there is no Return
        in the plan". A newline that reached here from anywhere - Whisper, a
        rule he wrote, a stage nobody has thought of - is a space."""
        for text in ("a\nb", "a\r\nb", "a\tb", "a b", "a\vb"):
            with self.subTest(text=text):
                events = plan_text(text)
                self.assertEqual([e.kind for e in events],
                                 ["unicode"] * len(events))
                self.assertNotIn(0x0D, [e.code for e in events])
                self.assertNotIn(0x09, [e.code for e in events])

    def test_the_line_break_becomes_a_space_rather_than_disappearing(self):
        """Flattened, not dropped: "one.\\ntwo." is still two sentences."""
        events = plan_text("a\nb")
        self.assertEqual("".join(chr(e.code) for e in events if not e.up), "a b")

    def test_empty_text_plans_nothing(self):
        self.assertEqual(plan_text(""), [])

    def test_chunking_covers_everything_exactly_once(self):
        events = plan_text("the quick brown fox")
        batches = chunk(events, 7)
        self.assertEqual([e for b in batches for e in b], events)
        self.assertTrue(all(len(b) <= 7 for b in batches))

    def test_chunk_size_must_be_positive(self):
        with self.assertRaises(ValueError):
            chunk(plan_text("x"), 0)


class HotkeySpec(unittest.TestCase):
    def test_normalises_the_common_spellings(self):
        self.assertEqual(normalise("Ctrl + Alt + Space"), "control + alt + space")
        self.assertEqual(normalise("CONTROL+ALT+SPACE"), "control + alt + space")
        self.assertEqual(normalise("win + shift + a"), "shift + window + a")

    def test_hyphens_work_too(self):
        self.assertEqual(normalise("ctrl-alt-space"), "control + alt + space")

    def test_modifier_order_is_canonical(self):
        self.assertEqual(normalise("alt + ctrl + space"), normalise("ctrl + alt + space"))

    def test_duplicates_are_collapsed(self):
        self.assertEqual(normalise("ctrl + ctrl + space"), "control + space")

    def test_a_bare_key_is_allowed(self):
        self.assertEqual(normalise("f13"), "f13")

    def test_modifier_only_is_refused_with_a_reason(self):
        with self.assertRaises(ConfigError) as ctx:
            normalise("ctrl + alt")
        self.assertIn("modifier", ctx.exception.message)

    def test_two_normal_keys_is_refused(self):
        with self.assertRaises(ConfigError):
            normalise("ctrl + a + b")

    def test_empty_is_refused(self):
        with self.assertRaises(ConfigError):
            normalise("  +  ")

    def test_human_description(self):
        self.assertEqual(describe("ctrl+alt+space"), "Ctrl + Alt + Space")


class DoctorReport(unittest.TestCase):
    def test_all_ok_says_so(self):
        report = format_report([CheckResult("Thing", Status.OK, "fine")])
        self.assertIn("[ ok ]", report)
        self.assertIn("Everything dictate needs is in place.", report)

    def test_a_failure_puts_the_remedy_last(self):
        report = format_report([
            CheckResult("Good", Status.OK),
            CheckResult("Model", Status.FAIL, "not found at C:\\x",
                        "Download it:\n  scripts\\fetch-models.ps1"),
        ])
        self.assertIn("[FAIL] Model", report)
        self.assertIn("What to do:", report)
        self.assertTrue(report.rstrip().endswith("scripts\\fetch-models.ps1"))

    def test_a_warning_still_gets_its_remedy(self):
        report = format_report([CheckResult("Captions", Status.WARN, "missing", "download")])
        self.assertIn("[warn]", report)
        self.assertIn("download", report)

    def test_the_graphics_card_check_answers_am_i_using_your_vram_right_now(self):
        from dictate import config as config_mod
        from dictate.doctor import check_idle_release

        # A port nothing is listening on: dictate is not holding any memory.
        cfg = config_mod.from_mapping({"whisper": {"port": 65533}})
        result = check_idle_release(cfg)
        self.assertIs(result.status, Status.OK)
        self.assertIn("5 minute", result.detail)
        self.assertIn("no transcription model is loaded right now", result.detail)

    def test_the_graphics_card_check_says_when_release_is_switched_off(self):
        from dictate import config as config_mod
        from dictate.doctor import check_idle_release

        cfg = config_mod.from_mapping({"whisper": {"idle_release_minutes": 0}})
        detail = check_idle_release(cfg).detail
        self.assertIn("stays loaded", detail)
        self.assertIn("idle_release_minutes = 0", detail)

    def test_the_rules_check_notices_an_old_copy_that_still_eats_real_speech(self):
        """`dictate init` never overwrites a rules file that already exists, so
        fixing the shipped one does not reach a copy taken before the fix. The
        cleanup pass cannot notice - deleting is what it is for - so this is the
        only thing that tells him his sentences are losing their middle."""
        import tempfile

        from dictate import config as config_mod
        from dictate.doctor import check_cleanup_rules

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cleanup-rules.toml"
            path.write_text('filler_phrases = ["you know", "kind of"]\n', encoding="utf-8")
            cfg = config_mod.from_mapping({"cleanup": {"rules_file": str(path)}})
            result = check_cleanup_rules(cfg)
            self.assertIs(result.status, Status.WARN)
            self.assertIn('"you know"', result.detail)
            self.assertIn('"kind of"', result.detail)
            self.assertIn("Do the answer?", result.detail)
            self.assertIn("filler_phrases", result.remedy or result.detail)

    def test_the_rules_check_is_quiet_about_the_rules_that_ship(self):
        # Nothing may report a healthy system as broken: the shipped file is
        # exactly what the warning above tells him to move to.
        from dictate import config as config_mod
        from dictate.doctor import check_cleanup_rules

        shipped = Path(__file__).resolve().parent.parent / "config" / "cleanup-rules.toml"
        cfg = config_mod.from_mapping({"cleanup": {"rules_file": str(shipped)}})
        self.assertIs(check_cleanup_rules(cfg).status, Status.OK)

    def test_the_import_shadow_check_is_quiet_about_an_ordinary_folder(self):
        import tempfile

        from dictate.doctor import check_import_shadow

        with tempfile.TemporaryDirectory() as tmp:
            result = check_import_shadow(cwd=tmp)
        self.assertIs(result.status, Status.OK)
        self.assertIn("nothing", result.detail)

    def test_the_import_shadow_check_catches_a_fake_dictate_py(self):
        """The bug this exists to catch: something called `dictate` sitting
        in the directory a command is run from is found before the installed
        package - and finding that out must never mean importing it."""
        import tempfile

        from dictate.doctor import check_import_shadow

        with tempfile.TemporaryDirectory() as tmp:
            impostor = Path(tmp) / "dictate.py"
            impostor.write_text(
                "raise SystemExit('this must never be imported to find out')\n",
                encoding="utf-8",
            )
            result = check_import_shadow(cwd=tmp)
        self.assertIs(result.status, Status.FAIL)
        self.assertIn(str(impostor), result.detail)
        self.assertIn("not the installed", result.detail)

    def test_the_import_shadow_check_catches_a_fake_dictate_package(self):
        import tempfile

        from dictate.doctor import check_import_shadow

        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp) / "dictate"
            pkg.mkdir()
            (pkg / "__init__.py").write_text(
                "raise SystemExit('this must never be imported to find out')\n",
                encoding="utf-8",
            )
            result = check_import_shadow(cwd=tmp)
        self.assertIs(result.status, Status.FAIL)
        self.assertIn(str(pkg / "__init__.py"), result.detail)

    def test_worst_ranks_correctly(self):
        self.assertEqual(worst([CheckResult("a", Status.OK)]), Status.OK)
        self.assertEqual(worst([CheckResult("a", Status.OK),
                                CheckResult("b", Status.WARN)]), Status.WARN)
        self.assertEqual(worst([CheckResult("a", Status.WARN),
                                CheckResult("b", Status.FAIL)]), Status.FAIL)
        self.assertEqual(worst([]), Status.OK)


if __name__ == "__main__":
    unittest.main()
