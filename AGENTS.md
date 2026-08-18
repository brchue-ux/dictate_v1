# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

## Where the real documentation is

- **`docs/DESIGN.md`** — the four settled decisions, the six constraints that break the
  product if ignored, the threading model, and what was deliberately not done. Read it
  before changing anything in `src/dictate/platform/` or `src/dictate/engines/`.
- **`README.md` → "What was verified, and what was not"** — the honest split between what
  the tests cover and what needs the product owner's Windows/AMD machine.
- The research this was built from lives outside the repo, in firstmate's
  `data/dictate-*` reports. `docs/DESIGN.md` carries the conclusions that matter.

## Running things

- Tests: `PYTHONPATH=src python -m unittest discover -s tests -t .` — stdlib only, no
  pip needed, runs anywhere. pytest collects them too if it is installed.
- Installing the product: `setup.ps1` is the single entry point, six steps, idempotent.
  `scripts/build-whisper-vulkan.ps1` and `scripts/fetch-models.ps1` are thin wrappers
  onto `setup.ps1 -Only <step>` — do not reimplement a step in them.
- The installer's own logic is tested: `scripts/tests/setup-lib.tests.ps1`, plain
  PowerShell, no Pester. Add a case there for anything you change in
  `scripts/setup-lib.ps1`.
- **PowerShell cannot be run on the machines this project is developed on.** CI is the
  only syntax check some changes get, so `.github/workflows/ci.yml` parses every `.ps1`
  with Windows PowerShell 5.1 before anything else runs.
- The core (pipeline, cleanup, config, process supervision, HTTP client) has **no
  third-party dependencies**, deliberately: that is what lets it be tested off Windows.
  Keep it that way; platform and model code goes behind the seams in
  `src/dictate/platform/base.py` and `src/dictate/engines/base.py`.

## Sharp edges

- **No fallback implementations, ever.** `platform/factory.py` raises on non-Windows
  rather than returning a no-op. `tests/test_cli.py` asserts `src/` contains no test
  doubles — fakes live in `tests/fakes.py`.
- **The cleanup pass may only delete words.** Rules have no replacement field, and
  `cleanup/engine.clean()` re-checks that the output words are a subsequence of the
  input words, discarding the whole cleanup if not. Do not add a rule type that can
  substitute text.
- **The one fault the guarantee cannot catch is a rule that deletes real speech**,
  because deleting is what the pass is permitted to do. `filler_phrases` in
  `config/cleanup-rules.toml` has no guard at all - it removes the phrase anywhere -
  and it shipped holding "you know", "I mean", "sort of", "kind of", "like I said"
  and "if that makes sense", so "Do you know the answer?" was pasted as "Do the
  answer?". The list is empty now and every one of those is a `[[deletions]]` rule
  requiring Whisper's comma fencing. `tests/test_cleanup.py::PhrasesThatAreAlsoRealSpeech`
  holds the line, list included; a phrase belongs in `filler_phrases` only if no
  sentence exists where it is meant literally.
- **`config/cleanup-rules.toml` is TOML**: plain settings must come *before* the
  `[[deletions]]` blocks, or they silently become fields of the last one. Same
  trap, same rule, in `config/voice-punctuation.toml`.
- **Nothing dictated may press a key, and `whisper-server` replies in lines.**
  whisper.cpp's `output_str` writes `"\n"` after EVERY segment, so the JSON
  `text` field is one line per segment; `WhisperServerClient._parse` joins them
  with a space, exactly as the `verbose_json` branch beside it always did.
  Leaving that newline in is what pressed Enter in his terminal and ran a
  command. Behind it, `plan_text` emits Return or Tab only when the caller
  passes `allow_return=True`, and `injector.send` applies
  `platform/line_breaks.py` once, above both paste methods - a newline on the
  clipboard submits just as well as a synthesised one. `[paste] line_breaks =
  "return"` is the only way back, and it is what makes the spoken "new line"
  mark do anything. `tests/test_stray_enter.py` holds the whole chain, and
  `docs/DESIGN.md` constraint 6 carries the reasoning. Related, same class:
  `platform/modifier_guard.py` waits for Ctrl/Alt/Win to come up before typing,
  because the chord's other keys can still be down when the paste happens.
- **Where the text is allowed to go is decided in `src/dictate/delivery.py`, not
  in the injector.** The window is still captured at hotkey press; what is new is
  what happens when he is somewhere ELSE when the words are ready. It used to
  raise that window over whatever he had moved to — or, if Windows refused, raise
  `InjectionError` and **destroy the text**: not on the clipboard, not in the
  history (which was written only after a delivery), and the audio already
  dropped at the release. Three things are load-bearing now. Nothing is ever
  pasted into a window he did not dictate into, at any setting — there is no
  third mode, on purpose. An unknown is never a change: no captured window, or
  no reading of what is in front now, means paste exactly as before, because
  refusing over an empty query is reporting a healthy system as broken. And a
  refusal must never lose the text — `Pipeline._hold` puts it on the clipboard
  (the one exception to constraint 3, announced in the same message) and in the
  history marked `delivered=False`, and it does **not** keep it on `self`, which
  is the shape `CaptionsCanNeverBePasted` enforces. **`on_focus_change =
  "restore"` no longer restores foreground:** `deferred.DeferredDelivery` holds
  one finished *batch* result outside the pipeline and pastes only after the
  user returns to the captured handle. A new dictation, a closed target or a
  real injection refusal ends the wait with the clipboard/history fallback;
  the injector's last foreground check may retry but may never focus the target,
  also re-checks that no newer dictation retired the wait, and a known process
  id guards against a closed target's hwnd being recycled.
  `docs/DESIGN.md` → "Where the finished text goes when he has moved on" carries
  the Windows API reasoning; `tests/test_delivery.py`, `tests/test_deferred.py`
  and `test_pipeline.py::FocusMovedWhileHeWasSpeaking` hold it. The final run in
  his actual terminal is still unverified and is spelled out in README.
- **The trigger can be one mouse button, and the button keeps its own job on a
  click.** Windows shows the middle and thumb buttons to nobody except a
  `WH_MOUSE_LL` hook, so `platform/windows/mouse.py` sits in the path of every
  mouse event on his machine: it decides and returns, and a worker thread does
  everything with any weight in it. All the deciding is pure and tested
  (`platform/mouse_trigger.py`, `platform/hotkey_spec.py`,
  `platform/trigger_pair.py`, `tests/test_mouse_trigger.py`) - **nothing about
  the hook itself has ever been run.** Three things are load-bearing: the
  button-down is always swallowed and the *length of the hold* decides whether
  the up is swallowed too (a dictation) or the click is replayed to the app (a
  click), which is the only shape that neither breaks Back all day nor
  navigates Back on every dictation; the replay is recognised on the way back in
  by dictate's own `dwExtraInfo` tag, never by "it was injected", so a mouse
  driver's events still trigger; and `[hotkey] keyboard_fallback` is registered
  alongside it and always works, because a hook can be refused at install and
  dropped afterwards and dictate cannot tell the second case from an idle mouse.
  Left and right are refused by name. `docs/DESIGN.md` → "The mouse trigger"
  carries the reasoning, and `hotkey_switch.MOUSE_COST` is the one place that
  says what each button costs - the middle button's autoscroll included.
- **The tray changes the hotkey by offering a short list, and writes it down.**
  He will not hand-edit a config file, and a running dictate may not show a
  dialog (it holds the instance lock), so "press the keys you want" is out -
  `src/dictate/hotkey_switch.py` carries that reasoning and the choices. Order
  is the safety: register the new combination, and only if Windows accepts it
  write the config, so a combination another program owns leaves both the
  hotkey and the file as they were (`app.change_hotkey`). The write is a
  one-line text edit, never a re-serialised TOML - `config_edit.py` preserves
  his comments, his CRLF endings and his byte order mark, and is the module to
  reuse for any other setting the tray ever changes.
- **Spoken punctuation is a stage of its own and must stay one.** It substitutes,
  which the cleanup pass is built to make impossible, so it lives in
  `src/dictate/punctuation/` and runs *after* cleanup — never inside it, never
  before it. Its own guarantee is that `insert` may hold no letter or digit,
  re-checked on the output with cleanup's own `words_are_subsequence` (imported,
  never copied). Order is load-bearing: cleanup's filler rules eat a trailing
  comma, so punctuating first would delete the comma he just asked for.
  `docs/DESIGN.md` carries the reasoning; `tests/test_punctuation.py` holds it,
  including transcripts MEASURED from large-v3-turbo and a
  `WhereTheRuleIsWrong` class that asserts the failures on purpose.
- **Whisper writes a dictated mark twice: as punctuation AND as the word.**
  MEASURED: "hello comma world" → `Hello, comma, world.`; "are you sure question
  mark" → `Are you sure? Question mark.`; and it writes `open, quote,` and
  `semi-colon`. That is why marks are matched word by word rather than as
  strings, and why a substitution absorbs the punctuation touching it. Do not
  "simplify" either back into a plain string replace.
- **Caption threads are capped at 4 in `config.validate()`** because more threads were
  measured to be slower. This is a settled decision, not a limitation to lift, and it
  is not the answer to late captions — `dictate captions <clip.wav>` is how to find
  out what is. That command (`cli.cmd_captions` over the pure `engines/measure.py`)
  reports first-word latency in *recording* time, the update interval, and the RTF,
  and it is the one measurement only his machine and his voice can settle.
- **Which caption model runs is config, not architecture, and it was changed on
  2026-08-13.** Decision 4 fixes "a small streaming transducer, display-only, never
  Whisper"; the file behind it is four keys in `[captions]`. The LibriSpeech-trained
  Zipformer that shipped first got whole phrases wrong on ordinary speech (MEASURED,
  `docs/DESIGN.md` → "The caption model", with the numbers). It is NVIDIA's streaming
  FastConformer now — English only, lower case, unpunctuated. **Lower case is
  load-bearing**: constraint 4 lets the caption stay on screen until the paste lands
  partly because it is visibly not the finished text, so a punctuated, sentence-cased
  caption model is not a free upgrade however much better it reads.
  `engines/sherpa_stream.py` names no architecture and no file on purpose — keep it
  that way. The migration trap: the file *names* inside the folder changed with the
  model, so `setup.ps1` writes all four keys together and never `model_dir` alone.
- **Never remove or rename a key from a config dataclass.** An *unknown* key is a
  hard error by design, so a key that disappears from `[overlay]` makes the product
  owner's existing `dictate.toml` — written by `dictate init` from the example —
  fail to load on startup. Add keys; deprecate by ignoring, never by deleting.
  `tests/test_config.py::ShippedExample` asserts every key of every section in the
  shipped example still exists.
- **The overlay lays itself out once per appearance, never per caption.** Text and
  colour are all `_apply` may touch; `geometry()`, `SetWindowPos` and
  `update_idletasks` belong in `_show()`. A window that re-measures every 320 ms is
  visibly restless in peripheral vision, and re-measuring is also the slower option.
  `tests/test_overlay.py::TheLooksOwnRules` greps for this, against source with the
  comments stripped — that file discusses the calls it must not make, at length.
- **The caption stays on screen from press until the pasted text lands**, greyed
  once the hotkey is released. Constraint 4 (caption text never reaches the
  document) is therefore held by the *shape* of the code, not by when the screen is
  cleared: the pipeline never holds caption text, so the release passes
  `platform.base.KEEP` (no text) rather than re-sending it; `Utterance` has no
  string field; `injector.send` has one call site. Keep all four true —
  `tests/test_pipeline.py::CaptionsCanNeverBePasted` is where each is held. `KEEP`
  may only ever keep text that is on screen *now*, or a previous utterance's words
  reappear under a new one. The guarantee is enforced by *grep over `vars(pipeline)`*,
  so it catches more than the obvious: skipping duplicate caption sends was tried and
  rejected because comparing against the last caption means keeping it here.
- **Audio thrown away is counted per utterance and said out loud, in two separate
  sentences.** `Pipeline.note_input_loss` (the OS discarded it — the recording
  Whisper transcribes has a hole, so the PASTED TEXT may be missing a word) and the
  bounded caption queue's own drops (only the screen was affected). Both were
  effectively silent before: the first was logged on the 1st, 10th and 100th
  occurrence and then never again, the second at DEBUG. Never merge the two
  messages — the consequences are not the same, and the second one exists partly to
  say the first did not happen.
- **Everything in `[overlay]` is a pixel value at 100% display scaling**, multiplied
  by the chosen monitor's DPI in `geometry.plan_slab`. Fonts are sized in pixels
  (Tk's negative-size form), not points, so scaling is decided here rather than
  inferred by Tk from the primary display.
- **Those pixel values describe the panel at `size = "huge"`, and are multiplied
  again by the size ladder** in `src/dictate/overlay_size.py` (`small` … `huge`,
  in eighths; ships at `compact`). Two multiplications, in this order and never
  folded together: the size knob is what he chose, the DPI scale is what the
  display demands. `text_size` and `panel_size` split the one knob and default to
  "follow `size`". Do not "simplify" the ladder into new defaults for
  `font_size`/`max_width_px`: his `dictate.toml` sets those explicitly, so a
  default change reaches him not at all. `docs/DESIGN.md` 2c carries the
  reasoning; `tests/test_overlay.py::TheSizeKnobs` holds the proportions.
- **Everything that writes to his config goes through `config_edit`.** The
  hotkey and the caption size both do (`app._persist_hotkey`,
  `overlay_size.write`), and anything else ever added must: it is the module
  that keeps his comments, his CRLF endings and his byte order mark, and a
  second "edit one line of TOML" is two ideas of what is safe, drifting.
  `overlay_size` only decides WHICH keys of `[overlay]` may be written that way.
- **Tk substitutes a missing font family silently.** `platform/fonts.py` compares
  what was asked for against `Font.actual("family")` and reports the substitution on
  startup. Anything that picks a font must go through it; "it looked wrong and
  nothing said why" is the failure it exists to prevent.
- **An update chosen from the tray runs in another process, with a console.**
  The copy running the tray is the copy being replaced and restarted, so a
  thread doing the work inside it would be killed part way through writing
  files. `app._start_update` therefore starts `dictate update` through
  `update.start_in_console` - a new console (a logon-started copy is under
  `pythonw.exe` and cannot print), `--pause` so the window is still readable
  after it ends, and `recovery.spawn_detached` so it breaks out of the job
  Task Scheduler runs us in and survives the stop it is about to ask for. The
  icon goes when this copy stops and comes back with the new one; that window
  is the whole user interface in between. A failure to *start* it has no window
  to appear in, so it goes through `notify("error")` - the icon turns red - and
  never a `MessageBoxW`, which would block the thread that owns the icon.
- **`dictate update --check` repairs nothing, including an interrupted update.**
  It names one and says to run `dictate update`. A check is what the tray's
  "Check for updates" runs, and the promise there is that choosing it cannot
  leave the folder different.
- **The tray icon is the only visible surface a logon-started copy has.** What it says
  and offers is pure and tested (`src/dictate/tray.py`, including the icon's own bytes);
  `platform/windows/tray.py` is the thin Win32 half and is untested by anyone. It must
  never block the app - a tray that will not start is reported and skipped, like live
  captions. Every menu item names the command that does the same thing, on purpose.
- **The dictation history is a file of everything he says, so it is his.** All of
  `src/dictate/history.py`, pure and tested anywhere. Three things are load-bearing:
  every line of his text is indented, which is what makes splitting the file back
  into entries on the rule line exact; the write is `os.replace` of a temp file, so
  an interrupted one cannot leave half a history; and `[history] enabled = false`
  stops new writes but never deletes what is there — that stays his decision, via
  `dictate history --delete` or the tray. It also must never cost him a dictation:
  `Pipeline._remember` and the store both swallow, and the store complains once.
- **`dictate overlay`** shows the caption panel with sample text and no dictation.
  It is the only way anyone without Windows can get the look in front of the product
  owner, so keep it working when you change the overlay. Its sample text is deliberately
  in the shape the caption model really produces — lower case, unpunctuated — so it
  has to move when the model does.
- **Model residency is bounded by use, not by process lifetime.**
  `engines/residency.py` (`ResidentModel`) wraps the batch backend and unloads
  whisper-server after `[whisper] idle_release_minutes` so the GPU memory goes back.
  Two things about it are load-bearing: the reload starts at hotkey **press**
  (`app.Application._on_hotkey_press`), not at release, which is what hides the load
  behind the speaking; and nothing whisper-specific lives in the wrapper, so a reload
  is an ordinary `start()` and the health wait, restart budget and clean shutdown all
  still apply. `docs/DESIGN.md` constraint 1 carries the reasoning.
- **The Vulkan SDK's winget id is `KhronosGroup.VulkanSDK`**, not
  `LunarG.VulkanSDK` — winget files it under that publisher and only *displays*
  "LunarG Inc.". The wrong id returns "No package found matching input criteria"
  (`0x8A150014`), which is a silent no-op unless someone checks for it.
- **Never treat an environment variable as proof a tool is installed.** Setup
  once read an unset `VULKAN_SDK` as "installed, Windows just has not announced
  it yet" and told the product owner to reboot, forever. Presence tests look for
  the thing itself: `Resolve-VulkanSdk` in `scripts/setup-lib.ps1` checks the
  session, the registry, `C:\VulkanSDK\<version>` on disk and the
  installed-programs list, then sets the variable for this session rather than
  asking for a restart. Every tool in the toolchain step carries its own such
  `Check`, run straight after its install so a failure is reported where it
  happened.
- **A step may only name a cause it has established, and "I do not know" is a
  real answer.** Twice now setup has asserted one it had not: the Vulkan step
  announced an SDK had installed when none had, and the install step blamed the
  internet for `[WinError 5] Access is denied` on `Scripts\dictate.exe` — the
  product owner went and checked his connection and his proxy. `Get-PipFailureKind`
  in `scripts/setup-lib.ps1` is the shape to copy: it reads what the tool printed
  and returns `locked`/`network`/`disk`/`unknown`, and `unknown` prints
  `Get-ToolErrorLines` under "Setup does not know why". Anything that ends a step
  reports evidence, not the most common explanation.
- **A process check can never answer "can this file be replaced".**
  `dictate stop --stale-only` looks at the instance lock and the transcription
  port; the lock belongs to the *python* process, while `Scripts\dictate.exe` is
  the launcher process above it, and antivirus and the Windows indexer hold
  freshly written executables without being dictate at all. So `Invoke-Install`
  asks the files themselves (`Test-FileReplaceable`), waits 20 s for a transient
  holder (`Wait-ForFilesReplaceable` — the port precedent is ~2 s), and only then
  stops, naming the holder via the Restart Manager (`Get-FileHolder`, rstrtmgr.dll,
  no admin rights, falls back to an image-path scan and never throws). CI holds a
  real handle from a real second process on the real `dictate.exe` and requires
  both outcomes; the unit tests skip that part off Windows via `Test-WindowsCase`,
  because file locks are mandatory only there.
- **Native commands in PowerShell go through `Invoke-Tool`.** git, cmake and pip write
  ordinary progress to stderr, which Windows PowerShell turns into a terminating error
  under `$ErrorActionPreference = 'Stop'`. Exit codes are what decide success.
- **Never render a captured stderr line with `.ToString()`** - use
  `Get-NativeOutputLine`. `2>&1` wraps each line in an ErrorRecord whose exception
  carries the text; for a BLANK line that text is empty and `ToString()` falls back to
  the exception's type name, so a blank line printed as
  `System.Management.Automation.RemoteException` in the middle of an install report.
- **Downloads are pinned by size AND SHA-256 in `scripts/models.psd1`**, which both the
  installer and CI read. Provenance for each fingerprint is in the comments there; do
  not add an entry you have not verified. The one deliberate exception is the Vulkan
  SDK installer fetched from LunarG when winget cannot install it: its version floats,
  so there is no fingerprint to pin, and `Test-InstallerSignature` requires a valid
  Authenticode signature naming LunarG instead. Nothing downloaded is ever run without
  one of those two checks.
- **CI has no GPU and never will.** Keep "it compiles and the tests pass" separate from
  "the GPU path runs" in the workflow, in the README, and in any PR description. A
  green tick is not GPU verification. The same applies to the logon task: CI proves
  Windows *accepts* it, never that it fires — a runner never logs on.
- **Starting at logon must never become a Windows service.** Services run in session 0
  with no interactive desktop, so the overlay cannot be shown and synthesised
  keystrokes reach nothing — it would install and start and do nothing. It is a
  per-user Task Scheduler logon task; `src/dictate/autostart.py` carries the reasoning
  and the settings that are load-bearing.
- **Starting at logon is offered in three places and turned on in none of them.**
  The mechanism was finished and on his machine for hours while he asked for the
  feature; what failed was that the only way in was a typed command. So setup asks
  at the START of its run (`Get-AutostartPlan`, and `Read-YesNoWithTimeout`, which
  can never hang an unattended install — no answer is no), the tray has **Start
  when I log in** (`tray.autostart_item`, tick = state, command = what a click
  does), and a console `dictate run` says it once (`autostart.console_hint`).
  Adding it to Windows startup without him choosing is still forbidden. None of
  the three keeps its own idea of whether it is on: the two in-process ones read
  `autostart.registered_or_unknown` and setup reads `dictate autostart status`
  back, so they cannot disagree. `None` means "could not read", and is never
  reported as OFF. The tray's answer is refreshed on the slow watch loop
  (`app.AUTOSTART_POLL_S`), never from `_tray_state` — the hotkey path calls that.
- **Enabling it starts the copy NOW; disabling it does not stop one.** The same
  defect came back: he turned it on, nothing visible happened, and the feature did
  not begin until his next logon. `autostart.start_now` is the whole of it and is
  called from `autostart.enable`, so the CLI, the tray and setup (which runs the
  CLI) cannot behave differently. Three things are load-bearing: it asks
  `instance.running_instance()` FIRST and starts nothing if there is one — which is
  always the case from the tray, since the tray is drawn by a running copy;
  it starts the same thing the task does, via `recovery.relaunch_argv(...,
  autostart=True)` through `spawn_detached`, so no console appears and it outlives
  the window that asked; and it reports only what it watched — `started`,
  `already-running`, `failed` (with the exit code or the OS error) or
  `unconfirmed`, never a fourth, cheerful one. The registration's outcome and the
  start's outcome are separate paragraphs of `enable`'s report for that reason.
  `disable` is deliberately NOT symmetric — its docstring carries why — and setup
  reads `Test-DictateRunningOn` back rather than assuming the start worked.
- **Only one copy may run**, or two hooks fight over the hotkey and two servers over
  the port. `src/dictate/instance.py` holds an exclusive byte-range lock for the life
  of the process, so the OS releases it on any kind of death and there is no such
  thing as a stale lock. It is plain stdlib and works on Linux too, which is what lets
  `tests/test_instance.py` contend for it with a real second process anywhere.
- **A child process may never outlive the parent, however the parent dies.**
  `platform/windows/job.py` puts every child `ManagedProcess` spawns in a job object
  with `KILL_ON_JOB_CLOSE`, so Windows ends it when dictate ceases to exist - Ctrl+C,
  the "Terminate batch job" prompt, End Task or a crash. No shutdown handler can cover
  that case, which is the whole reason it is the OS doing it. `docs/DESIGN.md`
  constraint 1b, and the CI job `orphan` proves it by killing a parent outright.
- **`dictate stop` asks first, and if it has to force, it takes the whole tree.**
  Never the parent alone: it owns a whisper-server holding the transcription port and
  ~1.6 GB of VRAM, so ending it on its own is what strands one. Everything the rescue
  decides lives in `src/dictate/recovery.py` (plain, tested anywhere); only netstat,
  tasklist and taskkill are behind the platform seam. It is also the ONE command every
  failure message is required to name - he should never have to work out which failure
  he is looking at.
- **Nothing may report a healthy system as broken.** `doctor.check_port` asks the lock
  who is running before it judges the port, and `setup.ps1` records a check it could
  not RUN (`$script:VerifyDeferred`) separately from a check that failed. Saying "not
  working yet" to someone whose install is perfect costs the trust of every other
  message the tool prints.
- **"Task Scheduler says the task is running" is not "dictate is running", and
  `autostart.status_lines` may never print the two as one story.** Windows'
  267009 (`STILL_RUNNING`) is about the process the task started; the instance
  lock is about the copy answering the hotkey; they came apart on 2026-08-14 and
  the report printed both without noticing. `reconcile` is the comparison and
  `DISAGREE_MARK` is what a test greps for, over the whole matrix of readings
  (`tests/test_autostart.py::TheTwoReadingsAreCompared`). Three things are
  load-bearing. **A pid is not an identity** - Windows reuses them, so the log's
  pid and the lock's pid count as one copy only when the lock record also agrees
  about HOW it was started. **An unknown is never a verdict**: `run_is_alive` and
  `LogonStart.alive` are three-valued, off-Windows and unreadable-schtasks both
  answer `None`, and the report says so and names `dictate autostart status
  --why`. And **the logon start's own pid is the only way to ask about that
  process at all** - it is written into `autostart.log` behind `PID_MARK`,
  superseded by `RESTART_MARK` across a tray restart, and `read_last_block` keeps
  the first TWO lines of a trimmed block for exactly that reason.
- **A failed logon start is alive, lockless and not running until the box is
  clicked.** `run_at_logon` gives the lock back before `_notify_failure` (commit
  f668453, deliberately), and `MessageBoxW` is modal - so the process sits there,
  Task Scheduler counts the task as Running the whole time, and the only outward
  sign was a status line that read as health. It writes `DIALOG_MARK` before the
  box and a closing line after it; the report reads those back. Do not "simplify"
  either write away, and do not make the dialog non-blocking without deciding
  what then tells him at all.
- **The console-less log begins before the CLI import.** The task and
  `start_now` enter through `pythonw.exe -m dictate`, where stdout and stderr are
  absent. `src/dictate/__main__.py` must open the canonical `autostart.log` and
  redirect both before importing `cli`; `run_at_logon` then writes the ordinary
  run block. Keep its minimal state-path resolver and marker literals aligned
  with `instance.state_dir`/`autostart` (the tests compare them), and keep the
  Windows CI launch of the real `pythonw.exe` — registering task XML alone does
  not exercise this boundary.
- **`dictate autostart status --why` reports and changes nothing.** It is the one
  line he pastes: what Windows has running (`ProcessTools.pids_named`, tasklist),
  who holds the transcription port, the lock, and schtasks' raw answer. It may
  never end a process or write a file. It is also honest about its own limits -
  tasklist prints no command lines, so two `pythonw.exe` is not two dictates and
  the report says so; the whisper-server count and the lock are what settle it.
- **Nothing under test may reach a blocking Win32 call.** `MessageBoxW` in
  `platform/windows/notify.py` waits for a click, and on a CI runner nobody ever
  clicks: the suite hung for hours instead of failing. `tests/test_autostart.py`
  patches `_notify_failure` for the whole logon-start class rather than relying on
  each test's config to turn it off — the test that found this was the one whose
  config is deliberately unparseable, so it could not read `notify_on_failure` at
  all. CI jobs carry `timeout-minutes`, and the suite runs under `python -u` so a
  hang names the test it is in rather than losing it in a buffer.
- **A modal dialog must never be shown while holding the instance lock.** It can sit
  there until the next morning, and `dictate run` would answer "already running" for
  a copy that gave up hours ago.
- **`dictate update` replaces the folder Python loads from, never a named one.**
  `update.find_install_root()` answers that from the module's own location, because
  the install is editable and updating some other copy would appear to succeed and
  change nothing. The source arrives as an API archive through the GitHub CLI (the
  repo is public since 2026-08-14, but `gh` needs a sign-in for any API call and
  dictate never handles the token — `gh` does), is unpacked and
  checked outside the install, and is written over a complete backup with a journal
  in `%LOCALAPPDATA%\dictate\updates`; an interrupted run is put back by the next
  `dictate update` or by `--restore`. It never rebuilds whisper.cpp or the models,
  and it never forces a stop. `docs/DESIGN.md` → "Things deliberately not done"
  carries the reasoning; `src/dictate/update.py` carries the ordering.
  **A 404 from GitHub names no cause**: while the repo was private it was read as
  "sign in", and that was the fourth instance of this project's oldest defect
  class the day the repo went public. `api_failure`'s docstring carries it; 401
  and 403 still name a cause because their answer establishes one.
- **`.dictate-install.json` is what `dictate --version` cannot be.** The version
  string is 0.1.0 and always will be, so the install folder carries its own record
  of the revision AND a SHA-256 per file — that is what makes "you have edited these
  three files" answerable rather than guessable. It is written by
  `update.record_install()`, called from `setup.ps1` at install time and by every
  update; never reimplement the hashing in PowerShell, or the two ideas of "which
  files count" will drift and report edits nobody made. It is deliberately not
  part of the tree it describes (`is_ignored`) but IS part of a backup.
- **A source archive's links are skipped AND named.** `CLAUDE.md` is a symlink to
  `AGENTS.md`, and dictate never writes a link out of an archive. `safe_members`
  therefore returns what it refused as well as what it kept, and `plan_apply` takes
  that as `kept_as_is`: without it, "not in the new tree" reads as "deleted
  upstream" and a tracked file disappears from the install folder on every update.
  CI caught this; do not simplify either return value away.
- **Anything that must not interrupt an utterance reads `instance.read_activity()`.**
  The running copy publishes busy/idle from `app.Application._refresh_tray`, on
  change only, which is the tray's existing answer rather than a second one. Do not
  move that write into the hotkey or transcription path.
- **What autostart costs is `[whisper] idle_release_minutes`, not "1.6 GB all day".**
  Since the idle release, leaving the logon task on holds no VRAM between dictation
  sessions. Anything that states the cost — the README, `autostart enable`'s output,
  the example config — reads that setting rather than asserting a number, because
  the honest answer is different when it is 0.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
