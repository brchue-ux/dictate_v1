<#
.SYNOPSIS
    Builds whisper.cpp with the Vulkan backend from source, on Windows.

.DESCRIPTION
    This is Stage V of data/dictate-gpu-batch/report.md, automated.

    Why from source rather than a download: whisper.cpp publishes no official
    Windows Vulkan binary (issues #3673 and #3691, both still open), so the
    alternatives were a stranger's zip or this. Building it yourself is fully
    trusted, it takes about twenty minutes once, it also produces
    parakeet-cli.exe, and - the part that matters later - it means you can roll
    back to an earlier commit if a Vulkan regression ever ships. That happens:
    llama.cpp #22992 was a gfx1030-specific Vulkan regression, fixed in two days.

    Why Vulkan rather than ROCm: AMD ships no gfx1030 matrix-multiply kernels in
    any current ROCm-for-Windows release, so the ROCm route cannot work on an
    RX 6900 XT. Run scripts/check-rocm-gfx1030.py if you want to see that for
    yourself - it takes 30 seconds and downloads nothing.

.PARAMETER Root
    Where to put everything. Default C:\dictate-gpu.

.PARAMETER Ref
    A whisper.cpp tag or commit to build. Default: the default branch tip.
    Pass a known-good commit here if a regression ever lands.

.PARAMETER SkipTools
    Do not run winget. Use if you already have CMake, Git, the Vulkan SDK and
    the MSVC build tools.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\build-whisper-vulkan.ps1

.EXAMPLE
    # Pin to a specific release if a later one regresses on your card:
    powershell -ExecutionPolicy Bypass -File scripts\build-whisper-vulkan.ps1 -Ref v1.9.2
#>

[CmdletBinding()]
param(
    [string]$Root = 'C:\dictate-gpu',
    [string]$Ref = '',
    [switch]$SkipTools
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Write-Step   { param($m) Write-Host "`n=== $m" -ForegroundColor Cyan }
function Write-Good   { param($m) Write-Host "  OK  $m" -ForegroundColor Green }
function Write-Warn   { param($m) Write-Host "  !!  $m" -ForegroundColor Yellow }
function Write-Bad    { param($m) Write-Host "  XX  $m" -ForegroundColor Red }

function Fail {
    param($Message, $WhatToDo)
    Write-Bad $Message
    if ($WhatToDo) { Write-Host "`n      What to do:`n      $WhatToDo" -ForegroundColor Yellow }
    exit 1
}

Write-Host "whisper.cpp + Vulkan build" -ForegroundColor White
Write-Host "Working folder: $Root"

# ---------------------------------------------------------------------------
Write-Step '1/6  Working folder'
New-Item -ItemType Directory -Force -Path $Root | Out-Null
Set-Location $Root
Write-Good $Root

# ---------------------------------------------------------------------------
Write-Step '2/6  Build tools'
if ($SkipTools) {
    Write-Warn 'Skipped at your request.'
} else {
    $packages = @(
        @{ Id = 'Kitware.CMake';  Name = 'CMake' },
        @{ Id = 'Git.Git';        Name = 'Git' },
        @{ Id = 'LunarG.VulkanSDK'; Name = 'Vulkan SDK' }
    )
    foreach ($p in $packages) {
        Write-Host "  installing $($p.Name)..."
        winget install --id $p.Id --accept-source-agreements --accept-package-agreements 2>&1 |
            Out-String | Write-Verbose
    }
    Write-Host '  installing Visual Studio 2022 Build Tools (this is the slow one)...'
    winget install --id Microsoft.VisualStudio.2022.BuildTools `
        --accept-source-agreements --accept-package-agreements `
        --override '--quiet --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended' 2>&1 |
        Out-String | Write-Verbose
    Write-Good 'Tool installation finished.'
    Write-Warn 'If this is the first time you have installed these, CLOSE THIS WINDOW,'
    Write-Warn 'open a new PowerShell, and run this script again. The new tools only'
    Write-Warn 'appear on your PATH in a fresh window.'
}

# ---------------------------------------------------------------------------
Write-Step '3/6  Checking the tools are really there'

foreach ($tool in @('cmake', 'git')) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        Fail "$tool is not on your PATH." @"
Close this PowerShell window, open a new one, and run this script again.
If it still says this, install it by hand:
  winget install --id $(if ($tool -eq 'cmake') { 'Kitware.CMake' } else { 'Git.Git' })
"@
    }
}
Write-Good "cmake  $((cmake --version | Select-Object -First 1))"
Write-Good "git    $((git --version))"

if (-not $env:VULKAN_SDK) {
    Fail 'VULKAN_SDK is not set, so cmake will not find Vulkan.' @"
The SDK installed but its environment variable has not reached this session.
REBOOT (a new PowerShell window is often not enough for this one), then run
this script again.
"@
}
Write-Good "VULKAN_SDK = $env:VULKAN_SDK"

# ---------------------------------------------------------------------------
Write-Step '4/6  Can Vulkan see your graphics card?'

$vulkanInfo = Join-Path $env:VULKAN_SDK 'Bin\vulkaninfoSDK.exe'
if (Test-Path $vulkanInfo) {
    $summary = & $vulkanInfo --summary 2>&1 | Out-String
    $devices = $summary -split "`n" | Where-Object { $_ -match 'deviceName' }
    if ($devices) {
        foreach ($d in $devices) { Write-Good $d.Trim() }
        if ($summary -notmatch 'AMD|Radeon|NVIDIA|Intel') {
            Write-Warn 'No recognisable GPU in that list. The build will still work,'
            Write-Warn 'but whisper.cpp may fall back to the CPU when it runs.'
        }
    } else {
        Fail 'Vulkan reported no devices at all.' @"
This is a graphics driver problem, not a whisper.cpp problem, and building
now would not help. Update AMD Adrenalin from the AMD Software app (or from
amd.com), reboot, and run this script again.
"@
    }
} else {
    Write-Warn "vulkaninfoSDK.exe not found under $env:VULKAN_SDK - continuing anyway."
}

# ---------------------------------------------------------------------------
Write-Step '5/6  Building whisper.cpp'

$src = Join-Path $Root 'whisper.cpp'
if (Test-Path $src) {
    Write-Host '  repository already here; updating it'
    Push-Location $src
    git fetch --tags --depth 1 origin 2>&1 | Out-String | Write-Verbose
    Pop-Location
} else {
    Write-Host '  cloning ggml-org/whisper.cpp'
    git clone --depth 1 https://github.com/ggml-org/whisper.cpp $src
}

Push-Location $src
try {
    if ($Ref) {
        Write-Host "  checking out $Ref"
        git fetch --depth 1 origin $Ref 2>&1 | Out-String | Write-Verbose
        git checkout $Ref
    }
    $commit = (git rev-parse --short HEAD)
    Write-Good "building commit $commit"
    Write-Host '  WRITE THIS DOWN. If a later build ever produces gibberish, come'
    Write-Host "  back to it with:  -Ref $commit"

    # A stale build directory is the usual cause of "it lost the Vulkan flag".
    if (Test-Path 'build') {
        Write-Host '  removing the previous build directory'
        Remove-Item -Recurse -Force 'build'
    }

    Write-Host '  cmake configure (GGML_VULKAN=1)'
    cmake -B build -DGGML_VULKAN=1
    if ($LASTEXITCODE -ne 0) {
        Fail 'cmake could not configure the build.' @"
If it said "Could not find Vulkan": VULKAN_SDK was not visible to cmake.
Reboot, then run this script again.
Anything else: the message above is from cmake itself and names what is missing.
"@
    }

    Write-Host '  compiling (5-15 minutes; warnings scrolling past are normal)'
    cmake --build build -j --config Release
    if ($LASTEXITCODE -ne 0) {
        Fail 'The compile failed.' @"
Scroll up to the first line containing the word "error" - that is the real one,
and everything after it is noise. The usual cause is the Visual Studio Build
Tools not being installed with the "Desktop development with C++" workload.
"@
    }
} finally {
    Pop-Location
}

# ---------------------------------------------------------------------------
Write-Step '6/6  Checking what came out'

$binDir = Join-Path $src 'build\bin\Release'
if (-not (Test-Path $binDir)) {
    $binDir = Join-Path $src 'build\bin'
}
$server = Join-Path $binDir 'whisper-server.exe'

if (-not (Test-Path $server)) {
    Fail "whisper-server.exe was not produced (looked in $binDir)." @"
The build finished but the server binary is missing. List what IS there with:
  Get-ChildItem '$binDir\*.exe'
and report that back - dictate needs whisper-server.exe specifically, because
that is the form that keeps the model loaded in VRAM between sentences.
"@
}

Get-ChildItem "$binDir\*.exe" | ForEach-Object {
    Write-Good ('{0,-24} {1,8:N1} MB' -f $_.Name, ($_.Length / 1MB))
}

if (Test-Path (Join-Path $binDir 'parakeet-cli.exe')) {
    Write-Good 'parakeet-cli.exe is here too - this is the thing the prebuilt zips do not have.'
}

Write-Host "`n" -NoNewline
Write-Host 'Done.' -ForegroundColor Green
Write-Host ''
Write-Host 'Put this into your dictate config, under [whisper]:'
Write-Host ""
Write-Host "  server_exe = '$server'" -ForegroundColor White
Write-Host ''
Write-Host 'Next:'
Write-Host "  powershell -ExecutionPolicy Bypass -File scripts\fetch-models.ps1"
Write-Host '  dictate doctor'
Write-Host ''
Write-Host 'To check the GPU is really being used, run this and look for a'
Write-Host '"using Vulkan0 backend" line:'
Write-Host ""
Write-Host "  & '$server' --model <your-model.bin> --port 8178" -ForegroundColor DarkGray
Write-Host ''
Write-Host 'NOTE: do not add -fa / --flash-attn. whisper.cpp issue #3806 is'
Write-Host 'whisper-server crashing inside amdvlk64.dll with it enabled on AMD.'
