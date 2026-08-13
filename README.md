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
without a window appearing, ready for the hotkey. You never type anything to
start dictating again.

```powershell
dictate autostart status     # is it on, is it running, and did it start?
dictate stop                 # stop the copy that is running, now
dictate autostart disable    # never mind, go back to how it was
```

**What it costs.** Almost nothing while you are not dictating. The 1.6 GB of
graphics memory is handed back after five idle minutes either way — see [your
graphics card is only busy while you are
dictating](#your-graphics-card-is-only-busy-while-you-are-dictating) — so
leaving this on is not the same as leaving that memory tied up all day, and
there is no reason to turn it off before a game. What stays is one background
Python process, a few tens of megabytes of ordinary memory, waiting for a
keypress.

The one case where it does cost you the card: if you have set
`idle_release_minutes = 0` to keep the model loaded permanently, then starting
at logon means it is loaded from logon. Then it *is* 1.6 GB all day, and
`dictate autostart disable` — or setting that back to 5 — is the remedy.

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

### Getting a newer version

```
dictate update
```

Seconds, not the half hour the first install took. It fetches the current source,
tells you what changed in the words of the changes themselves, puts it in place,
and starts dictate again if it was running:

```
install folder:  C:\dictate_v1
                 (this is the folder Python loads dictate from)
this copy:       0f6ae00 on main, recorded 2026-08-12 22:31:04
latest on main:  bd4d941  Make an orphaned whisper-server impossible

What changed:
  - Make an orphaned whisper-server impossible, and give him one command out
    of any stuck state
  - tidy: the tray leaves nothing half-alive when it cannot start

Fetching the new version…
29 file(s) to update.
```

**It does not rebuild anything.** The build tools, the compiled `whisper.cpp` and
the models live in `C:\dictate-gpu` and do not change when the application does,
so none of them are touched — that is the whole reason this takes seconds. If a
future change ever does need one of them, it will say so and name the exact
`setup.ps1 -Only <step>` to run. Your settings in `%APPDATA%\dictate` are never
opened either.

**The first time, it will ask you to sign in to GitHub.** The source is in a
private repository, and your browser only gets in because you are already signed
in there — a command has no such luck. So:

```powershell
winget install --id GitHub.cli     # if you do not have it
gh auth login                      # opens your browser; once, ever
```

Choose **GitHub.com**, then **HTTPS**, then **Login with a web browser**. Windows
remembers it from then on, and `dictate update` never asks again. dictate itself
never sees, prints or stores the credential — the GitHub CLI holds it, in the
same place Windows keeps your other sign-ins.

Two things worth knowing:

* `dictate update --check` says what would change and changes nothing.
* `dictate update --restore` puts back the version that was there before the last
  update. A complete copy of your install folder is kept every time anything is
  changed, at `%LOCALAPPDATA%\dictate\updates`, so there is always a way back.

If you have edited a file inside `C:\dictate_v1` yourself, it stops and names the
file rather than overwriting it. `dictate update --force` goes ahead anyway, and
still keeps the copy.

### If anything is ever stuck

```
dictate stop
```

That is the whole answer, and it is the only command worth remembering. It does
not matter what went wrong — dictate will not start, a window was closed with
the X, something says the transcription port is already in use, a copy is
running that you cannot see. `dictate stop` looks at each thing dictate can
leave behind and clears whichever of them is there:

- a copy that is running is asked to shut down, exactly the way Ctrl+C asks;
- one that will not answer is ended, **together with the whisper-server it
  owns** — never the parent on its own, because that is what strands a
  transcription process holding your graphics card's memory;
- a whisper-server left over from an earlier run is ended too;
- and if something that is *not* dictate is holding the port, it says which
  program, and prints the exact command to type. It never ends anything that is
  not ours.

You can also right-click **the dictate icon by the clock** — it is there
whenever dictate is running, including when it started by itself at logon and
there is no window anywhere. It shows what dictate is doing (grey while it
waits, gold while it is listening to you, red if something needs reading), and
carries **Stop**, **Restart** and **Open the log folder**. Each one is labelled
with the command that does the same thing, so the icon teaches you the commands
rather than replacing them. Turn it off with `[tray] enabled = false` if you
would rather not have it.

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

### The caption window

The words that appear while you are speaking come up in a panel at the bottom of
the screen. To look at it without dictating — it shows sample text and runs the
whole appear, fill, clear and fade cycle:

```powershell
dictate overlay
```

Change something in the `[overlay]` block of your config, run that again, and you
see the result in about two seconds instead of a record-speak-release round trip.
`dictate overlay --error` shows the failure state, `--repeat 3` loops it, and
`--rate 150` runs the words in faster than real dictation does.

Three things about it are worth knowing:

* **It appears on the screen you are working on.** Not always the primary one —
  it uses the monitor holding the window you were in when you pressed the
  hotkey, which is the window your text is about to be pasted into. If that
  cannot be worked out it falls back to the monitor your mouse is on, then the
  primary. `follow_focus = false` restores the old always-primary behaviour.
* **It is sized for the monitor it lands on.** Every pixel measurement in
  `[overlay]` is at 100% scaling and is multiplied by that display's own
  scaling, so a second screen at 150% gets captions the same physical size
  rather than two thirds the size. If that misbehaves, `dpi_awareness = "off"`
  hands the scaling back to Windows.
* **It is set in Fira Code**, which is already installed on this PC. If it ever
  is not, dictate uses Consolas and **says so when it starts** — it will not
  quietly draw a different font and leave you wondering.

It never takes focus and clicks pass straight through it, which is what stops
your text being pasted into the wrong window. That has not changed and is not
allowed to.

### Your graphics card is only busy while you are dictating

The transcription model takes about **1.6 GB** of your card's memory. It used to
sit there for as long as dictate was running — all day, whether or not you had
said anything since breakfast — which is memory a game or a compute job could
have had.

Now dictate hands it back after **five minutes without dictating**. Between
dictation sessions the card is genuinely idle as far as dictate is concerned:
the full 16 GB is available to everything else, and there is nothing to close or
remember to close.

You get it back by dictating, and you are unlikely to notice it happening.
Loading the model starts the moment you **press** the hotkey — not when you let
go — so it happens while you are still speaking, which is about how long it
takes. The words on screen while you talk come from a different, much smaller
model that runs on the processor, so they appear exactly as usual either way.
Worst case, on a short sentence after a long gap, the paste is a second or two
later than normal, once.

To turn it off and keep the model loaded permanently, set this in your config:

```toml
[whisper]
idle_release_minutes = 0
```

`dictate doctor` tells you what it is set to and whether a model is loaded at
this moment.

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

### Verified anywhere — 474 tests, run and passing

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
  reason, clean shutdown, and the stop-during-restart deadlock. Including that
  after an idle release the server really can be started again on the same port,
  and that the restart-on-crash supervision applies to that second process too.
* **When the graphics card's memory is handed back and taken again**: the idle
  timer, that a running transcription is never unloaded from under itself, that
  the load starts at hotkey press rather than at release, and every awkward
  collision — pressing while the shutdown is running, pressing again while a
  load is already running, an utterance that ends before the model has loaded,
  two overlapping utterances, and shutdown arriving in the middle of any of it.
  Also that a model which cannot be loaded again says so and never silently
  pastes nothing.
* The whisper-server HTTP client against a **real HTTP server**: multipart WAV
  upload, response parsing, HTTP errors, nothing-listening.
* The cleanup pass, including the "can only delete" guarantee — and that every
  shipped rule preserves it on realistic dictation.
* Config and rules validation, WAV encoding, the utterance buffer, UTF-16
  surrogate handling for text injection, hotkey parsing, and the doctor's
  reporting.
* **Everything about the caption overlay that is not the window itself**: the
  fade curve and its mid-flight reversal, the font fallback decision, and the
  per-monitor layout arithmetic — including that a second monitor at 150%
  scaling gets a *bigger* caption rather than a smaller one, that the panel
  lands inside that monitor's work area and not the primary one's, and that it
  still fits on a 1366×768 laptop. Plus the look's own rules, checked against
  the source: no geometry call in the caption path, no call anywhere in the
  file that could activate the window, and comfortable rather than maximum
  text contrast.
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
* **That `dictate stop` gets out of every stuck state**, without being told
  which one it is looking at: a copy that is running, a copy that will not
  answer (ended with its whisper-server, never on its own), a whisper-server
  left holding the transcription port with no parent, one that will not die, a
  process id that has been reused by something else since, and a port held by a
  program that is not ours — which it names and refuses to touch. Also that
  every one of those answers names the exact command to type.
* **That every child process is handed to the guard that contains it** — the
  first one, the one that replaces a crash, and the one an idle release brings
  back. The containment itself is Windows' job and is proved on Windows; that
  nothing slips past it is proved here.
* **What the tray icon says and offers**: the tooltip, the status line, the four
  menu items, that each names the command that does the same thing, and that the
  icon's own bytes are an icon Windows can read whose colour is the status.
* **Everything `dictate update` decides**, driven with real archives built and
  unpacked in the test: which folder is the install, what changed between two
  revisions in plain language, which files to write and which to remove, which
  files *he* has edited (and the refusal that names them), an archive containing
  a path that escapes its folder, a new version that will not import being rolled
  back by itself, an update interrupted half way being put back from its journal,
  and that a copy in the middle of an utterance is left alone rather than stopped.
  Also that nothing token-shaped can reach the screen.
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
* **The 474 tests above, on Windows** as well as on Linux — which is where the
  single-instance lock is exercised against Windows' own byte-range locking
  rather than Linux's `flock`.
* **That a supervised child process cannot outlive its parent.** CI starts a
  parent that starts a child through dictate's own `ManagedProcess`, then ends
  the parent with `Stop-Process -Force` — a `TerminateProcess`, so *none* of
  dictate's shutdown code gets to run — and requires the child to be gone and
  its port free within seconds. The same test with the containment removed shows
  the child surviving, which is what makes the first result mean anything. This
  is the mechanism behind the orphaned `whisper-server`; the exact keystroke
  path (Ctrl+C, then `Y` at "Terminate batch job") is on the list below.
* **That `dictate stop` really ends a copy that will not answer.** On Windows
  the suite starts a second process that takes the lock and then ignores every
  request to stop — the state that used to mean Task Manager — and `dictate
  stop` ends it for real, through the real `taskkill /T /F`, and the lock comes
  free. On Linux, where dictate has no way to end it, the same test requires it
  to say so and exit non-zero rather than claim it did something.
* **That a blank line from a program is not turned into a .NET type name.**
  PowerShell wraps every stderr line in an error record, and a *blank* one used
  to come out as the text `System.Management.Automation.RemoteException` in the
  middle of the install report. Both halves of that are tested on the real
  Windows PowerShell 5.1 that ships with Windows.
* **That `dictate update` really fetches this private repository.** CI signs the
  GitHub CLI in with the workflow's own token, makes a throwaway editable install
  that is deliberately behind, and runs the real command: the source archive for
  a real revision comes back, a file that is behind is brought forward, a file no
  longer in the source is removed, the revision is recorded, and the commit
  subjects between the two revisions are reported. Then `--force` past an edited
  file, `--restore` back again, and — with the token cleared — that it names
  `gh auth login` instead of failing obscurely. What it cannot prove is the
  sign-in itself; see below.

### Still not verified — needs this actual PC

Nothing below has been run anywhere. It is where to look first if something
misbehaves.

* **Ctrl+C, then `Y` at "Terminate batch job (Y/N)?"** — the keystrokes that
  produced the orphaned `whisper-server` in the first place. What is proved on
  CI is the mechanism underneath that case, a parent dying with no chance to
  clean up; nobody has typed those two keys at a real `dictate run` with a real
  whisper-server holding a real 1.6 GB of VRAM.
* **The icon by the clock.** Whether it appears, what it looks like at the size
  Windows draws it, whether the menu opens and closes properly, and whether
  Stop and Restart do what they say. A hosted runner has no notification area
  and no one to click anything in it. The `[tray] enabled = false` line in your
  config turns it off if it misbehaves; nothing else depends on it.
* **`gh auth login` in a browser, and Windows remembering it.** CI borrows the
  workflow's own token, so the one interactive step of `dictate update` — the
  sign-in you do once — has never been run by anybody. Everything after it is
  proved; that first minute is not.
* **Anything GPU.** CI machines have no graphics card, so every transcription
  above ran on a processor. No timing figure here is measured. The 0.4–1.0 s
  estimate comes from a published RX 6750 GRE (RDNA2, Windows, Vulkan) encode of
  428 ms on a card with half the compute units. **Step 6 of setup answers this
  in about a minute**, and so does `dictate transcribe some.wav` at any time.
  A green tick on CI says nothing whatsoever about the GPU.
* **That the 1.6 GB really comes back.** dictate shuts `whisper-server` down and
  the process genuinely exits — that much is tested against a real child process
  here. Whether the driver then returns the VRAM to the pool, and how long the
  reload actually takes on your card, has been observed by nobody. Task
  Manager's Dedicated GPU memory figure, five minutes after you stop dictating,
  is the check.
* **The toolchain install** — winget fetching Python, CMake, the Vulkan SDK and
  the C++ build tools. CI machines already have most of those and do not use
  winget at all, so step 2 of setup is the one part of it no machine here has
  ever executed. Every tool it installs is now checked by looking for the tool
  itself afterwards, never by trusting the installer's exit code and never by
  reading an environment variable as a proxy — but the winget calls themselves,
  and the direct download from LunarG that steps in when winget cannot install
  the Vulkan SDK (including the Authenticode check on what arrives), have run
  nowhere.
* **The caption overlay, as a window.** The non-activating, click-through,
  always-on-top window is built from the documented Win32 extended styles, but
  no one has watched it fail to steal focus. This is the highest-risk unverified
  piece, because if it does steal focus the paste lands in the wrong window.
* **What the overlay looks like.** Nobody here has run it. The colours, the type
  and the layout arithmetic were checked by rendering the panel at exactly the
  sizes the code uses, with the real Fira Code file, against a white document, a
  dark editor and a photo wallpaper — but that is a picture of the design, drawn
  by a different renderer, not a screenshot of the Tk window. `dictate overlay`
  is how it gets looked at for real, and it takes about ten seconds.
* **How Fira Code's ligatures render here.** Fira Code puts all of them in the
  OpenType `calt` feature — it has no `liga` table at all (checked against the
  6.2 release). Tk draws text through GDI on Windows, which is not expected to
  apply `calt`, so the ligatures most likely do not appear at all, which for
  prose captions is the outcome we want. That expectation has not been tested on
  a real screen. If something does look odd in a sentence, the fix is one line:
  set `font_family` to `"Fira Mono"` or anything else.
* **Per-monitor DPI and multi-monitor placement.** The rule and the arithmetic
  are tested here; the Win32 calls behind them (`SetProcessDpiAwarenessContext`,
  `MonitorFromWindow`, `GetDpiForMonitor`) have run nowhere. A mixed-DPI desktop
  is the case to watch. `dpi_awareness = "off"` and `follow_focus = false` are
  both one-line retreats if either misbehaves.
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
