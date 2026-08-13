<#
.SYNOPSIS
    Downloads the two models dictate needs.

.DESCRIPTION
    1. Whisper large-v3-turbo, f16 (1.62 GB) - the transcription that actually
       gets pasted. Runs on the GPU through the Vulkan build.
    2. The sherpa-onnx streaming Zipformer (310 MB) - the words that appear on
       screen while you are still talking. This one is display-only; its text is
       thrown away when you let go of the hotkey.

    Both sizes are checked after downloading, because a truncated download of a
    1.6 GB file is the failure that looks like "the app is broken" rather than
    like a download problem.

.PARAMETER Root
    Where to put them. Default C:\dictate-gpu\models.

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
$ProgressPreference = 'SilentlyContinue'   # makes Invoke-WebRequest far faster

function Write-Step { param($m) Write-Host "`n=== $m" -ForegroundColor Cyan }
function Write-Good { param($m) Write-Host "  OK  $m" -ForegroundColor Green }
function Write-Warn { param($m) Write-Host "  !!  $m" -ForegroundColor Yellow }

# The exact byte count of ggml-large-v3-turbo.bin, measured when the GPU route
# was researched. A different number means a truncated or different file.
$WhisperUrl   = 'https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin'
$WhisperBytes = 1624555275

$CaptionUrl  = 'https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-streaming-zipformer-en-2023-06-26.tar.bz2'
$CaptionDir  = 'sherpa-onnx-streaming-zipformer-en-2023-06-26'

New-Item -ItemType Directory -Force -Path $Root | Out-Null
Set-Location $Root
Write-Host "Downloading into: $Root"

# ---------------------------------------------------------------------------
Write-Step '1/2  Whisper large-v3-turbo (1.62 GB)'

$whisperPath = Join-Path $Root 'ggml-large-v3-turbo.bin'
if ((Test-Path $whisperPath) -and ((Get-Item $whisperPath).Length -eq $WhisperBytes)) {
    Write-Good 'Already downloaded and the size is exactly right.'
} else {
    if (Test-Path $whisperPath) {
        Write-Warn 'A file is there but the size is wrong. Downloading it again.'
        Remove-Item $whisperPath -Force
    }
    Write-Host '  this is 1.6 GB and will take a few minutes...'
    Invoke-WebRequest -Uri $WhisperUrl -OutFile $whisperPath
    $got = (Get-Item $whisperPath).Length
    if ($got -ne $WhisperBytes) {
        Write-Warn "Downloaded $got bytes, expected $WhisperBytes."
        Write-Warn 'That means the download was cut short. Delete the file and run'
        Write-Warn 'this script again:'
        Write-Warn "  Remove-Item '$whisperPath'"
        exit 1
    }
    Write-Good "$whisperPath  ($([math]::Round($got / 1GB, 2)) GB)"
}

# ---------------------------------------------------------------------------
Write-Step '2/2  Live-caption model (310 MB)'

if ($SkipCaptions) {
    Write-Warn 'Skipped at your request. Set [captions] enabled = false in your config.'
} else {
    $captionPath = Join-Path $Root $CaptionDir
    if (Test-Path (Join-Path $captionPath 'tokens.txt')) {
        Write-Good 'Already downloaded.'
    } else {
        $archive = Join-Path $Root 'caption-model.tar.bz2'
        Write-Host '  downloading...'
        Invoke-WebRequest -Uri $CaptionUrl -OutFile $archive
        Write-Host '  unpacking...'
        tar -xf $archive -C $Root
        Remove-Item $archive -Force
        if (-not (Test-Path (Join-Path $captionPath 'tokens.txt'))) {
            Write-Warn "Unpacked, but $captionPath\tokens.txt is not there."
            Write-Warn "List what did appear with:  Get-ChildItem '$Root'"
            exit 1
        }
    }
    Get-ChildItem $captionPath -Filter *.onnx | ForEach-Object {
        Write-Good ('{0,-58} {1,6:N0} MB' -f $_.Name, ($_.Length / 1MB))
    }
}

# ---------------------------------------------------------------------------
Write-Host "`nDone." -ForegroundColor Green
Write-Host ''
Write-Host 'Put these into your dictate config:'
Write-Host ''
Write-Host '  [whisper]'
Write-Host "  model = '$whisperPath'" -ForegroundColor White
if (-not $SkipCaptions) {
    Write-Host ''
    Write-Host '  [captions]'
    Write-Host "  model_dir = '$(Join-Path $Root $CaptionDir)'" -ForegroundColor White
}
Write-Host ''
Write-Host 'Then:  dictate doctor'
