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
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .platform.fade import MIN_FADE_MS

# The live-caption Zipformer measurably gets *worse* above 2 threads
# (dictate-feasibility report S3c: 6 threads was 3x worse than 2).
# This is a settled decision, so the config refuses to break it silently.
CAPTION_THREAD_CEILING = 4

# Releasing the GPU model more often than this would unload it between one
# sentence and the next, which costs the ~2 s load that residency exists to
# avoid. 0 (never release) is still allowed and is a different thing entirely.
MIN_IDLE_RELEASE_MINUTES = 0.5


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
    """The caption window.

    Every pixel measurement here is at 100% display scaling. On a monitor set to
    150% they are all multiplied by 1.5, so the overlay is the same physical size
    on every display rather than two thirds the size on the scaled one.
    """

    #: bottom-center | top-center | bottom-left | bottom-right | top-left | top-right
    position: str = "bottom-center"
    margin_px: int = 64
    max_width_px: int = 1080
    font_family: str = "Fira Code"
    font_size: int = 18
    #: 1.0 on purpose. A partly transparent panel goes muddy over a white
    #: document, which is where this is looked at most.
    opacity: float = 1.0
    #: The lit face of the slab - the surface the words sit on.
    background: str = "#262a31"
    #: Caption text. Deliberately not white: comfortable, not maximum, contrast.
    foreground: str = "#c7ccd6"
    #: The state bar and word while recording. The one saturated thing on screen.
    accent: str = "#c8a45c"
    #: The unlit shoulder around the face, and the plinth it sits on.
    edge: str = "#0a0b0e"
    #: The state colour once recording has stopped - transcribing, and pasted.
    muted: str = "#7e8794"
    #: The state colour when something went wrong.
    error: str = "#c9705c"
    #: Thickness of that shoulder, and of the state bar. The plinth is twice it.
    edge_px: int = 8
    #: Space between the face's edge and anything drawn on it.
    padding_px: int = 26
    #: Caption lines the slab reserves. The window is this tall whether or not
    #: there is anything on the second line, so it never grows mid-sentence.
    lines: int = 2
    #: Longest caption tail kept on screen. Older words scroll off the left.
    max_chars: int = 110
    #: Let mouse clicks fall through to the window underneath.
    click_through: bool = True
    #: Ease the window's opacity in and out instead of appearing at full alpha.
    #: false restores the old behaviour, which snapped.
    fade: bool = True
    fade_in_ms: int = 260
    #: Longer than the fade in, deliberately. It runs after the text has already
    #: been pasted, so it costs nothing and reads as an object leaving rather
    #: than a box being switched off.
    fade_out_ms: int = 420
    fade_step_ms: int = 16
    #: Put the captions on the monitor holding the window captured at hotkey
    #: press, then the mouse pointer's monitor, then the primary one. false
    #: restores the old behaviour, which always used the primary display.
    follow_focus: bool = True
    #: per-monitor | system | off. Governs how the overlay is sized on displays
    #: with different scaling. "off" hands the scaling back to Windows.
    dpi_awareness: str = "per-monitor"


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
    #: Minutes without dictating after which whisper-server is shut down, so the
    #: graphics card gets its ~1.6 GB back for games and other work. Pressing
    #: the hotkey loads it again, and that load starts while you are still
    #: speaking. 0 means never: the model stays loaded until dictate exits.
    idle_release_minutes: float = 5.0


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
class TrayConfig:
    """The icon in the notification area.

    It is on by default and there is one reason for that: with
    `dictate autostart enable` there is no window at all, and something that is
    running with nothing on screen has to be visible somewhere. See
    `dictate/tray.py`.
    """

    enabled: bool = True


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
    tray: TrayConfig = field(default_factory=TrayConfig)
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
    "tray": TrayConfig,
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

_DPI_MODES = {"per-monitor", "system", "off"}

#: Tk accepts named colours too, but a typo in a name is a Tk error thrown deep
#: inside the overlay on the UI thread, where it becomes "captions stopped
#: working". Hex is checked here instead, where the message can say what to fix.
_COLOUR = re.compile(r"#[0-9a-fA-F]{6}")


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
            "1.0 is the default - a solid panel stays readable over a white page.",
        )
    if cfg.overlay.max_chars < 20:
        raise ConfigError(
            "[overlay] max_chars must be at least 20.",
            "110 is the default.",
        )
    for key in ("margin_px", "max_width_px", "edge_px", "padding_px", "font_size"):
        value = getattr(cfg.overlay, key)
        if value < 1:
            raise ConfigError(
                f"[overlay] {key} is {value}, and it has to be at least 1 pixel.",
                "Delete the line to get the default back.",
            )
    if not 1 <= cfg.overlay.lines <= 6:
        raise ConfigError(
            f"[overlay] lines is {cfg.overlay.lines}, which is outside 1-6.",
            "2 is the default. The slab reserves this many caption lines and "
            "never grows past them, so more lines means a taller window all the "
            "time, not only when you talk for longer.",
        )
    for key in ("fade_in_ms", "fade_out_ms"):
        value = getattr(cfg.overlay, key)
        if cfg.overlay.fade and value < MIN_FADE_MS:
            raise ConfigError(
                f"[overlay] {key} is {value} ms, which is short enough to read "
                f"as a flicker rather than a fade.",
                f"Use at least {MIN_FADE_MS}, or set fade = false to turn the "
                f"fade off altogether.",
            )
    if not 1 <= cfg.overlay.fade_step_ms <= 200:
        raise ConfigError(
            f"[overlay] fade_step_ms is {cfg.overlay.fade_step_ms}, which is "
            f"outside 1-200.",
            "16 is the default - about one step per frame at 60 Hz.",
        )
    if cfg.overlay.dpi_awareness not in _DPI_MODES:
        raise ConfigError(
            f"[overlay] dpi_awareness is {cfg.overlay.dpi_awareness!r}, which is "
            f"not one of {', '.join(sorted(_DPI_MODES))}.",
            'Use "per-monitor" unless captions misbehave on a second screen, in '
            'which case "off" hands the scaling back to Windows.',
        )
    for key in ("background", "foreground", "accent", "edge", "muted", "error"):
        value = getattr(cfg.overlay, key)
        if not _COLOUR.fullmatch(value):
            raise ConfigError(
                f"[overlay] {key} is {value!r}, which is not a colour dictate "
                f"can use.",
                'Write colours as "#rrggbb", for example "#262a31".',
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
    if cfg.whisper.idle_release_minutes < 0:
        raise ConfigError(
            "[whisper] idle_release_minutes cannot be negative.",
            "5 gives the graphics card its memory back after five minutes "
            "without dictating. 0 never gives it back, which is how dictate "
            "used to behave.",
        )
    if 0 < cfg.whisper.idle_release_minutes < MIN_IDLE_RELEASE_MINUTES:
        # Below about half a minute the model would be unloaded between one
        # sentence and the next, so every utterance would pay the ~2 s load that
        # keeping it resident exists to avoid (docs/DESIGN.md, constraint 1).
        raise ConfigError(
            f"[whisper] idle_release_minutes is {cfg.whisper.idle_release_minutes}, "
            f"which would unload the model between one sentence and the next and "
            f"make every dictation about two seconds slower.",
            f"Use {MIN_IDLE_RELEASE_MINUTES} or more (5 is the default), or 0 to "
            f"keep the model loaded for as long as dictate is running.",
        )
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
