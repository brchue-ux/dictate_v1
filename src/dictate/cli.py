"""Command line.

    dictate run           hold the hotkey and talk (the actual product)
    dictate stop          stop it, however stuck it is - the one way out
    dictate update        fetch the latest version and install it in place
    dictate autostart     start it (or stop it) starting itself when you log in
    dictate doctor        check everything the app needs, and say what to fix
    dictate init          write a config file you can edit
    dictate devices       list the microphones dictate can see
    dictate clean         run the cleanup rules over text on stdin
    dictate punctuate     turn spoken marks ("comma") into marks (",")
    dictate history       open what you have dictated, or delete it
    dictate overlay       show the caption overlay with sample text
    dictate transcribe    push a .wav through the resident GPU pass and time it

`doctor`, `init`, `clean` and `punctuate` all work on any platform, on purpose:
they are the commands you want when the app will not start. So does
`history --delete`: getting rid of a record of everything you have said must not
depend on dictate being in a fit state to run.

Exit codes: 0 fine, 1 something is missing, 2 an error with a remedy attached,
3 a copy of dictate is already running (and `stop --stale-only` left it alone),
130 Ctrl+C.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import threading
import time
from pathlib import Path

from . import (
    __version__, app as app_mod, autostart as autostart_mod, config as config_mod,
    doctor as doctor_mod, instance as instance_mod, pipeline as pipeline_mod,
    recovery as recovery_mod, update as update_mod,
)
from .errors import AlreadyRunningError, DictateError

#: `dictate run` while one is already running. Not a crash and not a success:
#: it did not start what was asked for, and it says why.
EXIT_ALREADY_RUNNING = 3

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

    if getattr(args, "autostart", False):
        return autostart_mod.run_at_logon(args.config)

    # Before anything is loaded and long before the hotkey or the port is taken:
    # two copies would fight over both, and the resulting failure is baffling.
    lock = instance_mod.InstanceLock(started_by="hand")
    try:
        lock.acquire()
    except AlreadyRunningError as exc:
        _err(f"\n{exc.report()}\n")
        return EXIT_ALREADY_RUNNING

    try:
        cfg = _load_config(args)
        configure(cfg.logging.level, cfg.logging.file)

        results = doctor_mod.collect(cfg)
        failures = [r for r in results if r.status is doctor_mod.Status.FAIL]
        if failures:
            _err("dictate cannot start yet:\n")
            _err(doctor_mod.format_report(failures))
            _err("\nRun `dictate doctor` for the full check.")
            return 2
        code = app_mod.run(cfg, console=_err)
    finally:
        # Before the restart below, always: the new copy takes this lock the
        # moment it starts, and one that raced its own predecessor would be
        # turned away as "dictate is already running".
        lock.release()

    if code == app_mod.EXIT_RESTART:
        return _restart(args)
    return code


def _restart(args: argparse.Namespace) -> int:
    """Start the fresh copy the tray's Restart item asked for."""
    try:
        pid = recovery_mod.relaunch(args.config)
    except DictateError as exc:
        _err(f"\n{exc.report()}\n")
        return 2
    _out(f"dictate has been restarted (process {pid}).")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    """The one command that gets out of any stuck state.

    It does not ask which failure this is. It stops a copy that is running the
    way Ctrl+C does; ends one that will not answer, together with the
    whisper-server it owns; clears a whisper-server an earlier run left behind
    on the transcription port; and names the exact thing to type if anything is
    left. Everything it decides is in `recovery.py` and is tested there.
    """
    try:
        cfg = _load_config(args)
    except DictateError as exc:
        # A config that will not load must not stop a rescue: the port is the
        # thing being rescued, and the default is what the app would have used.
        _err(f"(your config file could not be read, so the default "
             f"transcription port is assumed: {exc.message})")
        cfg = config_mod.load(None)

    outcome = recovery_mod.stop(cfg, say=_out, timeout_s=args.timeout,
                                stale_only=args.stale_only)
    if outcome.left_alone:
        return EXIT_ALREADY_RUNNING
    return 0 if outcome.ok else 1


def cmd_update(args: argparse.Namespace) -> int:
    """Fetch the current source and put it in place.

    Everything it decides is in `update.py` and is tested there. This says what
    was asked for and turns the answer into an exit code; every failure worth a
    remedy is a `DictateError` and reaches `main` below, exactly like the rest.
    """
    if args.restore:
        outcome = update_mod.restore(say=_out, config_path=args.config,
                                     timeout_s=args.timeout)
    else:
        outcome = update_mod.update(
            say=_out, config_path=args.config, repo=args.repo,
            branch=args.branch, check_only=args.check, force=args.force,
            timeout_s=args.timeout)
    return 0 if outcome.ok else 1


def cmd_autostart(args: argparse.Namespace) -> int:
    action = getattr(args, "autostart_command", None)
    if action == "enable":
        for line in autostart_mod.enable(_load_config(args), config_path=args.config):
            _out(line)
        return 0
    if action == "disable":
        for line in autostart_mod.disable():
            _out(line)
        return 0
    for line in autostart_mod.status_lines():
        _out(line)
    return 0


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
        (templates / "voice-punctuation.toml", target.parent / "voice-punctuation.toml"),
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


def cmd_history(args: argparse.Namespace) -> int:
    """Open what he has dictated, or delete it.

    Deliberately these two things only. Reading it back is what he asked for -
    "keep a history just for review" - and getting rid of it has to be one step
    that does not involve finding a file.
    """
    from . import history as history_mod

    cfg = _load_config(args)
    store = history_mod.HistoryStore(history_mod.path_for(cfg),
                                     keep=cfg.history.keep,
                                     enabled=cfg.history.enabled)

    if args.delete:
        if store.delete():
            _out(f"deleted {store.path}")
        else:
            _out(f"there was nothing to delete ({store.path} does not exist)")
        return 0

    _out(f"file      {store.path}")
    if not store.path.exists():
        _out("          (nothing dictated yet - it appears the first time you do)")
    else:
        _out(f"keeping   {store.count()} of the last {cfg.history.keep} dictations")
    if not cfg.history.enabled:
        # An old file outlives the setting on purpose: turning the history off
        # stops dictate writing, and deleting what is already there stays his
        # decision rather than a side effect of editing a config file.
        _out("          [history] enabled = false, so nothing new is being kept")
    _out("delete    dictate history --delete")
    _out("")
    if not store.path.exists():
        return 0
    if not hasattr(os, "startfile"):
        # Not a fallback that pretends: it says what it did not do, and the
        # path above is what to do with instead.
        _out("It is a plain text file - open it in whatever you read text in. "
             "(dictate can only open it for you on Windows.)")
        return 0
    try:
        os.startfile(str(store.path))  # noqa: S606 - Windows only, by design
    except OSError as exc:
        _err(f"dictate could not open it for you: {exc}")
        return 1
    _out("Opening it now.")
    return 0


#: What the preview shows. Ordinary dictated speech, in the shape the caption
#: model actually produces it - upper case, no punctuation - because that is what
#: has to look right, not a designer's sample sentence.
_PREVIEW_WORDS = (
    "SO THE THING I WANTED TO SAY IS THAT THE OVERLAY SHOULD BE CALM ENOUGH TO "
    "READ WITHOUT LOOKING STRAIGHT AT IT WHILE I AM STILL TALKING"
).split()


def cmd_punctuate(args: argparse.Namespace) -> int:
    """Spoken punctuation over text you type, with no dictating and no Windows.

    This is how the rules get argued with. `--explain` prints every substitution
    the stage made, which is the same list that goes into the log every time it
    runs - a mark in the wrong place has to be traceable back to the words that
    produced it.
    """
    from .punctuation import rules as rules_mod
    from .punctuation.engine import apply

    cfg = _load_config(args)
    path = Path(args.rules) if args.rules else cfg.resolve(cfg.punctuation.rules_file)
    rules = rules_mod.load(path)
    text = " ".join(args.text) if args.text else sys.stdin.read()
    result = apply(text, rules)
    _out(result.text)
    if args.explain:
        _err("")
        _err(f"rules file: {path}")
        if not cfg.punctuation.enabled:
            _err("NOTE:       [punctuation] enabled = false, so dictation itself "
                 "is not doing this yet.")
        if result.applied:
            for line in result.applied:
                _err(f"applied:    {line}")
        else:
            _err("applied:    (nothing - no spoken mark was found)")
        if result.rejected_reason:
            _err(f"REJECTED:   {result.rejected_reason}")
    return 0


def cmd_overlay(args: argparse.Namespace) -> int:
    """Show the caption overlay with sample text, without dictating.

    The whole appear-type-release-fade cycle, on demand, so the overlay can be
    looked at and argued with in seconds instead of a record-speak-release round
    trip - and so that changing a colour in the config is a two-second question.

    It drives the real overlay through the real interface. It is not a mock: the
    only thing standing in for the pipeline is a timer that feeds it words.
    """
    from .platform import base, factory
    from .platform.base import OverlayState

    cfg = _load_config(args)
    overlay = factory.make_overlay(cfg, notify=lambda level, msg: _err(f"   {msg}"))

    _out("Showing the caption overlay. It appears, fills with words, holds them")
    _out("greyed while it 'thinks', clears them as the text 'lands', then fades")
    _out("out. Ctrl+C to stop early.")
    _out("")
    _out(f"  font       {cfg.overlay.font_family} {cfg.overlay.font_size}pt")
    _out(f"  position   {cfg.overlay.position}, {cfg.overlay.margin_px}px margin, "
         f"{cfg.overlay.max_width_px}px wide")
    _out(f"  fade       {'off' if not cfg.overlay.fade else f'{cfg.overlay.fade_in_ms}ms in, {cfg.overlay.fade_out_ms}ms out'}")
    _out(f"  monitor    {'follows the focused window' if cfg.overlay.follow_focus else 'primary only'}")
    _out("")
    if args.error:
        _out("Showing the error state, then stopping.")

    def script() -> None:
        # Runs on a worker thread, exactly as the pipeline's threads do, so this
        # exercises the real cross-thread queueing rather than a shortcut.
        overlay.wait_ready()
        target = None
        try:
            target = factory.make_window_tracker().foreground()
        except Exception:
            pass  # the preview still works without knowing the focused window
        for _ in range(max(1, args.repeat)):
            if args.error:
                overlay.set_state(OverlayState.ERROR,
                                  "whisper-server stopped responding", target)
                time.sleep(3.0)
                break
            overlay.set_state(OverlayState.LISTENING, "", target)
            shown = ""
            for word in _PREVIEW_WORDS:
                shown = f"{shown} {word}".strip()
                # The same trimming the real caption path does, so the preview
                # shows the same tail behaviour rather than a tidier one.
                overlay.set_state(OverlayState.LISTENING,
                                  pipeline_mod.caption_tail(shown, cfg.overlay.max_chars))
                time.sleep(args.rate / 1000.0)
            time.sleep(0.4)
            # No text: the words he was reading stay up, greyed, exactly as
            # they do while the real GPU pass runs. `dictate overlay` is the
            # only way anyone without this PC can see that, so it has to be the
            # same call the pipeline makes.
            overlay.set_state(OverlayState.THINKING, base.KEEP)
            time.sleep(1.1)
            overlay.set_state(OverlayState.DONE, "")
            time.sleep(2.0)
        overlay.set_state(OverlayState.HIDDEN, "")
        time.sleep(1.0)
        overlay.close()

    thread = threading.Thread(target=script, name="dictate-overlay-preview",
                              daemon=True)
    thread.start()
    try:
        overlay.run_forever()
    except KeyboardInterrupt:
        overlay.close()
    _out("")
    _out(f"That was: {overlay.describe}")
    _out("Everything above is in the [overlay] block of your config.")
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

    p_run = sub.add_parser("run", help="start dictating")
    p_run.add_argument("--autostart", action="store_true",
                       help=argparse.SUPPRESS)  # how the logon task calls it
    p_run.set_defaults(func=cmd_run)

    p_stop = sub.add_parser(
        "stop",
        help="stop dictate and clear anything a previous run left behind")
    p_stop.add_argument("--timeout", type=float, default=20.0,
                        help="seconds to wait for it to go before ending it "
                             "(default: 20)")
    p_stop.add_argument("--stale-only", action="store_true",
                        help="only clear what an earlier run left behind; leave "
                             "a copy that is running alone. setup.ps1 uses this "
                             "so checking the install cannot stop your dictation")
    p_stop.set_defaults(func=cmd_stop)

    p_up = sub.add_parser(
        "update",
        help="fetch the latest version of dictate and install it in place")
    p_up.add_argument("--check", action="store_true",
                      help="say what would change and change nothing")
    p_up.add_argument("--force", action="store_true",
                      help="update even though you have edited files in the "
                           "install folder (a complete copy is kept first)")
    p_up.add_argument("--restore", action="store_true",
                      help="put back the version that was there before the "
                           "last update")
    p_up.add_argument("--branch", default=update_mod.DEFAULT_BRANCH,
                      help=f"the branch to update from "
                           f"(default: {update_mod.DEFAULT_BRANCH})")
    p_up.add_argument("--repo", default=update_mod.DEFAULT_REPO,
                      help=f"the GitHub repository to update from "
                           f"(default: {update_mod.DEFAULT_REPO})")
    p_up.add_argument("--timeout", type=float, default=30.0,
                      help="seconds to wait for a running dictate to stop "
                           "before giving up and changing nothing (default: 30)")
    p_up.add_argument("--pause", action="store_true",
                      help="wait for Enter before the window closes. The "
                           "notification area's Update now uses this, because "
                           "that window is the only place its report - and any "
                           "failure - is ever shown")
    p_up.set_defaults(func=cmd_update)

    p_auto = sub.add_parser("autostart", help="start dictate when you log in")
    p_auto.set_defaults(func=cmd_autostart)
    auto_sub = p_auto.add_subparsers(dest="autostart_command")
    auto_sub.add_parser("enable", help="start dictate when you log in")
    auto_sub.add_parser("disable", help="stop doing that, and leave nothing behind")
    auto_sub.add_parser("status", help="is it on, is it running, and did it start")

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

    p_punct = sub.add_parser(
        "punctuate",
        help='turn spoken marks into marks ("hello comma world")')
    p_punct.add_argument("text", nargs="*", help="text to punctuate (default: read stdin)")
    p_punct.add_argument("--rules", help="a rules file to use instead of the configured one")
    p_punct.add_argument("--explain", action="store_true",
                         help="say which mark each substitution came from")
    p_punct.set_defaults(func=cmd_punctuate)

    p_hist = sub.add_parser("history",
                            help="open what you have dictated, or delete it")
    p_hist.add_argument("--delete", action="store_true",
                        help="delete the whole history, now")
    p_hist.set_defaults(func=cmd_history)

    p_ov = sub.add_parser("overlay",
                          help="show the caption overlay with sample text, "
                               "without dictating")
    p_ov.add_argument("--repeat", type=int, default=1,
                      help="run the appear-and-fade cycle this many times")
    p_ov.add_argument("--rate", type=int, default=320,
                      help="milliseconds between words (default: 320, the real "
                           "caption update interval)")
    p_ov.add_argument("--error", action="store_true",
                      help="show the error state instead")
    p_ov.set_defaults(func=cmd_overlay)

    p_tr = sub.add_parser("transcribe", help="time the GPU pass on a .wav file")
    p_tr.add_argument("wav")
    p_tr.add_argument("--repeat", type=int, default=3)
    p_tr.set_defaults(func=cmd_transcribe)

    return parser


def wait_for_enter(read=input) -> None:
    """Hold a window open until what is in it has been read.

    `dictate update --pause` is how the notification area runs an update, and
    that window is the whole of the user interface for it: there is no console
    behind it and no other copy of what it said. A window that closed the
    instant the command ended would take a failure and its remedy with it.

    Nothing here may raise. A stdin that is not a keyboard - redirected, closed,
    or the `pythonw.exe` case where there is none at all - means nobody is
    waiting, and the right answer to that is to return, not to end a command
    that has already done its work with a traceback.
    """
    try:
        _out("")
        _out("Press Enter to close this window.")
        read()
    except (EOFError, KeyboardInterrupt, OSError, RuntimeError, AttributeError):
        pass


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
    finally:
        # Outside the `except` blocks on purpose: the message a failure prints
        # is the thing the window is being held open for, so the wait has to
        # come after it rather than instead of it.
        if getattr(args, "pause", False):
            wait_for_enter()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
