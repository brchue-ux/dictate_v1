# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

## Where the real documentation is

- **`docs/DESIGN.md`** — the four settled decisions, the five constraints that break the
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
- **`config/cleanup-rules.toml` is TOML**: plain settings must come *before* the
  `[[deletions]]` blocks, or they silently become fields of the last one.
- **Caption threads are capped at 4 in `config.validate()`** because more threads were
  measured to be slower. This is a settled decision, not a limitation to lift.
- **Never remove or rename a key from a config dataclass.** An *unknown* key is a
  hard error by design, so a key that disappears from `[overlay]` makes the product
  owner's existing `dictate.toml` — written by `dictate init` from the example —
  fail to load on startup. Add keys; deprecate by ignoring, never by deleting.
  `tests/test_overlay.py` asserts every key in the shipped example still exists.
- **The overlay lays itself out once per appearance, never per caption.** Text and
  colour are all `_apply` may touch; `geometry()`, `SetWindowPos` and
  `update_idletasks` belong in `_show()`. A window that re-measures every 320 ms is
  visibly restless in peripheral vision, and re-measuring is also the slower option.
  `tests/test_overlay.py::TheLooksOwnRules` greps for this, against source with the
  comments stripped — that file discusses the calls it must not make, at length.
- **Everything in `[overlay]` is a pixel value at 100% display scaling**, multiplied
  by the chosen monitor's DPI in `geometry.plan_slab`. Fonts are sized in pixels
  (Tk's negative-size form), not points, so scaling is decided here rather than
  inferred by Tk from the primary display.
- **Tk substitutes a missing font family silently.** `platform/fonts.py` compares
  what was asked for against `Font.actual("family")` and reports the substitution on
  startup. Anything that picks a font must go through it; "it looked wrong and
  nothing said why" is the failure it exists to prevent.
- **The tray icon is the only visible surface a logon-started copy has.** What it says
  and offers is pure and tested (`src/dictate/tray.py`, including the icon's own bytes);
  `platform/windows/tray.py` is the thin Win32 half and is untested by anyone. It must
  never block the app - a tray that will not start is reported and skipped, like live
  captions. Every menu item names the command that does the same thing, on purpose.
- **`dictate overlay`** shows the caption panel with sample text and no dictation.
  It is the only way anyone without Windows can get the look in front of the product
  owner, so keep it working when you change the overlay.
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
