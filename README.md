# dictate_v1

Push-to-talk dictation for Windows. Hold a hotkey and speak — the words appear on
screen **while you are still talking**. Let go, and a second or two later clean,
punctuated text is pasted into whatever window you were in.

Everything runs on your own machine. Nothing is sent anywhere, and there are no
per-use charges.

```
   hold hotkey ──► mic ──► the whole take, kept in memory
                     │
                     ├──► live captions on screen  (fast, rough, DISPLAY ONLY —
                     │    they are thrown away when you let go)
                     │
   release ──────────┴──► Whisper large-v3-turbo on your GPU
                                ▼
                          strip the "um"s
                                ▼
                          paste into the window you were in
```

The captions and the pasted text come from two different models, on purpose. The
caption model is fast enough to keep up with your voice but writes in ALL CAPS
with no punctuation and gets the odd word wrong. That is fine, because **caption
text never reaches your document** — the text that gets pasted comes from Whisper
reading the whole recording after you stop.

---

## Setup

Three commands and a config file. On the Windows machine, in PowerShell:

```powershell
# 1. Build whisper.cpp with Vulkan (~20 min the first time, mostly downloading)
powershell -ExecutionPolicy Bypass -File scripts\build-whisper-vulkan.ps1

# 2. Download the two models (1.9 GB)
powershell -ExecutionPolicy Bypass -File scripts\fetch-models.ps1

# 3. Install dictate itself
pip install -e ".[windows]"

# 4. Write a config file you can edit
dictate init

# 5. Check everything is in place - this tells you exactly what is missing
dictate doctor
```

Each script prints the paths to paste into your config when it finishes.
`dictate doctor` checks every one of them and, for anything wrong, says what to
type. Then:

```powershell
dictate run
```

Hold **Ctrl + Alt + Space**, speak, let go. Ctrl+C in that window to quit.

### If something is wrong

`dictate doctor` first. It checks the operating system, Python, the hotkey, the
cleanup rules, the whisper-server binary, the model file (including whether the
download was truncated), the port, the caption model, the Python packages and
your microphones — and puts what to do about each failure at the bottom.

The full log, including whisper.cpp's own output, goes to the file named under
`[logging] file` in your config.

---

## Configuration

One file, `dictate.toml`, with a sane default for everything —
`config/dictate.example.toml` is the annotated copy `dictate init` gives you.
The four paths that need checking are marked `<<< CHECK THIS`; the rest can stay
as it is.

A typo in that file is an error with a message, not a silently ignored setting.

### Cleanup rules

`cleanup-rules.toml`, next to the config, is yours to edit — filler words, filler
phrases, and guarded patterns. Save it and the next thing you dictate uses the
new rules; a broken edit leaves the previous rules running and tells you what is
wrong with the new ones.

**These rules can only ever delete words.** There is no "replace with" option
anywhere in the file, and after the rules run, dictate checks that the words it
is about to paste are a subset of the words Whisper actually heard, in the same
order. If a rule ever breaks that, the whole cleanup is thrown away and Whisper's
text is pasted exactly as it came out. So the worst a mistake in there can do is
remove something you wanted to keep.

Try a rule without dictating:

```powershell
dictate clean --explain "Um, so I was, you know, thinking about it."
```

### Your clipboard is not touched

The default paste method synthesises the characters as keystrokes and never uses
the clipboard at all. (`[paste] method = "clipboard"` is available for the few
apps that mishandle long keystroke runs; that mode saves and restores your
clipboard, and refuses to clobber contents it cannot faithfully put back.)

---

## What was verified, and what was not

This was built on Linux. **There was no Windows machine and no AMD GPU
available**, so the honest split is:

### Verified here — 164 tests, run and passing

```bash
python -m unittest discover -s tests -t .
```

* The whole dictation state machine end to end: press → audio → captions →
  release → transcribe → clean → paste, plus every error path through it
  (transcription failure, paste failure, unexpected exception, captions failing
  to start, captions crashing mid-sentence, an unreadable focused window, an
  overlong utterance, a tap of the hotkey, silence, overlapping utterances).
* That caption text never reaches the injector, and is cleared and destroyed on
  release.
* That the target window is captured at press and not at paste time.
* The resident-backend lifecycle against a **real child process**: start, wait
  for health, slow start, crash → restart, repeated crashes → give up with a
  reason, clean shutdown, and the stop-during-restart deadlock.
* The whisper-server HTTP client against a **real HTTP server**: multipart WAV
  upload, response parsing, HTTP errors, nothing-listening.
* The cleanup pass, including the "can only delete" guarantee — and that every
  shipped rule preserves it on realistic dictation.
* Config and rules validation, WAV encoding, the utterance buffer, overlay
  placement arithmetic, UTF-16 surrogate handling for text injection, hotkey
  parsing, and the doctor's reporting.
* That the package ships no test doubles.

### Not verified — needs the physical machine

Nothing below has been run. It is written from the documented APIs and the
research reports, and it is where to look first if something misbehaves.

* **Anything GPU.** No timing figure here is measured. The 0.4–1.0 s estimate
  comes from a published RX 6750 GRE (RDNA2, Windows, Vulkan) encode of 428 ms on
  a card with half the compute units. `dictate transcribe some.wav` turns that
  estimate into a real number in about a minute.
* **The whisper.cpp Vulkan build itself** — `scripts/build-whisper-vulkan.ps1`
  has never been executed. Its commands follow the researched Stage V recipe.
* **The caption overlay.** The non-activating, click-through, always-on-top
  window is built from the documented Win32 extended styles, but no one has
  watched it fail to steal focus. This is the highest-risk unverified piece,
  because if it does steal focus the paste lands in the wrong window.
* **The global hotkey**, including whether press/release feels right in practice.
* **Microphone capture** through PortAudio.
* **Text injection** into real applications — terminals in particular vary.
* **sherpa-onnx** itself: `pip` was unavailable in the build environment, so the
  streaming Zipformer has never been loaded. The adapter is written to its
  documented Python API.

If any of it is wrong, `dictate doctor` and the log file are the two things to
look at, and the failures should be legible rather than silent.

---

## Credits

[`PinW/whisper-key-local`](https://github.com/PinW/whisper-key-local) (MIT) was
read as prior art for the Windows mechanics — the `platform/<os>/` package split,
the layout-independent `SendInput` + `KEYEVENTF_UNICODE` approach to typing text,
and `global_hotkeys` as the way to get separate key-press and key-release
callbacks. No code was copied. It has no caption overlay (it uses a system tray
and a terminal UI), so that window is built from the Win32 primitives directly.

[whisper.cpp](https://github.com/ggml-org/whisper.cpp) (MIT) and
[sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) (Apache-2.0) do the actual
transcription.

## Further reading

`docs/DESIGN.md` — the settled decisions, the five constraints that break the
product if ignored, the threading model, and what was deliberately not done.
