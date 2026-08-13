"""Live captions: the streaming Zipformer, on the CPU, via sherpa-onnx.

Everything this produces is DISPLAY ONLY. It is drawn on the caption overlay
while the hotkey is held and thrown away the moment it is released - it never
reaches the clipboard, the keyboard, or the document. That is what makes a fast,
ALL-CAPS, unpunctuated model acceptable here (docs/DESIGN.md, decision 4).

Threads: 2. The prior measurements found this model gets *worse* with more - six
threads was three times slower than two, because the per-chunk work is tiny and
thread synchronisation dominates. `config.validate()` refuses to raise it.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..audio import wav
from ..config import CaptionConfig, Config
from ..errors import MissingDependencyError

log = logging.getLogger(__name__)


def _load_numpy():
    try:
        import numpy  # noqa: PLC0415 - optional, only on the Windows install
        return numpy
    except ImportError:
        return None


class SherpaSession:
    """One utterance's decode. Implements `engines.base.StreamingSession`."""

    def __init__(self, recognizer, sample_rate: int) -> None:
        self._recognizer = recognizer
        self._stream = recognizer.create_stream()
        self._sample_rate = sample_rate
        self._np = _load_numpy()
        self._closed = False
        self._text = ""

    def accept(self, pcm: bytes) -> None:
        if self._closed or not pcm:
            return
        samples = wav.pcm16_to_float32(pcm)
        if self._np is not None:
            samples = self._np.asarray(samples, dtype=self._np.float32)
        self._stream.accept_waveform(self._sample_rate, samples)
        while self._recognizer.is_ready(self._stream):
            self._recognizer.decode_stream(self._stream)
        self._text = self._result()

    def _result(self) -> str:
        result = self._recognizer.get_result(self._stream)
        # sherpa-onnx returns a plain string from get_result(); some versions
        # return a result object with a .text attribute.
        return (result if isinstance(result, str) else getattr(result, "text", "")).strip()

    def text(self) -> str:
        return self._text

    def close(self) -> None:
        # Deliberately drop the text as well as the decoder state: the caption
        # is discarded on release, and this is where that actually happens.
        self._closed = True
        self._text = ""
        self._stream = None


class SherpaStreamingTranscriber:
    """Implements `engines.base.StreamingTranscriber`."""

    def __init__(self, cfg: CaptionConfig, paths: dict[str, Path], sample_rate: int,
                 *, recognizer=None) -> None:
        self.cfg = cfg
        self.paths = paths
        self.sample_rate = sample_rate
        self._recognizer = recognizer

    @classmethod
    def from_config(cls, cfg: Config, *, recognizer=None) -> "SherpaStreamingTranscriber":
        return cls(cfg.captions, cfg.caption_paths, cfg.audio.sample_rate,
                   recognizer=recognizer)

    def preflight(self) -> None:
        missing = [f"{k}: {v}" for k, v in self.paths.items() if not v.exists()]
        if missing:
            raise MissingDependencyError(
                "The live-caption model files were not found:\n  "
                + "\n  ".join(missing),
                "Run scripts/fetch-models.ps1 to download the streaming Zipformer, "
                "then set [captions] model_dir in your config to the folder it "
                "created. Or set [captions] enabled = false to run without live "
                "captions - the pasted text is unaffected either way.",
            )

    def _build(self):
        if self._recognizer is not None:
            return self._recognizer
        self.preflight()
        try:
            import sherpa_onnx  # noqa: PLC0415 - optional dependency
        except ImportError as exc:
            raise MissingDependencyError(
                "The sherpa-onnx package is not installed, so live captions "
                "cannot run.",
                "Install it with: pip install sherpa-onnx\n"
                "Or set [captions] enabled = false in your config to run "
                "without live captions.",
            ) from exc
        log.info("loading streaming Zipformer from %s", self.cfg.model_dir)
        self._recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=str(self.paths["tokens"]),
            encoder=str(self.paths["encoder"]),
            decoder=str(self.paths["decoder"]),
            joiner=str(self.paths["joiner"]),
            num_threads=self.cfg.num_threads,
            sample_rate=self.sample_rate,
            feature_dim=80,
            decoding_method=self.cfg.decoding_method,
            provider=self.cfg.provider,
            # The hotkey defines the utterance boundary, so the model must not
            # decide one for us mid-sentence.
            enable_endpoint_detection=False,
        )
        return self._recognizer

    def start_session(self) -> SherpaSession:
        return SherpaSession(self._build(), self.sample_rate)

    @property
    def describe(self) -> str:
        return (f"sherpa-onnx streaming Zipformer ({Path(self.cfg.model_dir).name}), "
                f"{self.cfg.num_threads} CPU threads")

    def close(self) -> None:
        self._recognizer = None
