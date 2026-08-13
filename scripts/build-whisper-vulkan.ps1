<#
.SYNOPSIS
    Builds whisper.cpp with the Vulkan backend from source, on Windows.

.DESCRIPTION
    This is one step of the full installer, kept as its own command because it
    is the one worth re-running on its own - to rebuild after a Vulkan
    regression, or to go back to a known-good version of whisper.cpp. It runs
    exactly the same code as `setup.ps1`; there is no second copy of the build
    to drift out of date.

    For a first-time install, run setup.ps1 instead. It does this and the other
    five steps in order.

    Why from source rather than a download: whisper.cpp publishes no official
    Windows Vulkan binary (issues #3673 and #3691, both still open), so the
    alternatives were a stranger's zip or this. Building it yourself is fully
    trusted, it also produces parakeet-cli.exe, and - the part that matters
    later - it means you can roll back to an earlier commit if a Vulkan
    regression ever ships. That happens: llama.cpp #22992 was a gfx1030-specific
    Vulkan regression, fixed in two days.

    Why Vulkan rather than ROCm: AMD ships no gfx1030 matrix-multiply kernels in
    any current ROCm-for-Windows release, so the ROCm route cannot work on an
    RX 6900 XT. Run scripts/check-rocm-gfx1030.py if you want to see that for
    yourself - it takes 30 seconds and downloads nothing.

.PARAMETER Root
    Where to put everything. Default C:\dictate-gpu.

.PARAMETER Ref
    A whisper.cpp tag or commit to build. Default: the current tip.
    Pass a known-good commit here if a regression ever lands.

.PARAMETER SkipTools
    Do not check or install the build tools first. Use if you know CMake, Git,
    the Vulkan SDK and the C++ build tools are already in place.

.PARAMETER Rebuild
    Compile from scratch even if a finished build is already there.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\build-whisper-vulkan.ps1

.EXAMPLE
    # Pin to a specific release if a later one regresses on your card:
    powershell -ExecutionPolicy Bypass -File scripts\build-whisper-vulkan.ps1 -Ref v1.9.2 -Rebuild
#>

[CmdletBinding()]
param(
    [string]$Root = 'C:\dictate-gpu',
    [string]$Ref = '',
    [switch]$SkipTools,
    [switch]$Rebuild
)

$ErrorActionPreference = 'Stop'

$steps = @('toolchain', 'build')
if ($SkipTools) { $steps = @('build') }

& (Join-Path (Split-Path -Parent $PSScriptRoot) 'setup.ps1') `
    -Root $Root -Ref $Ref -Only $steps -Rebuild:$Rebuild
exit $LASTEXITCODE
