# dictate_v1 — the design, and why each piece is what it is

Everything here was settled before the code was written, in
`dictate-decisions-2026-08-13.md` and the two research reports behind it. This
file records the decisions and, more usefully, the constraints that fall out of
them — the things that will break the product if a later change ignores them.

---

## The shape

```
   hold hotkey ──► mic ──► utterance buffer (the whole take, kept in memory)
                     │
                     ├──► streaming Zipformer, CPU, 2 threads, ~320 ms updates
                     │       └──► caption overlay — DISPLAY ONLY,
                     │            discarded on release, never pasted
                     │
   release hotkey ───┴──► Whisper large-v3-turbo (f16), GPU, whisper.cpp Vulkan
                                │   model RESIDENT in VRAM between utterances,
                                │   released after 5 idle minutes and reloaded
                                │   from the next hotkey PRESS (constraint 1)
                                ▼
                          rule-based cleanup  (<10 ms)
                                ▼
                          paste into the window captured at hotkey PRESS
```

Target machine: AMD RX 6900 XT (gfx1030), Ryzen 7 5800X3D, 32 GB, Windows.
Post-release budget: **2–4 s**. Estimated GPU pass: **0.4–1.0 s**.

---

## The four settled decisions

| # | Decision | Why |
|---|---|---|
| 1 | **GPU route: whisper.cpp + Vulkan** | Vulkan ships with the ordinary Adrenalin driver. The ROCm alternative is not merely riskier — it is blocked on this exact card: AMD ships rocBLAS kernels for gfx1100+ only, and **zero** files for gfx1030, in all of ROCm 7.1.1 / 7.2 / 7.2.1. Whisper is almost entirely matrix multiplies, so that path has nothing to run them with. `scripts/check-rocm-gfx1030.py` re-checks this in 30 seconds. |
| 2 | **Build whisper.cpp from source** | No official Windows Vulkan binary exists (whisper.cpp #3673, #3691, both open). The alternative was a stranger's zip. Building it also yields `parakeet-cli.exe`, and — the part that matters operationally — lets you roll back when a Vulkan regression ships, which has happened before on this exact architecture (llama.cpp #22992). `scripts/build-whisper-vulkan.ps1` prints the commit it built and tells you to write it down. |
| 3 | **Keep Whisper `large-v3-turbo`** | Parakeet was only ever on the table because turbo missed the budget *on CPU*. Moving the batch pass to the GPU removes that reason, and keeping Whisper honours what was originally asked for. |
| 4 | **Streaming Zipformer for captions, display-only** | Whisper structurally cannot do live captions — it has no partial-audio mode, and every "streaming Whisper" fakes it by re-running on overlapping chunks and rewriting words already shown. The accepted cost is that the caption text visibly differs from the pasted text (ALL CAPS, unpunctuated, occasionally wrong). That cost is contained *by the architecture*: the caption never reaches the document. |

**These are closed.** Decision 4 in particular is load-bearing: it is the only
reason a sloppy caption model is acceptable at all.

---

## The five constraints that break the product if ignored

### 1. The Whisper model stays resident in VRAM *while he is dictating*

Model load is ~2 s and ~1.6 GB. Spawning `whisper-cli.exe` per utterance pays
that every single time and blows the 2–4 s budget before any audio is looked at.

**How:** `whisper-server` — the resident form whisper.cpp actually ships, built
by the same Vulkan build. `WhisperVulkanBackend` starts it once at app start and
owns it end to end (`engines/whisper_backend.py`, `engines/process.py`):
preflight → spawn → poll `GET /health` → warm with a second of silence → restart
on crash with a budget → clean shutdown.

`tests/test_engines.py` exercises that whole lifecycle against a real child
process (`tests/stub_server.py`), including crash-and-restart, give-up-after-N,
and the deadlock where `stop()` races a restart in flight.

> Where to look if latency is bad: the log line `transcribed Xs of audio in Ys`,
> and whether whisper.cpp printed a Vulkan line at startup. `_log_backend_choice`
> warns explicitly if it did not.

**Amended once it was in daily use.** The same card runs games and GPU compute,
and holding 1.6 GB for hours in which nothing was said is not a cost residency
was meant to buy. So residency is now bounded by *use* rather than by process
lifetime: after `[whisper] idle_release_minutes` (default 5) without dictation
the server is shut down cleanly and the memory really goes back;
`engines/residency.py` (`ResidentModel`) owns that.

What makes it nearly free is **the warm-up starts at hotkey PRESS, not at
release**. He holds the key and speaks for several seconds before letting go —
which is the same order of magnitude as the load — so `app.Application.
_on_hotkey_press` asks for the load first and starts recording second. Live
captions come from the CPU Zipformer and are untouched, so words keep appearing
while the GPU model loads behind them; that is decision 4 paying for itself a
second time.

The constraint above is intact where it matters: within a dictation session the
model is resident and no utterance reloads it. `idle_release_minutes = 0` is the
way back to the old behaviour, and `config.validate()` refuses values small
enough to unload the model between one sentence and the next.

`tests/test_residency.py` covers the state machine off Windows — the timer, the
press-triggered warm-up, and the collisions (press during shutdown, press during
warm-up, an utterance that ends before the load finishes, overlapping
utterances, shutdown mid-transition). What is **not** verified anywhere is that
the VRAM is genuinely returned; that needs the card.

### 1b. whisper-server may not outlive dictate — by any death, not just a tidy one

The other half of owning that process. It holds the transcription port and, while
the model is resident, ~1.6 GB of VRAM, so a copy of it that survives its parent
makes every later `dictate run` **and** every `setup.ps1 -Only verify` fail on a
port that nothing appears to be using. That happened twice in one evening: Ctrl+C
in the window running `dictate run`, `Y` at Windows' "Terminate batch job (Y/N)?"
prompt, and the parent was gone while the child was not.

**A shutdown handler cannot fix this.** dictate already stops the child cleanly
on Ctrl+C, on SIGTERM, on `dictate stop` and on a failed start — and the case
that bit him is exactly the one where the parent runs none of it. Only the
operating system can end a process on behalf of one that is no longer executing.

**How:** a Windows **job object** with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`
(`platform/windows/job.py`). Every child `ManagedProcess` spawns is assigned to
it, and Windows empties the job when the last handle to it closes — which
happens when this process ceases to exist, whatever ended it. Nothing about that
depends on dictate behaving well on the way out, so the clean shutdown stays as
the *ordinary* path (a job kill is a `TerminateProcess`, which gives whisper.cpp
no chance to free the card) and this is the floor underneath it.

CI proves it on real Windows by killing a parent with `Stop-Process -Force` and
requiring the child to be gone — and proves the test means something by running
the same thing with the containment removed, where the child survives.

**And the way back for an orphan from an older build:** `dictate stop`, which is
the one command for every stuck state (`recovery.py`) — a copy that is running, a
copy that will not answer (ended with its child, never on its own), and a
whisper-server holding the port with no parent. `setup.ps1` calls the same thing
with `--stale-only` before it tests the install, and treats a copy that is
genuinely running as an ordinary situation rather than a broken installation.

### 2. The caption overlay must never take focus

If it does, the focused window changes and the paste lands in the wrong place —
which breaks the entire product.

**How** (`platform/windows/overlay.py`):

* `WS_EX_NOACTIVATE` — Windows never activates the window, not even on a click.
* `ShowWindow(SW_SHOWNOACTIVATE)` and `SetWindowPos(..., SWP_NOACTIVATE)`.
  Tk's own `deiconify()` is **deliberately not used** — it goes through `SW_SHOW`.
* `WS_EX_TRANSPARENT` — click-through, so clicks reach what is underneath.
* `WS_EX_TOOLWINDOW` — off the taskbar and out of Alt-Tab.
* Nothing in that file calls `SetForegroundWindow`, `SetFocus`, `focus_force`,
  `grab_set` or `lift()`.

And separately: **the target window handle is captured at hotkey press**
(`Pipeline._capture_target`), not at paste time. Between press and paste the
overlay appears, a notification may pop, and the user may alt-tab.
`test_target_window_is_captured_at_press_not_at_paste` holds this in place.

That same handle is passed to `overlay.set_state()`, because it answers a second
question as well: **which monitor the captions belong on.** The window the text
is about to be pasted into is the window the user is looking at, so the captions
go on its display rather than always on the primary one — falling back to the
mouse pointer's monitor, then the primary. Nothing in that path can activate
anything: `MonitorFromWindow`, `GetMonitorInfoW`, `GetCursorPos` and
`GetDpiForMonitor` are read-only queries.

### 2b. The overlay is laid out once per appearance, not once per caption

Size and position are decided in `_show()` and then left alone. The caption path
(`_apply`) sets text and colour and touches nothing else — no `geometry()`, no
`SetWindowPos`, no `update_idletasks`. Two things follow from that, and both
matter:

* A window that re-measures itself every 320 ms is *restless* in peripheral
  vision, which is most of what "the box the text comes up in is ugly" was. The
  slab reserves two caption lines whether or not there are two to show, so it
  never grows mid-sentence.
* It is also strictly less work than re-measuring, so the nicer version is the
  cheaper one. `tests/test_overlay.py::TheLooksOwnRules` greps `_apply` for
  those calls, because this is the kind of rule an innocuous edit undoes.

Everything in `[overlay]` is a measurement **at 100% display scaling**, scaled by
the chosen monitor's DPI in `geometry.plan_slab`. The process declares itself
per-monitor DPI aware before any window exists (`windows/monitors.py`) — without
that Windows virtualises this process's coordinates, and the monitor rectangles
stop being in the same space as the rectangle Tk is asked to occupy, which puts
the overlay on the wrong screen on a mixed-DPI desktop.

Fading is a cubic ease on the window's own opacity (`platform/fade.py`), out
slower than in, resuming from the current alpha if it is interrupted. The fade
out runs after the text has already been pasted, so its length costs nothing.
`fade = false` restores the old snap.

### 3. The clipboard is preserved — by not touching it

Default paste method is `sendinput`: the text is synthesised as Unicode
keystrokes and **the clipboard is never involved**, so there is nothing to save
or restore and no window in which a clipboard manager can observe or clobber it.
Save-paste-restore is a race by construction — you cannot know when the target
has finished reading the clipboard, so the restore is always a guess.

`method = "clipboard"` exists because a few remote-desktop and terminal apps drop
long synthetic keystroke runs. In that mode the previous contents **are** saved
and restored, and if the clipboard holds something that cannot be faithfully put
back (an image, a file list) it falls back to keystrokes for that paste rather
than destroying it.

### 4. Caption text is discarded on release

`Pipeline.finish_utterance` clears the overlay text, drops the session reference
and bumps a generation counter that invalidates every queued and in-flight
caption block, then hands the session to the caption thread to close (which also
empties its text). Four tests in `CaptionsAreDiscarded` cover this, including
"audio queued before release never updates the screen after it".

### 5. Live captions run on 2 CPU threads

Measured: 6 threads was **3× worse** than 2 — the per-chunk work is tiny and
thread synchronisation dominates. `config.validate()` refuses values above 4 and
says why. This is not a knob to "optimise".

---

## Why the code is shaped this way: nobody who built it had the hardware

No Windows machine and no AMD GPU were available. That is the single biggest
influence on the structure, and it is deliberate rather than apologetic:

* **Four platform seams** (`platform/base.py`): audio capture, global hotkey,
  caption overlay, text delivery. One implementation each, Windows-only.
* **No fallbacks.** `platform/factory.py` raises `PlatformUnsupportedError` on
  any other platform. There is no no-op overlay and no pretend paste — a
  component that cannot work says so and stops. `tests/test_cli.py` asserts that
  `src/` contains no test doubles at all.
* **The pipeline, cleanup, config, process supervision, model residency and HTTP
  client are plain Python** and are tested for real, here — 343 tests, on Linux
  and on Windows.
* **The fiddly bits of the platform code were factored out into pure functions**
  so they could be tested anyway: `platform/geometry.py` (overlay placement and
  the slab's per-monitor layout), `platform/fade.py` (the opacity ramp),
  `platform/fonts.py` (which font actually resolved, and the fallback when one
  did not), `platform/injection_plan.py` (UTF-16 surrogate pairs, Return vs
  Unicode), `platform/hotkey_spec.py` (hotkey parsing).
* **`dictate overlay`** exists because the one thing that genuinely cannot be
  tested from here is what the overlay *looks* like. It runs the real overlay
  through the real interface with sample text, so a look-and-react loop is
  seconds rather than a record-speak-release round trip.

What that leaves genuinely unverified is listed in the README under
"What has not been verified".

---

## The cleanup pass, and the guarantee

Rule-based, not an LLM. An LLM rewriting dictation will occasionally invent
words, and in a chat assistant that is an annoyance while in a dictation tool it
is much worse — the invented text goes straight into the document and reads
fluently enough to miss. A stray "um" is a trivial problem; a confidently
inserted clause you never said is not.

So the pass **can only delete**, and that is enforced twice:

1. The rule schema has **no replacement field** (`cleanup/rules.py`). A user who
   adds `replace = "..."` gets told why it does not exist.
2. `clean()` verifies the result: the word sequence of the output must be a
   **subsequence** of the word sequence of the input. If any rule — including one
   the user wrote — breaks that, the entire cleanup is discarded and Whisper's
   text is pasted exactly as it came out.

Rule 2 is what makes rule 1 trustworthy given a user-editable regex file. It
already earned its keep during the build: a plausible-looking "strip the dangling
restart dash" rule turned `to - I` into `toI`, and the check caught it. That rule
is now in `cleanup-rules.toml` commented out, with the story, as a warning.

Whisper already emits punctuation and casing, so the pass does not add either.
The one exception is repairing its own damage: if deleting a leading "Um," left a
sentence starting lowercase, the capital is put back — and only when the word was
*not* sentence-initial before the deletion, so Whisper's own lowercase choices
are left alone.

---

## Threading

| Thread | Owns | Rule |
|---|---|---|
| main | the Tk overlay message loop | every Tk call happens here; `set_state()` queues from elsewhere |
| hotkey | the keyboard hook | calls `start_utterance` / `finish_utterance`; every callback is wrapped so an exception cannot wedge the hook and with it the whole keyboard |
| audio | PortAudio | calls `push_audio` only, which appends and enqueues — it never runs a model |
| caption | — | the **only** thread that ever touches a streaming session |
| finalize | one worker | so two utterances finishing close together paste in the order they were spoken |
| idle watch | the residency clock | ticks `ResidentModel.check_idle()`; a short-lived worker of its own does the unload and the reload, so neither the hotkey thread nor the timer ever blocks on one |
| tray | the notification-area icon and its window | a window's messages go to the thread that created it, so the icon is created, updated and destroyed there; `update()` from any other thread stores the state and posts a message |

Caption audio is queued with a bounded queue that drops the oldest block when
full. Captions are disposable, so dropping them is strictly better than blocking
the audio callback; the utterance buffer feeding the GPU pass is never dropped.

---

## Things deliberately not done

* **No LLM cleanup pass.** Available as a future option (the same Vulkan
  toolchain builds llama.cpp), but hallucination risk in a dictation tool is the
  wrong trade. Revisit only if rule-based output disappoints on real speech.
* **No re-punctuation or re-casing.** Whisper does both, better.
* **No Windows service**, even though "run it as a service" is the obvious
  answer to "start it automatically". Services run in session 0, which has no
  interactive desktop: the caption overlay cannot appear on screen from there
  and synthesised keystrokes cannot reach a focused window. Both are constraints
  2 and 3 above, so a service would install, start, and do nothing. Starting at
  logon is a **per-user Task Scheduler logon task** running in his own session -
  see `src/dictate/autostart.py`, which carries the rest of the reasoning.
* **No installer, no Windows CI.** Separate follow-up task.
* **No ROCm path.** Blocked on this card; kept only as the diagnostic in
  `scripts/check-rocm-gfx1030.py`.

---

## Prior art

[`PinW/whisper-key-local`](https://github.com/PinW/whisper-key-local) (MIT), read
as prior art for the Windows mechanics. What was taken from it: the
`platform/<os>/` package split, the confirmation that layout-independent
`SendInput` + `KEYEVENTF_UNICODE` is the right way to deliver text, and the
`global_hotkeys` package as the way to get separate press/release callbacks
(Windows' own `RegisterHotKey` reports key-down only, so it cannot express
push-to-talk). What was **not** taken: any code, and the overlay — it has no
caption overlay, using a system tray and a terminal UI instead, so this window is
built from the Win32 primitives directly. (dictate now has a tray icon too, for
a different reason: it starts at logon with no window at all, and a command you
cannot see is a command you will not recall. It carries status, stop and restart
and nothing else — `src/dictate/tray.py`.) Credited in the source files that
follow it and in the README.
