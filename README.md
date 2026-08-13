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

### Or have it start by itself when you log in

```powershell
dictate autostart enable
```

From then on it is just there: about half a minute after you reach the desktop,
without a window appearing, with the model already loaded — so your first
sentence is as fast as your tenth instead of costing an extra two seconds.

```powershell
dictate autostart status     # is it on, is it running, and did it start?
dictate stop                 # stop the copy that is running, now
dictate autostart disable    # never mind, go back to how it was
```

**What it costs.** dictate keeps `whisper-server` running with the model loaded,
which is **about 1.6 GB of your graphics card's memory, for as long as you are
logged in**. That is the whole reason it is fast, and on a 16 GB card it is
affordable — but it is 1.6 GB a game cannot have. If something starts
stuttering, `dictate autostart disable` is the remedy, and it gives the memory
back at your next logon (or straight away, with `dictate stop`).

Two other things worth knowing:

* **Only one copy ever runs.** If autostart already has it and you type
  `dictate run`, the second one tells you which copy is already running and
  stops, rather than quietly fighting it for the hotkey.
* **If it cannot start at logon, it says so.** It waits and tries again a few
  times first — at logon the graphics driver may still be loading — and if it
  still cannot, it puts a message on screen and writes the reason to
  `%LOCALAPPDATA%\dictate\autostart.log`. `dictate autostart status` prints that
  reason back to you. Nothing disappears into a window that closed.

Turning it off is one command and leaves nothing behind: it is a single Windows
scheduled task, which you can also see and delete in Task Scheduler under the
name `dictate`.

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

If it is set to start when you log in and did not, run `dictate autostart status`
as well: it says whether Windows ran it, what it returned, and what went wrong
the last time it tried.

The full log, including whisper.cpp's own output, goes to the file named under
`[logging] file` in your config. The separate, much shorter record of what
happened at logon is at `%LOCALAPPDATA%\dictate\autostart.log` — separate on
purpose, because a config file that will not load is one of the things that can
go wrong at logon, and the report of that cannot live somewhere the config
points to.

---

## Configuration

One file, `dictate.toml`, with a sane default for everything —
`config/dictate.example.toml` is the annotated copy of it. Setup writes it for
you at `%APPDATA%\dictate\dictate.toml` with the paths already pointing at what
it installed, so there is nothing you have to fill in.

It is yours to edit after that. Setup will not overwrite your version: running
it again only re-points those paths, and leaves every other line — the hotkey,
the overlay colours, anything you have changed — exactly as you left it.

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

### Verified anywhere — 231 tests, run and passing

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
* **That only one copy can run**, with a second real process contending for the
  lock — including that a copy killed outright leaves no lock behind, and that
  the second one is told which copy is already running rather than failing
  obscurely.
* **What the logon task says** — that it triggers on this user's logon, runs in
  his own session without administrator rights, has no execution time limit, and
  starts the interpreter that has no console window.
* **That a logon start that fails writes down why**, retries a bounded number of
  times first, does not retry a broken config file at all, and stands aside when
  a copy is already running.
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
* **That a real, installed Vulkan SDK is found without `VULKAN_SDK` being set.**
  The Vulkan build job installs the actual LunarG SDK, then clears the variable
  and checks the installer still finds the SDK on disk and points itself at it.
  That is the state that used to stop the install dead with a message telling
  you to restart the PC.
* **That a genuinely absent Vulkan SDK is reported as absent** — CI asserts the
  installer never claims it installed, never asks for a restart, and always
  prints a download address.
* **That Windows Task Scheduler accepts the logon task and removing it leaves
  nothing behind.** CI runs the real `dictate autostart enable`, reads it back
  with `dictate autostart status`, then `disable`s it and checks Windows agrees
  it is gone. What that does *not* prove is the part that needs a logon — see
  below.
* **The 231 tests above, on Windows** as well as on Linux — which is where the
  single-instance lock is exercised against Windows' own byte-range locking
  rather than Linux's `flock`.

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
  the C++ build tools. CI machines already have most of those and do not use
  winget at all, so step 2 of setup is the one part of it no machine here has
  ever executed. Every tool it installs is now checked by looking for the tool
  itself afterwards, never by trusting the installer's exit code and never by
  reading an environment variable as a proxy — but the winget calls themselves,
  and the direct download from LunarG that steps in when winget cannot install
  the Vulkan SDK (including the Authenticode check on what arrives), have run
  nowhere.
* **The caption overlay.** The non-activating, click-through, always-on-top
  window is built from the documented Win32 extended styles, but no one has
  watched it fail to steal focus. This is the highest-risk unverified piece,
  because if it does steal focus the paste lands in the wrong window.
* **The global hotkey**, including whether press/release feels right in practice.
* **Microphone capture** through PortAudio.
* **Text injection** into real applications — terminals in particular vary.
* **The logon itself.** CI machines never log on, so nobody has watched the task
  fire, watched dictate come up in an interactive session, or confirmed that no
  console window appears on the way. The three things to check the first morning
  after `dictate autostart enable`: that the hotkey works without you having
  started anything, that nothing flashed on screen, and that
  `dictate autostart status` says `last result: 267009 (it is running right
  now)`.
* **The message box** that a failed logon start puts on screen. It is one
  `MessageBoxW` call and it is wrapped so that failing to show it cannot change
  anything, but it has never been displayed. The log entry behind it is written
  either way, and that part *is* tested.

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
