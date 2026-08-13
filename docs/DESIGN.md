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
                                │   model RESIDENT in VRAM between utterances
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

### 1. The Whisper model stays resident in VRAM

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
* **The pipeline, cleanup, config, process supervision and HTTP client are plain
  Python** and are tested for real, here, on Linux — 164 tests.
* **The fiddly bits of the platform code were factored out into pure functions**
  so they could be tested anyway: `platform/geometry.py` (overlay placement),
  `platform/injection_plan.py` (UTF-16 surrogate pairs, Return vs Unicode),
  `platform/hotkey_spec.py` (hotkey parsing).

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

Caption audio is queued with a bounded queue that drops the oldest block when
full. Captions are disposable, so dropping them is strictly better than blocking
the audio callback; the utterance buffer feeding the GPU pass is never dropped.

---

## Things deliberately not done

* **No LLM cleanup pass.** Available as a future option (the same Vulkan
  toolchain builds llama.cpp), but hallucination risk in a dictation tool is the
  wrong trade. Revisit only if rule-based output disappoints on real speech.
* **No re-punctuation or re-casing.** Whisper does both, better.
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
built from the Win32 primitives directly. Credited in the source files that
follow it and in the README.
