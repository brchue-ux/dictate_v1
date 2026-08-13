"""Put a WAV file through the live-caption engine, with no screen involved.

This is a CI check, not part of the product. It exists because the live-caption
path is the one piece of dictate that needs a real third-party model loaded into
a real process, and until this ran, nobody had ever loaded sherpa-onnx at all -
the adapter was written from its documented API.

What it proves when it passes on a Windows runner:

  * sherpa-onnx installs and loads on Windows
  * the streaming Zipformer files the setup downloads are the ones the adapter
    asks for, by name
  * feeding it audio in the same 32 ms blocks the app uses produces words
  * closing the session really does destroy the caption text, which is the
    guarantee that caption text never reaches the document

What it does NOT prove: anything about the on-screen overlay, which needs a
desktop, or anything about the GPU - this model runs on the CPU by design.

    python scripts/ci/caption-smoke.py <config.toml> <clip.wav> <expected word>
"""

from __future__ import annotations

import sys
import wave
from pathlib import Path

from dictate import config as config_mod
from dictate.engines.sherpa_stream import SherpaStreamingTranscriber


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    config_path, wav_path, expected = Path(argv[0]), Path(argv[1]), argv[2]

    cfg = config_mod.load(config_path)
    with wave.open(str(wav_path)) as clip:
        if (clip.getframerate(), clip.getnchannels(), clip.getsampwidth()) != (16000, 1, 2):
            print(f"FAIL: {wav_path} must be 16 kHz mono 16-bit")
            return 1
        pcm = clip.readframes(clip.getnframes())

    transcriber = SherpaStreamingTranscriber.from_config(cfg)
    print(f"engine: {transcriber.describe}")
    session = transcriber.start_session()

    # The same block size the app feeds it: [audio] block_ms of 16 kHz mono.
    block = int(cfg.audio.sample_rate * cfg.audio.block_ms / 1000) * 2
    for offset in range(0, len(pcm), block):
        session.accept(pcm[offset:offset + block])

    text = session.text()
    print(f"caption text: {text!r}")

    session.close()
    if session.text() != "":
        print("FAIL: closing the session left the caption text behind. It is "
              "supposed to be destroyed on release - that is what keeps it out "
              "of the document.")
        return 1
    print("ok: the caption text was destroyed when the session closed")

    if expected.upper() not in text.upper():
        print(f"FAIL: expected to hear {expected!r} in the caption text")
        return 1
    print(f"ok: heard {expected!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
