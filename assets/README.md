# assets

## `jfk.wav`

The test clip. Eleven seconds of John F. Kennedy's 1961 inaugural address —
"ask not what your country can do for you…" — as 16 kHz mono 16-bit PCM, which
is exactly the format both models want, so nothing has to convert it.

    352,078 bytes
    sha256 59dfb9a4acb36fe2a2affc14bacbee2920ff435cb13cc314a08c13f66ba7860e

Taken from [whisper.cpp](https://github.com/ggml-org/whisper.cpp)'s
`samples/jfk.wav`, where it is the standard clip every benchmark in that project
uses — including the ones in the GPU research this build was sized against, so
timings here can be compared with those directly. whisper.cpp is MIT-licensed;
the recording itself is a work of the United States federal government and is in
the public domain.

It is committed here rather than downloaded because two things depend on it
being present and being exactly this file:

* **Step 6 of `setup.ps1`** puts it through the whole transcription path and
  checks the word "country" comes back. A test that has to download something
  first is a test that fails when the network does.
* **The Windows CI build** uses the same clip, so "the binary transcribes this
  correctly" means the same thing here and on the product owner's PC.
