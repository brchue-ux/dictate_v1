"""`dictate doctor` - the first-run experience.

The product owner is not a developer and the machine this runs on is not one
anybody here can reach. So when something is missing, the app has to say which
thing, where it looked, and what to type. That is all this module is: a list of
checks, each of which returns a status and, when it fails, a remedy.

The checks are ordinary functions returning `CheckResult`, so the report
formatting is tested (tests/test_doctor.py) without needing anything to be
installed.
"""

from __future__ import annotations

import shutil
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .config import Config
from .errors import DictateError

#: whisper.cpp's ggml-large-v3-turbo.bin, f16. Measured in the GPU report.
EXPECTED_TURBO_BYTES = 1_624_555_275


class Status(Enum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass
class CheckResult:
    name: str
    status: Status
    detail: str = ""
    remedy: str = ""


ICONS = {Status.OK: "[ ok ]", Status.WARN: "[warn]", Status.FAIL: "[FAIL]"}


def format_report(results: Iterable[CheckResult], width: int = 78) -> str:
    """Render the checklist. Failures repeat at the bottom with their remedy, so
    the thing to do next is the last thing on screen."""
    lines: list[str] = []
    results = list(results)
    for r in results:
        head = f"{ICONS[r.status]} {r.name}"
        lines.append(head if not r.detail else f"{head}\n         {r.detail}")

    problems = [r for r in results if r.status is not Status.OK and r.remedy]
    if problems:
        lines.append("")
        lines.append("-" * width)
        lines.append("What to do:")
        for i, r in enumerate(problems, 1):
            lines.append("")
            lines.append(f"{i}. {r.name}")
            for line in r.remedy.splitlines():
                lines.append(f"   {line}")
    else:
        failures = [r for r in results if r.status is Status.FAIL]
        if not failures:
            lines.append("")
            lines.append("Everything dictate needs is in place.")
    return "\n".join(lines)


def worst(results: Iterable[CheckResult]) -> Status:
    statuses = [r.status for r in results]
    if Status.FAIL in statuses:
        return Status.FAIL
    return Status.WARN if Status.WARN in statuses else Status.OK


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------


def check_platform() -> CheckResult:
    if sys.platform == "win32":
        return CheckResult("Operating system", Status.OK, "Windows")
    return CheckResult(
        "Operating system", Status.FAIL, sys.platform,
        "dictate's microphone capture, hotkey, overlay and paste are Windows-only.\n"
        "Everything else (transcription, cleanup, the tests) runs anywhere, but\n"
        "the app itself has to run on the Windows machine.",
    )


def check_python() -> CheckResult:
    v = sys.version_info
    text = f"{v.major}.{v.minor}.{v.micro}"
    if v >= (3, 11):
        return CheckResult("Python version", Status.OK, text)
    return CheckResult(
        "Python version", Status.FAIL, text,
        "dictate needs Python 3.11 or newer (it reads TOML config with the\n"
        "standard library). Install it with:\n"
        "  winget install --id Python.Python.3.12",
    )


def check_package(module: str, pip_name: str, why: str, *,
                  required: bool = True) -> CheckResult:
    name = f"Package: {pip_name}"
    try:
        __import__(module)
    except ImportError:
        return CheckResult(
            name, Status.FAIL if required else Status.WARN, "not installed",
            f"{why}\nInstall it with:\n  pip install {pip_name}",
        )
    except OSError as exc:
        return CheckResult(
            name, Status.FAIL, f"installed but would not load: {exc}",
            f"Reinstall it with:\n  pip install --force-reinstall {pip_name}",
        )
    return CheckResult(name, Status.OK, "installed")


def check_file(name: str, path: Path, *, min_bytes: int = 0,
               expect_bytes: int | None = None, remedy: str = "") -> CheckResult:
    if not path.exists():
        return CheckResult(name, Status.FAIL, f"not found at {path}", remedy)
    size = path.stat().st_size
    human = f"{size / 1024 / 1024:.0f} MB" if size > 1024 * 1024 else f"{size} bytes"
    if min_bytes and size < min_bytes:
        return CheckResult(
            name, Status.FAIL, f"{path} is only {human}",
            "That is far too small, so the download was cut short.\n"
            "Delete the file and download it again.\n" + remedy,
        )
    if expect_bytes is not None and size != expect_bytes:
        return CheckResult(
            name, Status.WARN, f"{path} is {human} ({size} bytes, expected {expect_bytes})",
            "This is not the exact file size recorded for this model. It may be a\n"
            "different quantisation, which is fine, or a truncated download, which\n"
            "is not. If transcription comes back as nonsense, download it again.",
        )
    return CheckResult(name, Status.OK, f"{path} ({human})")


def check_whisper_server(cfg: Config) -> CheckResult:
    exe = cfg.resolve(cfg.whisper.server_exe)
    return check_file(
        "whisper-server binary", exe, min_bytes=10_000,
        remedy="Run setup again - it builds whisper.cpp and fills this path in\n"
               "for you, and skips everything that is already done:\n"
               "  powershell -ExecutionPolicy Bypass -File setup.ps1\n"
               "To do only the build:\n"
               "  powershell -ExecutionPolicy Bypass -File scripts\\build-whisper-vulkan.ps1\n"
               "then put the path it prints into [whisper] server_exe in your config.",
    )


def check_whisper_model(cfg: Config) -> CheckResult:
    model = cfg.resolve(cfg.whisper.model)
    expect = EXPECTED_TURBO_BYTES if model.name == "ggml-large-v3-turbo.bin" else None
    return check_file(
        "Whisper model", model, min_bytes=100 * 1024 * 1024, expect_bytes=expect,
        remedy="Run setup again - it downloads the models and fills this path in\n"
               "for you, and resumes rather than starting a download over:\n"
               "  powershell -ExecutionPolicy Bypass -File setup.ps1\n"
               "To do only the download:\n"
               "  powershell -ExecutionPolicy Bypass -File scripts\\fetch-models.ps1\n"
               "then put the path into [whisper] model in your config.",
    )


def check_caption_model(cfg: Config) -> CheckResult:
    if not cfg.captions.enabled:
        return CheckResult("Live-caption model", Status.OK, "captions disabled in config")
    missing = [str(p) for p in cfg.caption_paths.values() if not p.exists()]
    if missing:
        return CheckResult(
            "Live-caption model", Status.WARN, f"{len(missing)} file(s) missing",
            "Live captions will be off; the pasted text is unaffected.\n"
            "Missing:\n  " + "\n  ".join(missing) + "\n"
            "Download the streaming Zipformer:\n"
            "  powershell -ExecutionPolicy Bypass -File scripts\\fetch-models.ps1\n"
            "then set [captions] model_dir to the folder it created.",
        )
    return CheckResult("Live-caption model", Status.OK, str(cfg.resolve(cfg.captions.model_dir)))


def check_port(cfg: Config) -> CheckResult:
    """Who has the transcription port, and whether that is a problem at all.

    It usually is not. dictate holding its own port while it runs is the correct
    state, and reporting it as a warning is how an install with nothing wrong
    with it got called broken. So this asks the lock who is running before it
    says anything: a port held by the copy that is running is `ok`, a port held
    by a whisper-server with no dictate behind it is the orphan, and it names
    the one command that clears it.
    """
    from . import instance as instance_mod
    from .engines.whisper_server import WhisperServerClient

    where = f"{cfg.whisper.host}:{cfg.whisper.port}"
    client = WhisperServerClient(cfg.whisper.host, cfg.whisper.port)
    if not client.port_is_open():
        return CheckResult("Transcription port", Status.OK, f"{where} is free")

    healthy = client.is_healthy()
    holder = instance_mod.running_instance()
    if holder is not None:
        return CheckResult(
            "Transcription port", Status.OK,
            f"{where} is in use by the copy of dictate that is running "
            f"({holder.describe()}) - which is where it should be",
        )
    what = ("a whisper-server that is loaded and answering"
            if healthy else "something")
    return CheckResult(
        "Transcription port", Status.WARN,
        f"{where} is in use by {what}, and no dictate is running",
        "An earlier run was ended without being allowed to shut down, and its\n"
        "transcription process is still holding the port. Clear it with:\n"
        "  dictate stop\n"
        "That is the whole fix - it clears a leftover as well as stopping a\n"
        "copy that is running. If something else on this PC needs that port,\n"
        "give dictate a different one with [whisper] port in your config.",
    )


def check_idle_release(cfg: Config) -> CheckResult:
    """What the graphics card is being asked to hold, and whether it holds it now.

    This is the answer to "is dictate using my VRAM at the moment?", which is
    the question the setting exists for. It is deliberately the only place that
    reports it: unloading and reloading are routine, so they are not announced
    while he is working.
    """
    from .engines.residency import minutes_text
    from .engines.whisper_server import WhisperServerClient

    minutes = cfg.whisper.idle_release_minutes
    if minutes <= 0:
        return CheckResult(
            "Graphics card memory", Status.OK,
            "the model stays loaded for as long as dictate is running "
            "(idle_release_minutes = 0)",
        )
    loaded = WhisperServerClient(cfg.whisper.host, cfg.whisper.port).is_healthy()
    now = ("a transcription model is loaded right now" if loaded else
           "no transcription model is loaded right now - either dictate is not "
           "running, or it has given the memory back")
    return CheckResult(
        "Graphics card memory", Status.OK,
        f"given back after {minutes_text(minutes * 60.0)} without dictating; {now}",
    )


def check_install() -> CheckResult:
    """Which folder dictate is loaded from, which version it is, and whether an
    update was interrupted.

    The folder is here because "I updated it and nothing changed" has already
    cost an evening: the answer was that Python loads dictate from somewhere
    else. The version is here because `dictate --version` says 0.1.0 and always
    will, so it cannot tell anybody whether an update is needed.
    """
    from . import update as update_mod

    root = update_mod.find_install_root()
    stamp = update_mod.read_stamp(root)
    where = f"{root} ({stamp.describe()})" if stamp else \
        f"{root} (revision not recorded - `dictate update` records it)"

    try:
        update_mod.check_install_root(root)
    except DictateError as exc:
        # Not a broken dictate - it is running, or this check would not be - but
        # `dictate update` cannot replace a folder like this, and finding that
        # out at the moment he wants an update is worse than knowing now.
        return CheckResult("Install folder", Status.WARN, str(root), exc.remedy)

    journal = update_mod.read_journal()
    if journal is not None:
        return CheckResult(
            "Install folder", Status.WARN,
            f"{where}; an update did not finish",
            "An update was interrupted. The version that was there before it is "
            "kept, and\nthis puts it back:\n"
            "  dictate update --restore\n"
            "Or run the update again, which does the same thing first:\n"
            "  dictate update",
        )
    return CheckResult("Install folder", Status.OK, where)


#: Phrases that are ordinary speech somewhere, so removing them wherever they
#: appear eats real sentences. All six shipped in `filler_phrases`, which has no
#: guard of any kind, and the subsequence guarantee cannot catch it - a deletion
#: is exactly what the pass is allowed to do. They are guarded [[deletions]]
#: rules now, but the rules file is the user's copy and `dictate init` will not
#: overwrite it, so a copy taken before the fix is still doing this. Saying so is
#: the only way he finds out.
_UNGUARDED_PHRASES = ("you know", "i mean", "sort of", "kind of",
                      "like i said", "if that makes sense")


def check_cleanup_rules(cfg: Config) -> CheckResult:
    if not cfg.cleanup.enabled:
        return CheckResult("Cleanup rules", Status.OK, "cleanup disabled in config")
    path = cfg.resolve(cfg.cleanup.rules_file)
    try:
        from .cleanup import rules as rules_mod

        loaded = rules_mod.load(path)
    except DictateError as exc:
        return CheckResult("Cleanup rules", Status.FAIL, exc.message, exc.remedy)
    unguarded = [p for p in loaded.filler_phrases
                 if " ".join(p.lower().split()) in _UNGUARDED_PHRASES]
    if unguarded:
        listed = ", ".join(f'"{p}"' for p in unguarded)
        return CheckResult(
            "Cleanup rules", Status.WARN,
            f"{path}: filler_phrases still has {listed}, which is deleted "
            'wherever it appears - "Do you know the answer?" becomes "Do the '
            'answer?" and nothing warns you at the time',
            "Take the cleanup-rules.toml that ships beside dictate (it removes "
            "those phrases only where Whisper fenced them with commas, which is "
            "how it writes them when it hears them as filler), or delete those "
            "entries from filler_phrases yourself.",
        )
    return CheckResult(
        "Cleanup rules", Status.OK,
        f"{path} ({len(loaded.fillers)} filler words, "
        f"{len(loaded.filler_phrases)} phrases, {len(loaded.deletions)} patterns)",
    )


def check_hotkey(cfg: Config) -> CheckResult:
    from .platform.hotkey_spec import describe

    try:
        return CheckResult("Hotkey", Status.OK, describe(cfg.hotkey.combination))
    except DictateError as exc:
        return CheckResult("Hotkey", Status.FAIL, exc.message, exc.remedy)


def check_microphone(cfg: Config) -> CheckResult:
    try:
        from .platform.windows.audio import list_input_devices

        devices = list_input_devices()
    except DictateError as exc:
        return CheckResult("Microphone", Status.FAIL, exc.message, exc.remedy)
    except Exception as exc:
        return CheckResult("Microphone", Status.FAIL, str(exc),
                           "dictate could not ask Windows for your audio devices.")
    if not devices:
        return CheckResult(
            "Microphone", Status.FAIL, "no input devices found",
            "Plug in a microphone, and check Windows Settings > Privacy & security\n"
            "> Microphone allows desktop apps to use it.",
        )
    names = ", ".join(name for _, name, _ in devices[:3])
    return CheckResult("Microphone", Status.OK, f"{len(devices)} input device(s): {names}")


def check_vulkan_tools() -> CheckResult:
    """Not required at runtime, but its absence explains a failed build."""
    found = shutil.which("vulkaninfoSDK") or shutil.which("vulkaninfo")
    if found:
        return CheckResult("Vulkan SDK tools", Status.OK, found)
    return CheckResult(
        "Vulkan SDK tools", Status.WARN, "vulkaninfo not on PATH",
        # The identifier is KhronosGroup.VulkanSDK: winget files the SDK under
        # that publisher and only DISPLAYS it as LunarG. Asking for
        # LunarG.VulkanSDK gets "No package found matching input criteria",
        # which is what setup.ps1 used to do.
        "Only needed to BUILD whisper.cpp, not to run dictate. If you have not\n"
        "built it yet, run setup.ps1 - it installs the SDK for you. By hand:\n"
        "  winget install --id KhronosGroup.VulkanSDK\n"
        "or download it from https://vulkan.lunarg.com/sdk/home#windows",
    )


def collect(cfg: Config, *, checks: list[Callable[[], CheckResult]] | None = None
            ) -> list[CheckResult]:
    if checks is not None:
        return [c() for c in checks]
    windows = sys.platform == "win32"
    results = [
        check_platform(),
        check_python(),
        check_install(),
        check_hotkey(cfg),
        check_cleanup_rules(cfg),
        check_whisper_server(cfg),
        check_whisper_model(cfg),
        check_port(cfg),
        check_idle_release(cfg),
    ]
    if cfg.captions.enabled:
        results.append(check_caption_model(cfg))
        results.append(check_package(
            "sherpa_onnx", "sherpa-onnx",
            "Live captions need it. Without it the app still works, but no words\n"
            "appear while you speak.", required=False,
        ))
    if windows:
        results.append(check_package(
            "sounddevice", "sounddevice", "dictate cannot record without it."))
        results.append(check_package(
            "global_hotkeys", "global-hotkeys",
            "dictate cannot hear the push-to-talk key without it."))
        results.append(check_microphone(cfg))
    results.append(check_vulkan_tools())
    return results
