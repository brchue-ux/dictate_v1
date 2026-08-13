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

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
