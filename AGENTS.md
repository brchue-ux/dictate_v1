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
- **`dictate stop` asks; it never kills.** dictate owns a whisper-server child holding
  ~1.6 GB of VRAM and the port, and killing the parent orphans it. Anything that stops
  the app has to go through the same clean shutdown Ctrl+C uses.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
