"""Configuration: one obvious file, sane defaults, loud validation.

Design rules:

* Every setting has a default that is correct for the target machine described in
  `docs/DESIGN.md`, so a missing key is never an error.
* An *unknown* key IS an error. A typo in a config file is otherwise a silent
  failure, and this app is not allowed to fail silently.
* Values that would break a settled design decision (see `docs/DESIGN.md`) are
  rejected here with an explanation, not quietly accepted.
"""

from __future__ import annotations

import dataclasses
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError

# The live-caption Zipformer measurably gets *worse* above 2 threads
# (dictate-feasibility report S3c: 6 threads was 3x worse than 2).
# This is a settled decision, so the config refuses to break it silently.
CAPTION_THREAD_CEILING = 4


@dataclass
class HotkeyConfig:
    #: Held down to record. Modifier names: ctrl, alt, shift, win.
    combination: str = "ctrl + alt + space"
    #: "hold" is push-to-talk. "toggle" presses once to start, again to stop.
    mode: str = "hold"


@dataclass
class AudioConfig:
    #: Empty string means "the Windows default input device".
    device: str = ""
    #: 16 kHz mono is what both Whisper and the Zipformer want. Do not change.
    sample_rate: int = 16000
    channels: int = 1
    #: Mic callback block size. 32 ms keeps the caption pump responsive.
    block_ms: int = 32
    #: Shorter than this and we assume a mis-press: nothing is transcribed or pasted.
    min_utterance_ms: int = 350
    #: Hard stop, so a stuck key cannot grow the buffer without limit.
    max_utterance_s: int = 300


@dataclass
class CaptionConfig:
    enabled: bool = True
    #: Directory holding the sherpa-onnx streaming Zipformer (see scripts/fetch-models.ps1).
    model_dir: str = r"C:\dictate-gpu\models\sherpa-onnx-streaming-zipformer-en-2023-06-26"
    encoder: str = "encoder-epoch-99-avg-1-chunk-16-left-128.int8.onnx"
    decoder: str = "decoder-epoch-99-avg-1-chunk-16-left-128.onnx"
    joiner: str = "joiner-epoch-99-avg-1-chunk-16-left-128.int8.onnx"
    tokens: str = "tokens.txt"
    #: 2 is the measured optimum. Raising this makes captions slower, not faster.
    num_threads: int = 2
    provider: str = "cpu"
    decoding_method: str = "greedy_search"


@dataclass
class OverlayConfig:
    #: bottom-center | top-center | bottom-left | bottom-right | top-left | top-right
    position: str = "bottom-center"
    margin_px: int = 90
    max_width_px: int = 1100
    font_family: str = "Segoe UI"
    font_size: int = 20
    opacity: float = 0.88
    background: str = "#12121a"
    foreground: str = "#eaeaf2"
    accent: str = "#7aa2f7"
    #: Longest caption tail kept on screen. Older words scroll off the left.
    max_chars: int = 220
    #: Let mouse clicks fall through to the window underneath.
    click_through: bool = True


@dataclass
class WhisperConfig:
    """The resident GPU batch pass.

    `whisper-server` is started once and kept alive; the model stays in VRAM
    between utterances. See `docs/DESIGN.md` for why this is not `whisper-cli`.
    """

    server_exe: str = r"C:\dictate-gpu\whisper.cpp\build\bin\Release\whisper-server.exe"
    model: str = r"C:\dictate-gpu\models\ggml-large-v3-turbo.bin"
    host: str = "127.0.0.1"
    port: int = 8178
    #: CPU threads whisper.cpp uses for the non-GPU parts (mel, tokenisation).
    threads: int = 8
    language: str = "en"
    beam_size: int = 1
    best_of: int = 1
    use_gpu: bool = True
    #: whisper.cpp #3806: whisper-server crashes inside amdvlk64.dll with -fa on AMD.
    flash_attn: bool = False
    extra_args: list[str] = field(default_factory=list)
    #: Model load is ~2 s on disk, plus first-run Vulkan shader compilation.
    startup_timeout_s: float = 120.0
    request_timeout_s: float = 120.0
    #: How many times to resurrect a server that dies before giving up.
    max_restarts: int = 3
    #: Push one second of silence through at startup so the first real utterance
    #: does not pay for lazy GPU buffer allocation.
    warmup: bool = True


@dataclass
class CleanupConfig:
    enabled: bool = True
    #: Relative paths resolve against the config file's own directory.
    rules_file: str = "cleanup-rules.toml"


@dataclass
class PasteConfig:
    #: "sendinput" synthesises the characters directly and never touches the
    #: clipboard. "clipboard" copies, sends Ctrl+V, then restores the previous
    #: clipboard contents. See docs/DESIGN.md for the trade-off.
    method: str = "sendinput"
    #: Re-focus the window that had focus at hotkey press, if it lost focus.
    restore_focus: bool = True
    #: Some apps drop synthesised keystrokes sent with no gap at all.
    per_char_delay_ms: float = 0.0
    #: How long to wait after Ctrl+V before putting the old clipboard back.
    clipboard_restore_delay_ms: int = 300
    #: Append a trailing space so consecutive dictations do not run together.
    trailing_space: bool = True


@dataclass
class AutostartConfig:
    """Only used when dictate starts itself at logon (`dictate autostart enable`).

    None of it has any effect on a `dictate run` typed at a prompt.
    """

    #: How long after reaching the desktop to start. At logon the graphics driver
    #: may still be loading and another drive may not be mounted yet. Baked into
    #: the scheduled task, so changing it means running `autostart enable` again.
    logon_delay_s: int = 30
    #: How many times to try before giving up and reporting. Bounded on purpose:
    #: something that retries forever never tells anyone anything.
    startup_attempts: int = 5
    retry_delay_s: float = 20.0
    #: Put a dialog on screen if it could not start at all. The log has it either
    #: way; this is so he finds out without having been told to look.
    notify_on_failure: bool = True


@dataclass
class LoggingConfig:
    level: str = "INFO"
    #: Empty string means stderr only.
    file: str = ""


@dataclass
class Config:
    hotkey: HotkeyConfig = field(default_factory=HotkeyConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    captions: CaptionConfig = field(default_factory=CaptionConfig)
    overlay: OverlayConfig = field(default_factory=OverlayConfig)
    whisper: WhisperConfig = field(default_factory=WhisperConfig)
    cleanup: CleanupConfig = field(default_factory=CleanupConfig)
    paste: PasteConfig = field(default_factory=PasteConfig)
    autostart: AutostartConfig = field(default_factory=AutostartConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    #: Directory the config was loaded from; relative paths resolve against it.
    source_dir: Path = field(default_factory=Path.cwd)
    source_path: Path | None = None

    # -- derived helpers -------------------------------------------------

    def resolve(self, value: str) -> Path:
        """Resolve a possibly-relative config path against the config's directory."""
        p = Path(os.path.expandvars(value)).expanduser()
        return p if p.is_absolute() else (self.source_dir / p)

    @property
    def caption_paths(self) -> dict[str, Path]:
        base = self.resolve(self.captions.model_dir)
        return {
            "encoder": base / self.captions.encoder,
            "decoder": base / self.captions.decoder,
            "joiner": base / self.captions.joiner,
            "tokens": base / self.captions.tokens,
        }

    @property
    def block_frames(self) -> int:
        return max(1, int(self.audio.sample_rate * self.audio.block_ms / 1000))


_SECTIONS: dict[str, type] = {
    "hotkey": HotkeyConfig,
    "audio": AudioConfig,
    "captions": CaptionConfig,
    "overlay": OverlayConfig,
    "whisper": WhisperConfig,
    "cleanup": CleanupConfig,
    "paste": PasteConfig,
    "autostart": AutostartConfig,
    "logging": LoggingConfig,
}


def _build_section(name: str, cls: type, raw: Any) -> Any:
    if not isinstance(raw, dict):
        raise ConfigError(
            f"Config section [{name}] should be a table of settings, got {type(raw).__name__}.",
            f"Check the [{name}] section of your config file.",
        )
    fields = {f.name: f for f in dataclasses.fields(cls)}
    unknown = sorted(set(raw) - set(fields))
    if unknown:
        known = ", ".join(sorted(fields))
        raise ConfigError(
            f"Unknown setting(s) in [{name}]: {', '.join(unknown)}.",
            f"Valid settings for [{name}] are: {known}. Check for a typo.",
        )
    kwargs: dict[str, Any] = {}
    for key, value in raw.items():
        expected = fields[key].type
        value = _coerce(name, key, expected, value)
        kwargs[key] = value
    return cls(**kwargs)


def _coerce(section: str, key: str, expected: Any, value: Any) -> Any:
    """Light type checking. TOML gives us real types, so this catches genuine
    mistakes (a string where a number belongs) rather than doing conversions."""
    exp = expected if isinstance(expected, str) else getattr(expected, "__name__", "")
    if exp == "float" and isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    checks = {
        "int": (int,),
        "float": (float,),
        "bool": (bool,),
        "str": (str,),
        "list[str]": (list,),
    }
    allowed = checks.get(exp)
    if allowed is None:
        return value
    # bool is a subclass of int in Python; do not let True satisfy an int field.
    if exp != "bool" and isinstance(value, bool):
        raise ConfigError(
            f"[{section}] {key} should be {exp}, got a true/false value.",
            f"Set {key} to a {exp} value.",
        )
    if not isinstance(value, allowed):
        raise ConfigError(
            f"[{section}] {key} should be {exp}, got {type(value).__name__}.",
            f"Set {key} to a {exp} value.",
        )
    if exp == "list[str]" and not all(isinstance(v, str) for v in value):
        raise ConfigError(
            f"[{section}] {key} should be a list of strings.",
            f"Write {key} as a list of quoted strings, e.g. [\"--foo\", \"bar\"].",
        )
    return value


_POSITIONS = {
    "bottom-center",
    "bottom-left",
    "bottom-right",
    "top-center",
    "top-left",
    "top-right",
}


def validate(cfg: Config) -> Config:
    """Reject settings that are impossible or that break a settled design decision."""
    if cfg.hotkey.mode not in ("hold", "toggle"):
        raise ConfigError(
            f"[hotkey] mode must be 'hold' or 'toggle', got {cfg.hotkey.mode!r}.",
            "Use mode = \"hold\" for push-to-talk.",
        )
    if not cfg.hotkey.combination.strip():
        raise ConfigError(
            "[hotkey] combination is empty.",
            'Set something like combination = "ctrl + alt + space".',
        )
    if cfg.audio.sample_rate != 16000:
        raise ConfigError(
            f"[audio] sample_rate is {cfg.audio.sample_rate}, but both models require 16000.",
            "Leave sample_rate at 16000. Resampling happens in the driver, not here.",
        )
    if cfg.audio.channels != 1:
        raise ConfigError(
            f"[audio] channels is {cfg.audio.channels}, but transcription needs mono.",
            "Leave channels at 1.",
        )
    if not 1 <= cfg.audio.block_ms <= 500:
        raise ConfigError(
            f"[audio] block_ms is {cfg.audio.block_ms}, which is outside 1-500.",
            "32 is a good value.",
        )
    if cfg.audio.max_utterance_s <= 0:
        raise ConfigError(
            "[audio] max_utterance_s must be positive.",
            "300 (five minutes) is the default.",
        )
    if cfg.captions.num_threads < 1:
        raise ConfigError(
            "[captions] num_threads must be at least 1.",
            "2 is the measured optimum.",
        )
    if cfg.captions.num_threads > CAPTION_THREAD_CEILING:
        raise ConfigError(
            f"[captions] num_threads is {cfg.captions.num_threads}. "
            f"The live-caption model was measured to get *worse* above 2 threads "
            f"(6 threads was 3x slower than 2), so values above "
            f"{CAPTION_THREAD_CEILING} are refused.",
            "Set num_threads = 2. This is a settled decision, see docs/DESIGN.md.",
        )
    if cfg.overlay.position not in _POSITIONS:
        raise ConfigError(
            f"[overlay] position {cfg.overlay.position!r} is not recognised.",
            "Valid positions: " + ", ".join(sorted(_POSITIONS)) + ".",
        )
    if not 0.05 <= cfg.overlay.opacity <= 1.0:
        raise ConfigError(
            f"[overlay] opacity is {cfg.overlay.opacity}, which is outside 0.05-1.0.",
            "0.88 is the default.",
        )
    if cfg.overlay.max_chars < 20:
        raise ConfigError(
            "[overlay] max_chars must be at least 20.",
            "220 is the default.",
        )
    if not 1 <= cfg.whisper.port <= 65535:
        raise ConfigError(
            f"[whisper] port {cfg.whisper.port} is not a valid TCP port.",
            "8178 is the default.",
        )
    if cfg.whisper.threads < 1:
        raise ConfigError("[whisper] threads must be at least 1.", "8 is the default.")
    if cfg.whisper.max_restarts < 0:
        raise ConfigError("[whisper] max_restarts cannot be negative.", "3 is the default.")
    if cfg.whisper.flash_attn:
        # Not refused - it is the user's machine - but they get told.
        pass
    if cfg.paste.method not in ("sendinput", "clipboard"):
        raise ConfigError(
            f"[paste] method must be 'sendinput' or 'clipboard', got {cfg.paste.method!r}.",
            "sendinput is the default and never touches your clipboard.",
        )
    if not 0 <= cfg.autostart.logon_delay_s <= 600:
        raise ConfigError(
            f"[autostart] logon_delay_s is {cfg.autostart.logon_delay_s}, which is "
            "outside 0-600.",
            "30 is the default: long enough for the graphics driver to finish "
            "loading, short enough that dictate is ready before you are.",
        )
    if not 1 <= cfg.autostart.startup_attempts <= 20:
        raise ConfigError(
            f"[autostart] startup_attempts is {cfg.autostart.startup_attempts}, "
            "which is outside 1-20.",
            "5 is the default. It has to be bounded: something that retries "
            "forever never reports that it failed.",
        )
    if not 1.0 <= cfg.autostart.retry_delay_s <= 300.0:
        raise ConfigError(
            f"[autostart] retry_delay_s is {cfg.autostart.retry_delay_s}, which is "
            "outside 1-300.",
            "20 is the default.",
        )
    if cfg.logging.level.upper() not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        raise ConfigError(
            f"[logging] level {cfg.logging.level!r} is not a logging level.",
            "Use DEBUG, INFO, WARNING, ERROR or CRITICAL.",
        )
    return cfg


def from_mapping(raw: dict[str, Any], *, source_dir: Path | None = None,
                 source_path: Path | None = None) -> Config:
    unknown = sorted(set(raw) - set(_SECTIONS))
    if unknown:
        raise ConfigError(
            f"Unknown config section(s): {', '.join('[' + u + ']' for u in unknown)}.",
            "Valid sections: " + ", ".join(f"[{s}]" for s in _SECTIONS) + ".",
        )
    sections = {
        name: _build_section(name, cls, raw[name]) if name in raw else cls()
        for name, cls in _SECTIONS.items()
    }
    cfg = Config(
        **sections,
        source_dir=source_dir or Path.cwd(),
        source_path=source_path,
    )
    return validate(cfg)


def load(path: str | os.PathLike[str] | None) -> Config:
    """Load a config file. `None` (or a missing file) yields validated defaults."""
    if path is None:
        return validate(Config())
    p = Path(path)
    if not p.exists():
        raise ConfigError(
            f"Config file not found: {p}",
            "Copy config/dictate.example.toml to that path, or run "
            "`dictate init` to write one for you.",
        )
    try:
        # utf-8-sig, not utf-8: Notepad and Windows PowerShell both write UTF-8
        # with a byte order mark, and tomllib rejects that mark as a syntax
        # error on line 1 - which reads as "your config is broken" when nothing
        # is wrong with it. Files without a mark are unaffected.
        raw = tomllib.loads(p.read_text(encoding="utf-8-sig"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            f"Config file {p} is not valid TOML: {exc}",
            "Check for a missing quote or bracket on the line mentioned above.",
        ) from exc
    except OSError as exc:
        raise ConfigError(f"Could not read config file {p}: {exc}",
                          "Check the file exists and is readable.") from exc
    return from_mapping(raw, source_dir=p.parent.resolve(), source_path=p.resolve())


def default_config_path() -> Path:
    """Where the app looks for its config if you do not pass `--config`."""
    env = os.environ.get("DICTATE_CONFIG")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / "dictate" / "dictate.toml"
    return Path.home() / ".config" / "dictate" / "dictate.toml"
