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

    # The streaming Zipformer behind the live captions. GitHub publishes no
    # checksum for this release asset (it predates the digest field on their
    # API), so this SHA-256 was measured by downloading the file and hashing it
    # on 2026-08-13.
    Captions = @{
        Name   = 'sherpa-onnx-streaming-zipformer-en-2023-06-26'
        Uri    = 'https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-streaming-zipformer-en-2023-06-26.tar.bz2'
        Bytes  = 310414022
        Sha256 = '639e25b578e9e997131402199419c13a941f8e4e198e2da1ce57dbf5cf401282'
        Label  = 'Live-caption model'
        # Checked after unpacking: these are the names config/dictate.example.toml
        # expects under [captions].
        Files  = @(
            'tokens.txt',
            'encoder-epoch-99-avg-1-chunk-16-left-128.int8.onnx',
            'decoder-epoch-99-avg-1-chunk-16-left-128.onnx',
            'joiner-epoch-99-avg-1-chunk-16-left-128.int8.onnx'
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
