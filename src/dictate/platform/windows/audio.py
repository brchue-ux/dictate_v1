"""Microphone capture.

16 kHz mono 16-bit, because that is what both models want and asking PortAudio
for it directly avoids resampling in our own code. `RawInputStream` hands back
raw PCM bytes, which is exactly the format the utterance buffer, the WAV writer
and sherpa-onnx all take, so nothing is converted on the hot path.

The stream is opened once and started/stopped around each utterance rather than
opened per utterance: opening a PortAudio stream takes tens of milliseconds and
the user is already talking by then.
"""

from __future__ import annotations

import logging

from ...errors import DictateError, MissingDependencyError
from ..base import AudioCallback, AudioLossCallback

log = logging.getLogger(__name__)

#: How often to repeat the "audio is being discarded" warning in the log once it
#: is clearly not a one-off. The old ladder was 1st, 10th, 100th and then silence
#: for ever, which is exactly the shape that makes a log stop being evidence.
#: Whoever is listening (`Pipeline.note_input_loss`) gets every single one; this
#: is only about not writing a line per 32 ms block into the file.
LOSS_LOG_EVERY = 100


def _load_sounddevice():
    try:
        import sounddevice  # noqa: PLC0415 - optional, Windows install only
        return sounddevice
    except ImportError as exc:
        raise MissingDependencyError(
            "The sounddevice package is not installed, so dictate cannot use "
            "your microphone.",
            "Install it with: pip install sounddevice",
        ) from exc
    except OSError as exc:
        # sounddevice raises OSError when the PortAudio DLL is missing.
        raise MissingDependencyError(
            f"sounddevice is installed but its audio library would not load: {exc}",
            "Reinstall it with: pip install --force-reinstall sounddevice",
        ) from exc


def list_input_devices() -> list[tuple[int, str, int]]:
    """(index, name, max input channels) for every device that can record."""
    sd = _load_sounddevice()
    out = []
    for i, dev in enumerate(sd.query_devices()):
        if dev.get("max_input_channels", 0) > 0:
            out.append((i, dev["name"], dev["max_input_channels"]))
    return out


class SoundDeviceCapture:
    """Implements `platform.base.AudioCapture`."""

    def __init__(self, *, sample_rate: int = 16000, channels: int = 1,
                 block_frames: int = 512, device: str = "") -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.block_frames = block_frames
        self.device = device or None
        self._sd = None
        self._stream = None
        self._callback: AudioCallback | None = None
        self._on_loss: AudioLossCallback | None = None
        self._running = False
        #: Callbacks that arrived with a status flag set, since this process
        #: started. Only the log uses it - what he is told comes from
        #: `Pipeline`, per utterance, which is the unit he can act on.
        self.dropped = 0

    @property
    def describe(self) -> str:
        name = self.device or "system default input"
        return f"{name} at {self.sample_rate} Hz mono"

    def _resolve_device(self):
        """Accept a device index, an exact name, or a unique substring."""
        if self.device is None:
            return None
        raw = self.device.strip()
        if raw.isdigit():
            return int(raw)
        devices = list_input_devices()
        exact = [i for i, name, _ in devices if name == raw]
        if exact:
            return exact[0]
        partial = [(i, name) for i, name, _ in devices if raw.lower() in name.lower()]
        if len(partial) == 1:
            log.info("matched [audio] device %r to %s", raw, partial[0][1])
            return partial[0][0]
        available = "\n  ".join(f"{i}: {name}" for i, name, _ in devices) or "(none found)"
        if not partial:
            raise DictateError(
                f"No microphone matches [audio] device = {raw!r}.",
                f"Your input devices are:\n  {available}\n"
                f"Put one of those names (or its number) in the config, or leave "
                f"device = \"\" to use the Windows default.",
            )
        raise DictateError(
            f"[audio] device = {raw!r} matches more than one microphone: "
            f"{', '.join(n for _, n in partial)}.",
            "Use the full name or the device number instead.",
        )

    def _open(self) -> None:
        sd = self._sd = _load_sounddevice()
        device = self._resolve_device()

        def on_block(indata, _frames, _time, status) -> None:
            if status:
                # NOT benign, and not about the captions: PortAudio discarded
                # input audio before this callback ran, so the recording this
                # block belongs to has a hole in it and the pasted text is made
                # from the same holed recording. Tell whoever is listening
                # about every one, so it can be attributed to the utterance it
                # happened in instead of being a process-wide number nobody can
                # place.
                self.dropped += 1
                if self.dropped == 1 or self.dropped % LOSS_LOG_EVERY == 0:
                    log.warning("audio input status: %s (%d since start)",
                                status, self.dropped)
                # Only an OVERFLOW means samples were discarded. Any other flag
                # is worth the log line above and nothing more - telling him a
                # word may be missing when nothing was thrown away is how a
                # warning stops being believed. The default is True so an
                # unfamiliar sounddevice build errs towards reporting.
                if bool(getattr(status, "input_overflow", True)):
                    loss = self._on_loss
                    if loss is not None:
                        try:
                            loss(1)
                        except Exception:
                            log.exception("the audio-loss listener failed")
            cb = self._callback
            if cb is not None:
                try:
                    cb(bytes(indata))
                except Exception:
                    log.exception("audio callback failed")

        try:
            self._stream = sd.RawInputStream(
                samplerate=self.sample_rate,
                blocksize=self.block_frames,
                device=device,
                channels=self.channels,
                dtype="int16",
                callback=on_block,
            )
        except Exception as exc:
            raise DictateError(
                f"Could not open the microphone: {exc}",
                "Check that a microphone is plugged in, and that Windows "
                "Settings > Privacy & security > Microphone allows desktop apps "
                "to use it. `dictate devices` lists what dictate can see.",
            ) from exc

    def start(self, callback: AudioCallback,
              on_loss: AudioLossCallback | None = None) -> None:
        self._callback = callback
        self._on_loss = on_loss
        if self._stream is None:
            self._open()
        if not self._running:
            self._stream.start()
            self._running = True

    def stop(self) -> None:
        self._callback = None
        self._on_loss = None
        if self._stream is not None and self._running:
            try:
                self._stream.stop()
            except Exception:
                log.debug("stopping the audio stream raised", exc_info=True)
            self._running = False

    def close(self) -> None:
        self.stop()
        if self._stream is not None:
            try:
                self._stream.close()
            except Exception:
                log.debug("closing the audio stream raised", exc_info=True)
            self._stream = None
