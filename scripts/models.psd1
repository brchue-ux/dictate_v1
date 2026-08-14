<#
    Every file the setup downloads, pinned by size AND by SHA-256.

    One file so there is one place to change them, read by setup.ps1 and by the
    Windows CI workflow. A wrong number here does not fail politely three steps
    later - it fails immediately, at the download, with "this is not the file it
    should be", which is the whole point of pinning them.

    Provenance of each fingerprint is written next to it. None of them are
    guesses.
#>
@{
    # The transcription that actually gets pasted.
    #
    # Size and SHA-256 both come from Hugging Face's own metadata for the file:
    # an LFS object id IS the SHA-256 of the contents, read from
    # https://huggingface.co/api/models/ggerganov/whisper.cpp/tree/main on
    # 2026-08-13, and cross-checked by hashing a smaller file from the same
    # repository. The byte count matches the one measured independently in the
    # GPU research report.
    Whisper  = @{
        Name   = 'ggml-large-v3-turbo.bin'
        Uri    = 'https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin'
        Bytes  = 1624555275
        Sha256 = '1fc70f774d38eb169993ac391eea357ef47c88757ef72ee5943879b7e8e2bc69'
        Label  = 'Whisper model'
    }

    # The words that appear on screen while he is still speaking.
    #
    # CHANGED 2026-08-13. This was sherpa-onnx-streaming-zipformer-en-2023-06-26
    # (310 MB), trained on LibriSpeech alone - read audiobooks, transcribed in
    # upper case with no punctuation. Dictation is spontaneous speech and it
    # showed: on assets/jfk.wav that model printed "AND SAW MY FELLOW AMERICANS
    # ASK NOT WHAT'S YOUR COUNTRY CAN DO FOR YOU AS BUT YOU CAN DO FOR YOUR
    # COUNT", and it got worse with noise. Do not put it back without measuring
    # the replacement first - `dictate captions <clip.wav>` is that measurement.
    #
    # This is NVIDIA's cache-aware streaming FastConformer (NeMo
    # stt_en_fastconformer_hybrid_large_streaming_80ms), int8, exported to ONNX
    # by the sherpa-onnx project. English only, CC-BY-4.0, and its training set
    # includes Fisher and Switchboard - conversational telephone speech - which
    # is why it survives ordinary talking. Size and SHA-256 are GitHub's own
    # published digest for the release asset, read from
    # https://api.github.com/repos/k2-fsa/sherpa-onnx/releases/tags/asr-models
    # on 2026-08-13 and cross-checked by hashing the downloaded file.
    Captions = @{
        Name    = 'sherpa-onnx-nemo-streaming-fast-conformer-transducer-en-80ms-int8'
        Uri     = 'https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-nemo-streaming-fast-conformer-transducer-en-80ms-int8.tar.bz2'
        Bytes   = 102813625
        Sha256  = '7bd33a914e93370a1ba9c2066d9e841bdcad8613fa2a00537c1ae15d851a14d8'
        Label   = 'Live-caption model'
        # The four names setup.ps1 writes into [captions]. They are here rather
        # than only in the config template because the file names inside the
        # folder changed with the model, and a config that keeps the old names
        # beside the new folder finds nothing and turns captions off.
        Encoder = 'encoder.int8.onnx'
        Decoder = 'decoder.int8.onnx'
        Joiner  = 'joiner.int8.onnx'
        Tokens  = 'tokens.txt'
        # Checked after unpacking: these are the names config/dictate.example.toml
        # expects under [captions].
        Files   = @(
            'tokens.txt',
            'encoder.int8.onnx',
            'decoder.int8.onnx',
            'joiner.int8.onnx'
        )
    }

    # NOT used by the install. This is the small model the Windows CI job puts
    # through the binary it just compiled, to prove it really transcribes -
    # large-v3-turbo would be 1.6 GB on every CI run for no extra proof.
    # base.en is used rather than tiny.en because dictate refuses a model file
    # under 100 MB as "too small to be a real model".
    # Same provenance as the Whisper entry above.
    CiSmoke  = @{
        Name   = 'ggml-base.en.bin'
        Uri    = 'https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin'
        Bytes  = 147964211
        Sha256 = 'a03779c86df3323075f5e796cb2ce5029f00ec8869eee3fdfb897afe36c6d002'
        Label  = 'CI smoke-test model'
    }
}
