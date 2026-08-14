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
                     ├──► streaming FastConformer, CPU, 2 threads, ~160 ms updates
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
| 4 | **A streaming transducer for captions, display-only** | Whisper structurally cannot do live captions — it has no partial-audio mode, and every "streaming Whisper" fakes it by re-running on overlapping chunks and rewriting words already shown. The accepted cost is that the caption text visibly differs from the pasted text (lower case, unpunctuated, occasionally wrong). *Which* streaming model is a config change and has been made once — see "The caption model" below; the decision is about the architecture, not the file. That cost is contained *by the architecture*: the caption never reaches the document. Constraint 4 below is where that is enforced — and where the timing of what is on screen, which is not the same question, is set out. |

**These are closed.** Decision 4 in particular is load-bearing: it is the only
reason a sloppy caption model is acceptable at all.

---

## The six constraints that break the product if ignored

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
captions come from the CPU caption model and are untouched, so words keep appearing
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

### 2c. How big it is, is one word — and the pixel values are the design

The product owner saw the panel on his own screen and said the text was too
large by "like 40%", and that the panel therefore took up a lot of the screen.
The fix is *not* to change five numbers in the config, because those five
numbers only look right in relation to each other: type, padding, shoulder,
width and screen margin are one proportion, and hand-tuning any of them alone
produces a small line of text floating in a large slab. He also will not
hand-tune anything, and said so.

So the pixel values in `[overlay]` were left exactly as they are — they are the
drawing, at `size = "huge"` — and a named ladder multiplies all of them at once:
`small`, `compact`, `medium`, `large`, `huge`, in eighths, which lands the
caption text on 12, 15, 18, 21 and 24 pixels at 100% scaling. **It ships at
`compact`.** Two consequences are load-bearing:

* His existing `dictate.toml`, written by `dictate init` before any of this,
  sets `font_size = 18` and the rest explicitly. A change of *defaults* would
  therefore have done nothing for him at all. A multiplier is what makes a file
  with no new key get the smaller panel.
* Because the whole design scales together, the text column works out at about
  60 characters a line at *every* rung, so `max_chars` stays right and no size
  is the one where the padding looks wrong.

`text_size` and `panel_size` split that one knob when he wants the words bigger
than the box, or the box smaller than the words; both are empty by default,
meaning "follow `size`". Where they are split, two floors in `plan_slab` decide
it, and both are the words winning: padding may not fall below
`MIN_PADDING_SHARE` of the type it surrounds, and the panel may not be narrowed
until `max_chars` no longer fit on the lines it reserves — which would silently
clip the *newest* words off the bottom while he is still speaking. Neither floor
moves anything while the two agree.

**The size knobs are a second multiplication on top of the DPI scaling, never a
replacement for it.** `size` is what he chose; the monitor's scale is what the
display demands. A `compact` panel on a 150% screen is 150% of a compact panel.

None of it is reachable only by editing a file. `dictate overlay --size small`
(or `--text`, `--panel`, `--font`) shows the result and *then* asks whether to
keep it; `dictate look` says what is in force and changes it in one word; and
the tray has a "Caption size" submenu — the five names, with a tick on the one
in force — which takes effect on the next appearance, rule 2b again, since
nothing on screen may resize while it is being read. It is deliberately the same
shape as "Change the hotkey" beside it, down to writing his config through the
same `config_edit`: one line of one section, with every comment, line ending and
byte order mark left where it was. `src/dictate/overlay_size.py` carries the
ladder and owns which `[overlay]` keys may be written that way. His config file
is his.

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

### 4. Caption text is display-only — it can never reach the document

`Pipeline.finish_utterance` drops the session reference and bumps a generation
counter that invalidates every queued and in-flight caption block, then hands the
session to the caption thread to close (which also empties its text). So from the
release onwards, no caption text can be *produced*. What makes it unpasteable is
separate and structural:

* **The pipeline never holds it.** `pump_captions` reads a session's text and
  hands it straight to the overlay without keeping a reference. The only copy
  anywhere is the overlay's own Tk label.
* **The value that crosses into the paste path carries audio.** `Utterance` has
  PCM, a sample rate, a window handle and four numbers — no field a string could
  travel in.
* **`injector.send` is called from exactly one place**, in `_finalize`, with
  what came out of `batch.transcribe`.

`tests/test_pipeline.py::CaptionsCanNeverBePasted` holds each of those, plus the
awkward case: the caption still on screen at the moment the real text is
delivered, and still not what is delivered.

**Amended once it was in daily use.** Clearing the screen at release was one
*implementation* of this constraint, and it left him watching an empty panel for
the 0.4–1.0 s the GPU takes — the moment he is most likely to wonder whether
anything is happening. So the words now stay up from press until the text lands:

| Stage | State word | The big line |
|---|---|---|
| hotkey held | `listening`, accent | the caption, arriving |
| released, GPU running | `thinking`, muted | the same caption, drawn in `muted` |
| text delivered | `pasted`, muted | empty — the words go as the text lands |
| failed | `error` | the message |

Three things make the held caption safe to look at. It goes grey the moment he
lets go, so the panel visibly registered the release and the words visibly stop
being live. It is still lower case and unpunctuated, so it cannot be read as the
finished text. And what removes it is the paste itself, so "the words went" means
"it landed" rather than "a timer expired".

That middle one is why the caption model was replaced with a lower-case one
rather than with one of the 2026 models that punctuate and use sentence case: a
held caption that looks like Whisper's output is a held caption that can be read
as Whisper's output. The property that is load-bearing is "visibly not the
finished text", not "upper case".

The release passes `platform.base.KEEP` rather than any text, which is the point:
the pipeline could not re-send the caption if it wanted to, because it does not
have it. The overlay only ever keeps text that is on screen *now* — nothing
survives the panel being hidden, so a previous utterance's caption cannot
reappear under a new one.

### 5. Live captions run on 2 CPU threads

Measured: 6 threads was **3× worse** than 2 — the per-chunk work is tiny and
thread synchronisation dominates. `config.validate()` refuses values above 4 and
says why. This is not a knob to "optimise", and it is not the fix for late
captions — `dictate captions <clip.wav>` is how to find out what is.

---

### 6. Dictated text is typed. It does not press keys

He dictates into a terminal, and reported dictate "pasting things in and then
pressing enter" — an Enter he did not ask for runs whatever is on the command
line, and he did not notice at the time. Everything else this product has got
wrong has cost him time; this one can run commands.

**Where the Enter came from.** whisper.cpp's server writes one line per segment.
`output_str` in `examples/server/server.cpp` is
`result << speaker << text << "\n"` for *every* segment, and that whole string
is the `text` field of the JSON reply — so a two-sentence utterance came back as
`"Sentence one.\nSentence two.\n"`. Nothing downstream had any reason to remove
it: the cleanup pass collapses runs of `[ \t]` and deliberately leaves line
breaks alone, and `injection_plan.plan_text` turns a newline into VK_RETURN
because a keypress is the only thing that puts a line into another application.
`WhisperServerClient._parse` now joins those lines with a space, which is what
the same module already did on the `verbose_json` branch where the segments
arrive separately.

**And the wall behind it.** `[paste] line_breaks` decides what a line break
does, and `"space"` is the default. `plan_text` will not emit a Return or a Tab
unless the caller passes `allow_return=True`, so the guarantee is a property of
the function rather than a check every caller is trusted to have done — a rule
he writes, a spoken "new line", a model that starts writing line breaks of its
own, all end up as a space. `line_breaks.apply` runs once in `injector.send`,
above both paste methods, because a newline in the clipboard submits exactly as
well as a synthesised one.

`modifier_guard` is the same constraint from the other side: the hotkey listener
ends the utterance when *any* key of the chord comes up, so Ctrl and Alt can
still be down when the paste happens ~0.5 s later — and then `the` is Ctrl+T,
Ctrl+H, Ctrl+E. It waits `[paste] modifier_wait_ms` for them and forces the
key-up if they are still held.

**The history says when it happened.** `injector.send` returns how many Returns
it pressed and the entry records it. An Enter he did not notice at the time is
then something he can look up an hour later, which is the only form of evidence
that is any use for a thing you do not see happen.

---

## The caption model

Decision 4 fixes the *architecture*: a small streaming transducer, display-only,
never Whisper. Which model file runs is four settings in `[captions]`, and it has
been changed once.

**Until 2026-08-13** it was `sherpa-onnx-streaming-zipformer-en-2023-06-26`,
trained on LibriSpeech alone — read audiobooks, transcribed in upper case with no
punctuation. Dictation is spontaneous speech into a desk microphone, which is not
that, and the reported symptom was that the captions were "crazy wrong" while the
pasted text was correct.

**MEASURED** (Linux, i5-12500T, 2 threads, `dictate captions` on the clips below;
absolute times are from a loaded box, the comparison is what is being claimed):

| clip | the LibriSpeech Zipformer | NeMo FastConformer 80 ms |
|---|---|---|
| `assets/jfk.wav`, 11.0 s | first words 1.12 s, every 0.32 s — `AND SAW MY FELLOW AMERICANS ASK NOT WHAT'S YOUR COUNTRY CAN DO FOR YOU AS BUT YOU CAN DO FOR YOUR COUNT` | first words 0.93 s, every 0.32 s — `and so my fellow americans ask not what your country can do for you ask what you can do for your country` |
| LibriSpeech clip, 6.6 s | first words 1.12 s, every 0.32 s — ends `…OF THE BROTHEL` | first words 0.93 s, every 0.16 s — ends `…of the brothels` |
| LibriSpeech clip, 16.7 s | first words 1.12 s, every 0.32 s — ends `…A BLESSED SOUL IN HE` | first words 0.93 s, every 0.16 s — ends `…a blessed soul in heaven` |
| `jfk.wav` + noise at 20 / 10 / 5 dB SNR | three more errors appear, and `ASK` is lost entirely | identical and complete at all three |
| cost, warm, interleaved ×5 | RTF **0.051** | RTF **0.175** — 3.4×, still 5.7× faster than speech |
| cost on a GitHub Windows runner (`dictate captions`, in CI) | not measured there | RTF **0.582** |

Two things follow, and they are the two halves of what he reported. The words are
right because the replacement's training set includes Fisher and Switchboard —
conversational telephone speech — rather than read audiobooks only. And it is
never a word behind at the moment he lets go, which the old model always was:
that trailing word is what he was left looking at while the GPU worked, now that
the caption stays on screen until the text lands.

That last row is the honest ceiling and the reason the drop counter above exists:
a shared, throttled CI VM leaves less than half the budget spare. His 5800X3D is
nothing like that machine, but nobody here can prove it — so if the caption
thread ever does fall behind on his, the message at the release says so, and the
old model is still one config line away at a fifth of the cost.

It is **NVIDIA NeMo `stt_en_fastconformer_hybrid_large_streaming_80ms`**, int8,
exported to ONNX by the sherpa-onnx project, CC-BY-4.0, English only: a 98 MB
download against the old model's 310 MB. `scripts/models.psd1` pins and sources
it.

**What was measured and NOT taken.** The 2026 X-ASR streaming models punctuate
and use sentence case and cost about the same as the old model; they got the same
clips right. They were rejected on constraint 4: a held caption that looks like
Whisper's output can be read as Whisper's output, and they are also zh-en
bilingual, which is a script this product has no use for appearing on screen.

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

## Spoken punctuation, and why it is a second stage

"hello comma world" → `hello, world`. That is a **substitution**, and the pass
above is guaranteed never to make one. The guarantee is not negotiable, so
spoken punctuation is a stage of its own (`src/dictate/punctuation/`) that runs
**after** cleanup and leaves it exactly as it was — same schema, same
subsequence check, same position, handed the same bytes.

**Why after and not before.** Two reasons, and the first is the one that
decides it:

1. Cleanup is then handed, byte for byte, the text Whisper produced, exactly as
   it was before this feature existed. Its check is evaluated over the same
   input it always was, and "off" is provably today's behaviour rather than
   nearly it.
2. Cleanup's filler rules eat a comma that the filler was carrying — `um,` goes
   as a unit. Punctuation-first would hand it a comma this stage had just
   inserted, so "hello um comma world" would lose the comma he asked for.

**Its own guarantee.** It substitutes, so it cannot borrow cleanup's. It
carries a narrower one instead: a mark may only insert punctuation and
whitespace — checked on `insert` at load time, including on a rule the user
wrote — and afterwards `cleanup.engine.words_are_subsequence` is run over the
result. That is the same function, imported rather than copied. Deleting the
words of a mark phrase is a deletion and adding "," adds no word, so a correct
rule always passes and an incorrect one throws the whole stage away. Neither
pass can put a word into the document that was not spoken, which is the
property that matters.

**Mark or word** is the whole difficulty, and the rule is deliberately blunt: a
phrase is the WORD when the word in front of it is a determiner or possessive
("the comma goes here"), and the MARK everywhere else. Plurals never match.
What it gets wrong is asserted in `tests/test_punctuation.py::WhereTheRuleIsWrong`
rather than left to be discovered, and the escape — saying "literal" in front of
the phrase — is the way out of all of it.

**Whisper's own output is the other half of the design**, and it was measured
rather than assumed: a dictated mark comes back *punctuated as well as spelled
out*. "hello comma world" is transcribed `Hello, comma, world.` and "are you
sure question mark" as `Are you sure? Question mark.` So a substitution absorbs
the punctuation directly against it — his mark beats Whisper's guess in that
one spot — and phrases are matched word by word rather than as strings, because
Whisper writes `open, quote,` and `semi-colon`.

**Off by default.** Every phrase in the rules file is a phrase he can no longer
dictate literally, so installing a newer dictate must not silently start eating
the word "period" out of his sentences. One line in `dictate.toml` turns it on.

---

## The dictation history

He asked to "keep a history just for review". That framing is the whole
specification, and it settles more than it looks like it does: a thing to open
and read, not analytics, not telemetry, and nothing that leaves the machine.

`history.py` holds all of it and is plain standard library, so it is tested off
Windows like `recovery.py` and `tray.py`. The only platform call involved is the
one that opens the file for him.

* **What it keeps**: the text that was pasted, when he said it, how long he
  spoke for, and — only when dictate changed something on the way — what Whisper
  heard before the cleanup rules and spoken punctuation ran. That last pair is
  the evidence for "it ate a word" and for what a mark substituted, which is the
  argument a history has to be able to settle; when neither stage changed
  anything it would be the same sentence twice, so it is not written.
* **What it deliberately does not keep**: how long transcription took (a
  developer's question, already in the log), which window the text went to (that
  would make it a record of his day rather than of his words), and anything
  about a dictation that failed. Every line in the file is text that landed.
* **Where**: `history.txt` beside his `dictate.toml`, in the folder he already
  knows. Plain text, newest first, wrapped to 76 columns because Notepad opens
  with word wrap off and a dictation is one paragraph however long it is.
* **Bounded** by entry count — `[history] keep`, 200 by default — because
  newest-first means the file is rewritten whole each time, so the rewrite has
  to stay cheap. `config.validate()` refuses a cap high enough to make that
  rewrite something he would notice.
* **Deleting it** is one item on the tray menu and one command,
  `dictate history --delete`, and it removes the file. There is no confirmation
  dialog: a running dictate holds the single-instance lock, and a modal dialog
  behind that lock can sit unanswered until the next morning while `dictate run`
  answers "already running" — the reasoning is in `autostart.run_at_logon`, and
  it applies to any dialog this app might show, not only that one. So the item
  says exactly what it does instead. A mis-click costs a reading copy of text
  that already reached his documents.
* **Turning it off** stops anything new being written. It does *not* delete what
  is already there: data disappearing as a side effect of editing a config file
  is the wrong kind of surprise, and the config comment next to `enabled` says
  so and names the command.

The pipeline's share of this is one call, after the text has been delivered,
guarded so that a history that cannot be written can never turn a dictation that
worked into a reported failure. The store says so once and then stops going on
about it.

---

## Threading

| Thread | Owns | Rule |
|---|---|---|
| main | the Tk overlay message loop | every Tk call happens here; `set_state()` queues from elsewhere |
| hotkey | the keyboard hook | calls `start_utterance` / `finish_utterance`; every callback is wrapped so an exception cannot wedge the hook and with it the whole keyboard |
| audio | PortAudio | calls `push_audio` only, which appends and enqueues — it never runs a model |
| caption | — | the **only** thread that ever touches a streaming session |
| finalize | one worker | so two utterances finishing close together paste in the order they were spoken; runs clean → punctuate → paste |
| idle watch | the residency clock | ticks `ResidentModel.check_idle()`; a short-lived worker of its own does the unload and the reload, so neither the hotkey thread nor the timer ever blocks on one |
| tray | the notification-area icon and its window | a window's messages go to the thread that created it, so the icon is created, updated and destroyed there; `update()` from any other thread stores the state and posts a message |

Caption audio is queued with a bounded queue that drops the oldest block when
full. Captions are disposable, so dropping them is strictly better than blocking
the audio callback; the utterance buffer feeding the GPU pass is never dropped.

**Both kinds of dropped audio are counted per utterance and said out loud.** A
drop from that queue splices what the caption model hears, which is a perfectly
good reason for the words on screen to be wrong, and it was a `log.debug` nobody
has ever read. Audio the *operating system* discarded before dictate saw it —
PortAudio's `paInputOverflow` — is the more serious one, because it is missing
from the recording Whisper transcribes too, and it was counted process-wide and
logged on the 1st, 10th and 100th occurrence and then never again. They are two
messages at the release, never one, because the consequences differ: the first
affects only the screen, and the second may cost him a word in the document.

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
* **Starting at logon is never turned on for him.** He asked for it in general
  terms - "I thought it was gonna run like a service" - and the answer to that
  is still not to add ourselves to Windows startup on his behalf. Setup asks,
  once, at the START of the run while he is still at the keyboard, and takes no
  answer as no; the tray offers it as a toggle; a console `dictate run` mentions
  it once in its banner. Three offers and no default. What went wrong was never
  the mechanism - `dictate autostart enable` had been on his machine for hours -
  it was that the only way in was a typed command he had never been shown, and a
  discoverability defect is not fixed by making the product decide for him.
  `src/dictate/tray.autostart_item` and `setup-lib.Get-AutostartPlan` carry the
  rest, and neither keeps its own idea of whether it is on: both read what
  `dictate autostart status` reads, so the three answers cannot differ.
* **No installer, no Windows CI.** Separate follow-up task.
* **Nothing on top of the dictation history but opening it.** No re-pasting from
  it, no search, no window of its own. It is a text file he reads; each of those
  is a separate thing to ask for, and none of them was asked for.
* **No ROCm path.** Blocked on this card; kept only as the diagnostic in
  `scripts/check-rocm-gfx1030.py`.
* **`dictate update` does not adopt the install folder as a git clone.** The
  folder came out of a ZIP, so there is nothing to pull into, and the way to
  create one — `git init`, add a remote, fetch, hard reset — destroys whatever
  he has edited *before* anything can name it, and leaves a folder that is
  neither the old version nor the new one if it stops half way. The source is
  fetched as an archive over the authenticated API instead, unpacked somewhere
  else entirely, compared with what he has, and only then written file by file
  over a complete backup. `src/dictate/update.py` carries the ordering and why
  each step is where it is.
* **`dictate update` never rebuilds.** The toolchain, the compiled
  `whisper.cpp` and the models are in `C:\dictate-gpu` and do not change when
  the application does. A change that genuinely needs one of them says so and
  names `setup.ps1 -Only <step>`; it never spends half an hour he did not ask
  for. That distinction is the entire value of the command.
* **`dictate update` never forces a stop.** `dictate stop` may end a copy that
  will not answer, because he asked for it to be gone. An update has not been
  asked for that, so a copy that is mid-utterance, or that will not stop cleanly
  within its timeout, is left running and nothing is changed. The running copy
  publishes whether it is busy from the same place that keeps the tray icon
  honest (`instance.publish_activity`), so nothing in the hotkey or
  transcription path had to change to make that answerable.
* **The tray does not update dictate; it starts the command that does.** The
  process showing the menu is the process being replaced and restarted, so the
  work cannot happen inside it - the thread doing it would be killed part way
  through. `app._start_update` starts `dictate update` as a separate process
  with a console of its own, and that process stops this copy and brings it
  back through the ordinary path. The alternative, doing the writing in a
  worker thread and hoping to outrun our own shutdown, is not a design; it is
  a race with a corrupted install folder as the prize.

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
