"""The GPU batch pass: Whisper large-v3-turbo, resident, via whisper.cpp Vulkan.

Why a server and not `whisper-cli.exe` per utterance: loading the 1.6 GB f16
model takes about two seconds and it has to live in VRAM. Paying that per
utterance would spend the entire 2-4 s budget before any audio was looked at.
`whisper-server` is the resident form whisper.cpp actually ships (it is built by
the same Vulkan build in scripts/build-whisper-vulkan.ps1), so the model is
loaded once at app start and stays in VRAM until the app exits.

This class owns that process end to end: it starts it, waits for /health, warms
it, restarts it if it dies, and shuts it down on exit.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from ..audio import wav
from ..config import WhisperConfig
from ..errors import BackendUnavailableError, TranscriptionError
from .process import ManagedProcess
from .whisper_server import WhisperServerClient

log = logging.getLogger(__name__)


class WhisperVulkanBackend:
    """Implements `engines.base.BatchTranscriber`."""

    def __init__(self, cfg: WhisperConfig, *, resolve=Path) -> None:
        self.cfg = cfg
        self.exe = Path(resolve(cfg.server_exe))
        self.model = Path(resolve(cfg.model))
        self.client = WhisperServerClient(
            cfg.host, cfg.port,
            request_timeout_s=cfg.request_timeout_s,
            language=cfg.language,
        )
        self._proc = ManagedProcess(
            self._argv(),
            name="whisper-server",
            health_check=self.client.is_healthy,
            startup_timeout_s=cfg.startup_timeout_s,
            max_restarts=cfg.max_restarts,
        )
        self._started = False

    # -- lifecycle -------------------------------------------------------

    def _argv(self) -> list[str]:
        argv = [
            str(self.exe),
            "--model", str(self.model),
            "--host", self.cfg.host,
            "--port", str(self.cfg.port),
            "--threads", str(self.cfg.threads),
            "--language", self.cfg.language,
            "--beam-size", str(self.cfg.beam_size),
            "--best-of", str(self.cfg.best_of),
            "--no-timestamps",
        ]
        if not self.cfg.use_gpu:
            argv.append("--no-gpu")
        if self.cfg.flash_attn:
            # Off by default: whisper.cpp #3806 is whisper-server crashing inside
            # amdvlk64.dll with flash attention on an AMD card.
            argv.append("--flash-attn")
        argv.extend(self.cfg.extra_args)
        return argv

    def preflight(self) -> None:
        """Check the things the user can actually fix, before spawning anything."""
        if not self.exe.exists():
            raise BackendUnavailableError(
                f"whisper-server was not found at {self.exe}",
                "Build it with scripts/build-whisper-vulkan.ps1, then set "
                "[whisper] server_exe in your config to the path it prints.",
            )
        if not self.model.exists():
            raise BackendUnavailableError(
                f"The Whisper model was not found at {self.model}",
                "Download it with scripts/fetch-models.ps1, then set "
                "[whisper] model in your config to that path.",
            )
        size = self.model.stat().st_size
        if size < 100 * 1024 * 1024:
            raise BackendUnavailableError(
                f"The Whisper model at {self.model} is only "
                f"{size / 1024 / 1024:.0f} MB, which is too small to be a real model.",
                "The download was probably interrupted. Delete the file and "
                "re-run scripts/fetch-models.ps1.",
            )
        if self.client.port_is_open():
            raise BackendUnavailableError(
                f"Something is already listening on {self.cfg.host}:{self.cfg.port}.",
                "Another copy of dictate may already be running. Close it, or "
                "change [whisper] port in your config.",
            )

    def start(self) -> None:
        if self._started and self._proc.is_running():
            return
        self.preflight()
        log.info("starting whisper-server: %s", " ".join(self._argv()))
        t0 = time.monotonic()
        self._proc.start()
        self._started = True
        log.info("whisper-server ready in %.1fs", time.monotonic() - t0)
        self._log_backend_choice()
        if self.cfg.warmup:
            self._warmup()

    def _log_backend_choice(self) -> None:
        """Surface which ggml backend whisper.cpp actually picked.

        This is the single most useful line for the product owner: if it does not
        mention Vulkan, the GPU is not being used and everything will be slow.
        """
        tail = self._proc.log_tail(200)
        picked = [ln for ln in tail.splitlines()
                  if "ggml_vulkan" in ln or "backend" in ln.lower()]
        if picked:
            for line in picked[:6]:
                log.info("whisper.cpp: %s", line)
        if "vulkan" not in tail.lower() and self.cfg.use_gpu:
            log.warning(
                "whisper-server did not mention Vulkan at startup. It may be "
                "running on the CPU, which will be far slower than the 2-4 second "
                "budget. Run `dictate doctor` for the checks to try."
            )

    def _warmup(self) -> None:
        """One second of silence through the whole path, so the first real
        utterance does not pay for lazy GPU buffer allocation or shader compile."""
        try:
            t0 = time.monotonic()
            self.client.transcribe_wav(wav.pcm16_to_wav(wav.silence(1.0)))
            log.info("whisper-server warmed up in %.2fs", time.monotonic() - t0)
        except TranscriptionError as exc:
            # Not fatal - the server said it was healthy, so let the first real
            # utterance decide. But say so, because it is a useful early warning.
            log.warning("whisper-server warm-up failed: %s", exc.message)

    def stop(self) -> None:
        self._started = False
        self._proc.stop()

    def is_healthy(self) -> bool:
        return self._proc.is_running() and self.client.is_healthy()

    @property
    def describe(self) -> str:
        where = "GPU (Vulkan)" if self.cfg.use_gpu else "CPU"
        return f"whisper.cpp server on {where}, model {self.model.name}"

    # -- transcription ---------------------------------------------------

    def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        if not pcm:
            return ""
        if not self._proc.is_running():
            reason = self._proc.gave_up_reason
            if reason:
                raise BackendUnavailableError(
                    reason,
                    "dictate stopped trying to restart whisper-server. Fix the "
                    "problem above, then restart dictate.",
                )
            raise TranscriptionError(
                "whisper-server is not running right now.",
                "It is being restarted. Try again in a couple of seconds.",
            )
        audio = wav.pcm16_to_wav(pcm, sample_rate=sample_rate)
        t0 = time.monotonic()
        text = self.client.transcribe_wav(
            audio, beam_size=self.cfg.beam_size, best_of=self.cfg.best_of
        )
        elapsed = time.monotonic() - t0
        duration = len(pcm) / 2 / sample_rate
        log.info("transcribed %.1fs of audio in %.2fs (%.2fx real time)",
                 duration, elapsed, (duration / elapsed) if elapsed else 0.0)
        return text
