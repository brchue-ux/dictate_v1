"""Command line.

    dictate run           hold the hotkey and talk (the actual product)
    dictate doctor        check everything the app needs, and say what to fix
    dictate init          write a config file you can edit
    dictate devices       list the microphones dictate can see
    dictate clean         run the cleanup rules over text on stdin
    dictate transcribe    push a .wav through the resident GPU pass and time it

`doctor`, `init` and `clean` all work on any platform, on purpose: they are the
commands you want when the app will not start.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

from . import __version__, app as app_mod, config as config_mod, doctor as doctor_mod
from .errors import DictateError

def _template_dir() -> Path | None:
    """Find the shipped `config/` templates.

    They live beside the source tree rather than inside the package, so an
    editable install finds them one way and someone running from a checkout
    finds them another. Both are checked rather than assumed.
    """
    candidates = [
        Path(__file__).resolve().parent.parent.parent / "config",  # src/ layout
        Path(__file__).resolve().parent / "config",                # if ever packaged
        Path.cwd() / "config",                                     # run from a checkout
    ]
    for path in candidates:
        if (path / "dictate.example.toml").exists():
            return path
    return None


def _out(msg: str = "") -> None:
    print(msg, flush=True)


def _err(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _load_config(args: argparse.Namespace) -> config_mod.Config:
    path = Path(args.config) if args.config else config_mod.default_config_path()
    if not path.exists():
        if args.config:
            raise DictateError(
                f"Config file not found: {path}",
                "Run `dictate init` to create one, or check the path you passed "
                "to --config.",
            )
        _err(app_mod.config_search_note(path))
        return config_mod.load(None)
    return config_mod.load(path)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    from .logging_setup import configure

    cfg = _load_config(args)
    configure(cfg.logging.level, cfg.logging.file)

    results = doctor_mod.collect(cfg)
    failures = [r for r in results if r.status is doctor_mod.Status.FAIL]
    if failures:
        _err("dictate cannot start yet:\n")
        _err(doctor_mod.format_report(failures))
        _err("\nRun `dictate doctor` for the full check.")
        return 2
    return app_mod.run(cfg, console=_err)


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    results = doctor_mod.collect(cfg)
    _out(f"dictate {__version__} - checking your setup")
    if cfg.source_path:
        _out(f"config: {cfg.source_path}")
    else:
        _out("config: built-in defaults (no file loaded)")
    _out("")
    _out(doctor_mod.format_report(results))
    return {doctor_mod.Status.OK: 0, doctor_mod.Status.WARN: 0,
            doctor_mod.Status.FAIL: 1}[doctor_mod.worst(results)]


def cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.path) if args.path else config_mod.default_config_path()
    templates = _template_dir()
    if templates is None:
        _err("Cannot find the config templates that ship with dictate.\n"
             "Copy config/dictate.example.toml and config/cleanup-rules.toml from\n"
             "the dictate_v1 folder to where you want them, by hand.")
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    pairs = [
        (templates / "dictate.example.toml", target),
        (templates / "cleanup-rules.toml", target.parent / "cleanup-rules.toml"),
    ]
    for src, dst in pairs:
        if dst.exists() and not args.force:
            _out(f"kept   {dst} (already exists; pass --force to overwrite)")
            continue
        shutil.copyfile(src, dst)
        _out(f"wrote  {dst}")
    _out("")
    _out("Open the first file and check the four paths near the top, then run:")
    _out("  dictate doctor")
    return 0


def cmd_devices(args: argparse.Namespace) -> int:
    from .platform.windows import audio as audio_mod

    for index, name, channels in audio_mod.list_input_devices():
        _out(f"{index:>3}  {name}  ({channels} channel(s))")
    _out("")
    _out('Put a name or a number in [audio] device, or leave it "" for the default.')
    return 0


def cmd_clean(args: argparse.Namespace) -> int:
    from .cleanup import rules as rules_mod
    from .cleanup.engine import clean

    cfg = _load_config(args)
    path = Path(args.rules) if args.rules else cfg.resolve(cfg.cleanup.rules_file)
    rules = rules_mod.load(path)
    text = " ".join(args.text) if args.text else sys.stdin.read()
    result = clean(text, rules)
    _out(result.text)
    if args.explain:
        _err("")
        _err(f"rules file: {path}")
        _err(f"applied:    {', '.join(result.applied) if result.applied else '(nothing)'}")
        if result.rejected_reason:
            _err(f"REJECTED:   {result.rejected_reason}")
    return 0


def cmd_transcribe(args: argparse.Namespace) -> int:
    """Time the resident GPU pass on a WAV file. This is the command to run on
    the Windows machine to turn the estimated 0.4-1.0 s into a real number."""
    from .audio import wav as wav_mod
    from .engines.whisper_backend import WhisperVulkanBackend
    from .logging_setup import configure

    cfg = _load_config(args)
    configure(cfg.logging.level, cfg.logging.file, quiet_console=False)
    pcm, rate = wav_mod.wav_to_pcm16(Path(args.wav).read_bytes())
    duration = len(pcm) / 2 / rate

    backend = WhisperVulkanBackend(cfg.whisper, resolve=cfg.resolve)
    t0 = time.monotonic()
    backend.start()
    load = time.monotonic() - t0
    try:
        _out(f"model resident after {load:.2f}s (paid once, not per utterance)")
        best = None
        for run in range(1, args.repeat + 1):
            t1 = time.monotonic()
            text = backend.transcribe(pcm, rate)
            elapsed = time.monotonic() - t1
            best = elapsed if best is None else min(best, elapsed)
            _out(f"run {run}: {elapsed:.2f}s  ({duration / elapsed:.1f}x real time)")
        _out("")
        _out(f"audio length     {duration:.1f}s")
        _out(f"best of {args.repeat}       {best:.2f}s")
        _out(f"budget           2-4s  ->  {'INSIDE' if best <= 2.0 else 'OVER'}")
        _out("")
        _out(text)
    finally:
        backend.stop()
    return 0


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dictate",
        description="Push-to-talk dictation: live captions while you speak, "
                    "clean text pasted when you stop.",
    )
    parser.add_argument("--version", action="version", version=f"dictate {__version__}")
    parser.add_argument("--config", help="path to dictate.toml "
                                         "(default: %%APPDATA%%\\dictate\\dictate.toml)")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("run", help="start dictating").set_defaults(func=cmd_run)
    sub.add_parser("doctor", help="check the setup and say what to fix").set_defaults(
        func=cmd_doctor)
    sub.add_parser("devices", help="list microphones").set_defaults(func=cmd_devices)

    p_init = sub.add_parser("init", help="write a config file you can edit")
    p_init.add_argument("path", nargs="?", help="where to write it")
    p_init.add_argument("--force", action="store_true", help="overwrite an existing file")
    p_init.set_defaults(func=cmd_init)

    p_clean = sub.add_parser("clean", help="run the cleanup rules over some text")
    p_clean.add_argument("text", nargs="*", help="text to clean (default: read stdin)")
    p_clean.add_argument("--rules", help="a rules file to use instead of the configured one")
    p_clean.add_argument("--explain", action="store_true", help="say which rules fired")
    p_clean.set_defaults(func=cmd_clean)

    p_tr = sub.add_parser("transcribe", help="time the GPU pass on a .wav file")
    p_tr.add_argument("wav")
    p_tr.add_argument("--repeat", type=int, default=3)
    p_tr.set_defaults(func=cmd_transcribe)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except DictateError as exc:
        _err(f"\n{exc.report()}\n")
        return 2
    except KeyboardInterrupt:
        _err("")
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
