<#
.SYNOPSIS
    Downloads the two models dictate needs.

.DESCRIPTION
    This is one step of the full installer, kept as its own command because it
    is the one you re-run if a download was interrupted. It runs exactly the
    same code as `setup.ps1`, so a download that stopped half way carries on
    from where it stopped rather than starting again, and both files are checked
    against a published fingerprint before they are used.

    For a first-time install, run setup.ps1 instead. It does this and the other
    five steps in order.

    1. Whisper large-v3-turbo, f16 (1.62 GB) - the transcription that actually
       gets pasted. Runs on the GPU through the Vulkan build.
    2. The sherpa-onnx streaming caption model (98 MB) - the words that appear
       on screen while you are still talking. This one is display-only; its text
       is thrown away when you let go of the hotkey.

    The caption model CHANGED on 2026-08-13, from a LibriSpeech-trained
    Zipformer to NVIDIA's streaming FastConformer, because the old one got whole
    phrases wrong on ordinary speech. This script downloads it, but it does not
    touch your config - that is the install step. To do both:

        powershell -ExecutionPolicy Bypass -File setup.ps1 -Only models,install

    which points [captions] at the new folder AND at the new file names inside
    it; the names changed with the model, so the folder alone is not enough. The
    old folder is left where it is - delete it yourself once you are happy.

.PARAMETER Root
    Where to put them. Default C:\dictate-gpu\models. A path ending in \models
    is understood, so the old habit still works.

.PARAMETER SkipCaptions
    Only fetch the Whisper model. dictate runs without live captions; you just
    do not see words while speaking.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\fetch-models.ps1
#>

[CmdletBinding()]
param(
    [string]$Root = 'C:\dictate-gpu\models',
    [switch]$SkipCaptions
)

$ErrorActionPreference = 'Stop'

# setup.ps1 works in terms of the top folder and puts models in a `models`
# subfolder of it. This script has always taken the models folder itself, so
# accept either.
if ($Root -match '[\\/]models[\\/]*$') {
    $Root = Split-Path -Parent ($Root.TrimEnd('\', '/'))
}

& (Join-Path (Split-Path -Parent $PSScriptRoot) 'setup.ps1') `
    -Root $Root -Only models -SkipCaptions:$SkipCaptions
exit $LASTEXITCODE
