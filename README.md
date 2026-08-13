# dictate_v1

[![CI](https://github.com/brchue-ux/dictate_v1/actions/workflows/ci.yml/badge.svg)](https://github.com/brchue-ux/dictate_v1/actions/workflows/ci.yml)
— that badge means *it compiles on Windows and the tests pass there*. It is not a
GPU check; the machines that run it have no graphics card. See
[what was verified, and what was not](#what-was-verified-and-what-was-not).

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

**Double-click `setup.cmd`.**

That is the whole thing. Windows will ask for permission to install software —
click **Yes** — and then it runs by itself for about half an hour, mostly
downloading. You do not have to answer anything else, and there is nothing to
edit afterwards.

If you would rather see it in a window you started yourself, this is the same
thing:

```powershell
powershell -ExecutionPolicy Bypass -File setup.ps1
```

### What it is doing, and roughly how long each part takes

| | | |
|---|---|---|
| 1 | **Checks this PC** — Windows version, your graphics driver, free disk space | seconds |
| 2 | **Installs the build tools** — Python, Git, CMake, the Vulkan SDK, the C++ compiler. Anything already installed is left alone | 5–15 min |
| 3 | **Builds whisper.cpp** with the Vulkan backend, from source | 5–15 min |
| 4 | **Downloads the two models** (1.9 GB) | 5–20 min |
| 5 | **Installs dictate** and writes a config file with every path already filled in | 1 min |
| 6 | **Checks it actually works** — can Vulkan see your card, does the transcriber start, does a test clip come back as the right words | 1–2 min |

It needs about 25 GB of free space and, at the end, prints the one thing you
type to start dictating:

```powershell
dictate run
```

Hold **Ctrl + Alt + Space**, speak, let go. Ctrl+C in that window to quit.

### If setup stops part way

It is meant to. Setup never carries on past something that went wrong, and the
last thing on screen is always the problem in one sentence and what to do about
it. Do that thing, then **run setup again** — it keeps everything it has already
done and carries on from where it stopped. A download that died at 90% resumes
at 90%; a compile that finished does not happen twice.

Two of them are worth knowing in advance:

* **"Restart the PC and run setup again."** Some installers only become visible
  to Windows after a restart. This is not setup being cautious — it genuinely
  cannot see the tool until you do.
* **"This window does not have administrator rights."** Close it, right-click
  **Windows PowerShell**, choose **Run as administrator**, and run it again.
  (Double-clicking `setup.cmd` asks for this on your behalf.)

The full technical detail of every run, including the compiler's own output,
goes to `C:\dictate-gpu\setup-log.txt`. Nothing you need is only on screen.

### Re-running one part on its own

```powershell
# Just check everything again - useful after a driver update
powershell -ExecutionPolicy Bypass -File setup.ps1 -Only verify

# Rebuild whisper.cpp at a known-good version, if a new one ever misbehaves
powershell -ExecutionPolicy Bypass -File setup.ps1 -Only build,verify -Ref v1.9.2 -Rebuild
```

`scripts\build-whisper-vulkan.ps1` and `scripts\fetch-models.ps1` still work and
still do what their names say; they now run the same code as the step of setup
that does that job, rather than a second copy of it.

### If something is wrong later

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
available.** Since then a Windows CI build has been added, which moves several
things out of "written carefully, never run" and into "run on real Windows". The
GPU is not one of them, and cannot be: GitHub's Windows machines have no
graphics card in them at all.

So there are now three lists, not two.

### Verified anywhere — 165 tests, run and passing

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

### Verified on real Windows — by CI, on every change

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs on GitHub's hosted
Windows machines. These are things that used to be on the "never run" list:

* **whisper.cpp compiles from source with the Vulkan backend on Windows**, and
  the Vulkan backend really is in the build — not merely asked for. The CI job
  runs `setup.ps1 -Only build`, the same code this PC runs, so a difference
  between them is a difference in the PC and not a stale script.
* **The binary that comes out transcribes audio correctly** (`assets/jfk.wav`,
  through a small model, on the processor).
* **dictate starts and drives that real `whisper-server` end to end** — the
  resident process supervision, the health wait, the WAV encoding and the HTTP
  client, all against the actual compiled binary.
* **`pip install -e ".[windows]"` resolves and installs on Windows**, and
  sounddevice, numpy, sherpa-onnx and global-hotkeys all import.
* **The streaming Zipformer loads and produces caption text** from the bundled
  clip, and closing a caption session really does destroy that text.
* **The installer's own logic**, on Windows PowerShell 5.1 — the version that
  ships with Windows — including a download that is genuinely interrupted
  half-way and resumed, and the edit it makes to your config file.
* **The 165 tests above, on Windows** as well as on Linux.

### Still not verified — needs this actual PC

Nothing below has been run anywhere. It is where to look first if something
misbehaves.

* **Anything GPU.** CI machines have no graphics card, so every transcription
  above ran on a processor. No timing figure here is measured. The 0.4–1.0 s
  estimate comes from a published RX 6750 GRE (RDNA2, Windows, Vulkan) encode of
  428 ms on a card with half the compute units. **Step 6 of setup answers this
  in about a minute**, and so does `dictate transcribe some.wav` at any time.
  A green tick on CI says nothing whatsoever about the GPU.
* **The toolchain install** — winget fetching Python, CMake, the Vulkan SDK and
  the C++ build tools. CI machines already have most of those and do not have
  winget at all, so step 2 of setup is the one part of it no machine here has
  ever executed. It is written to check what is actually installed afterwards
  rather than to trust the installer's exit code.
* **The caption overlay.** The non-activating, click-through, always-on-top
  window is built from the documented Win32 extended styles, but no one has
  watched it fail to steal focus. This is the highest-risk unverified piece,
  because if it does steal focus the paste lands in the wrong window.
* **The global hotkey**, including whether press/release feels right in practice.
* **Microphone capture** through PortAudio.
* **Text injection** into real applications — terminals in particular vary.

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
