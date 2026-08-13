<#
.SYNOPSIS
    Installs dictate_v1 on this Windows PC, from nothing to working, in one go.

.DESCRIPTION
    Six steps, in order, each of which says what it is doing:

      1. Preflight   - Windows, graphics driver, disk space, and one 30-second
                       diagnostic that explains why the Vulkan route was chosen
      2. Toolchain   - Python, Git, CMake, the Vulkan SDK, the C++ build tools
      3. Build       - whisper.cpp with the Vulkan backend, from source
      4. Models      - Whisper large-v3-turbo and the live-caption model
      5. Install     - dictate itself, and a config file with the paths filled in
      6. Verify      - does Vulkan see the GPU, does the transcription process
                       start, does the bundled test clip come back as words

    Safe to run again. Every step checks whether its work is already done and
    skips it, so if something fails - a download drops, an installer needs a
    reboot - you fix that one thing, run this again, and it carries on from
    where it stopped rather than starting over.

    It never continues past a step that failed. When something goes wrong it
    says what the problem is and what to do about it, in the last thing it
    prints.

.PARAMETER Root
    Where everything goes. Default C:\dictate-gpu. It needs about 6 GB (plus
    another ~10 GB on C: for the build tools, wherever Root is).

.PARAMETER Only
    Run just some steps: preflight, toolchain, build, models, install, verify.
    Repeatable, e.g. -Only build,verify.

.PARAMETER Ref
    A whisper.cpp tag or commit to build instead of the current tip. Use this to
    go back to a known-good build if a Vulkan regression ever ships (that does
    happen - see docs/DESIGN.md).

.PARAMETER Rebuild
    Throw away the existing whisper.cpp build and compile it again.

.PARAMETER SkipCaptions
    Do not download the live-caption model. dictate still works; you just do not
    see words on screen while you are speaking.

.PARAMETER SkipDiagnostic
    Skip the informational ROCm check in step 1 (it needs the internet and
    changes nothing either way).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File setup.ps1

.EXAMPLE
    # Something failed half way. Fix it, then just run it again:
    powershell -ExecutionPolicy Bypass -File setup.ps1

.EXAMPLE
    # Rebuild whisper.cpp at a specific version and re-check:
    powershell -ExecutionPolicy Bypass -File setup.ps1 -Only build,verify -Ref v1.9.2 -Rebuild
#>

[CmdletBinding()]
param(
    [string]$Root = 'C:\dictate-gpu',
    [ValidateSet('preflight', 'toolchain', 'build', 'models', 'install', 'verify')]
    [string[]]$Only = @(),
    [string]$Ref = '',
    [switch]$Rebuild,
    [switch]$SkipCaptions,
    [switch]$SkipDiagnostic
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest is far faster without it

. (Join-Path $PSScriptRoot 'scripts\setup-lib.ps1')

$RepoRoot = $PSScriptRoot

# ---------------------------------------------------------------------------
# What gets downloaded, pinned by size and SHA-256 in one shared file so that
# the installer and the CI workflow can never disagree about it.
# ---------------------------------------------------------------------------

$Models = Import-PowerShellDataFile (Join-Path $PSScriptRoot 'scripts\models.psd1')
$WhisperModel = $Models.Whisper
$CaptionModel = $Models.Captions

$WhisperRepo = 'https://github.com/ggml-org/whisper.cpp'

# The Vulkan SDK's winget package identifier. It is KhronosGroup.VulkanSDK, not
# LunarG.VulkanSDK: winget's community repository files it under the publisher
# KhronosGroup (microsoft/winget-pkgs, manifests/k/KhronosGroup/VulkanSDK -
# there is no manifests/l/LunarG at all), while winget DISPLAYS the publisher as
# "LunarG Inc.". Setup asked for the display name for months; winget answered
# "No package found matching input criteria" every time, and the toolchain step
# then blamed the missing SDK on Windows not having announced it yet.
# Checked against microsoft/winget-pkgs on 13 August 2026, newest manifest
# there 1.4.357.0. If it ever moves again, the direct download from LunarG below
# is what keeps setup working while this line is corrected.
$VulkanWingetId = 'KhronosGroup.VulkanSDK'
$MinFreeGbHard = 15
$MinFreeGbComfortable = 25
$TestClip = Join-Path $RepoRoot 'assets\jfk.wav'

$AdrenalinClickPath = @'
Right-click your desktop -> AMD Software: Adrenalin Edition ->
the gear icon (top right) -> System tab -> Software & Driver.
The line labelled "Adrenalin Edition" is the version number.
'@

# ---------------------------------------------------------------------------
# Small helpers that need the parameters above
# ---------------------------------------------------------------------------

$script:Python = $null          # @{ Path; Version } once step 2 or 5 has found it
$script:BuiltCommit = ''
$script:VerifyProblems = New-Object System.Collections.Generic.List[string]

function Get-WhisperSourceDir { return (Join-Path $Root 'whisper.cpp') }
function Get-ModelsDir { return (Join-Path $Root 'models') }

function Get-WhisperServerExe {
    <# whisper.cpp puts binaries under build\bin\Release with the Visual Studio
       generator and under build\bin with single-config generators. Look in both
       rather than assuming. #>
    $src = Get-WhisperSourceDir
    foreach ($rel in @('build\bin\Release', 'build\bin')) {
        $candidate = Join-Path $src (Join-Path $rel 'whisper-server.exe')
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    return $null
}

function Get-RequiredPython {
    if ($script:Python) { return $script:Python }
    $found = Get-PythonCommand -MinimumVersion '3.11'
    if (-not $found) {
        Stop-Setup -Problem 'Python 3.11 or newer is not installed (or Windows is only offering the Microsoft Store placeholder).' `
            -NextAction @"
Run this setup again without -Only, so step 2 can install Python for you. Or
install it yourself with:
  winget install --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
then close this window, open PowerShell again, and re-run setup.
"@
    }
    $script:Python = $found
    return $found
}

function Invoke-Dictate {
    <# Run a dictate command through the interpreter that has it installed,
       which works whether or not the Python Scripts folder is on PATH.
       Returns @{ ExitCode; Output }. #>
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    $python = Get-RequiredPython
    return (Invoke-Tool -FilePath $python.Path -Arguments (@('-m', 'dictate') + $Arguments))
}

# ===========================================================================
# 1. Preflight
# ===========================================================================

function Invoke-Preflight {
    # -- Windows itself -----------------------------------------------------
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        Stop-Setup -Problem "This installs dictate on Windows, and this is not Windows." `
            -NextAction 'Run it on the Windows PC with the Radeon card in it.'
    }
    $os = $null
    try { $os = Get-CimInstance Win32_OperatingSystem -ErrorAction Stop } catch {
        Write-SetupLog "Win32_OperatingSystem query failed: $($_.Exception.Message)"
    }
    $build = [Environment]::OSVersion.Version.Build
    $caption = 'Windows'
    if ($os) { $caption = $os.Caption.Trim() }
    if ($build -lt 17763) {
        Stop-Setup -Problem "This is $caption (build $build), which is older than dictate's tools support." `
            -NextAction @"
dictate needs Windows 10 version 1809 (build 17763) or newer, because the
installer and the build tools it uses do. Update Windows from
Settings -> Windows Update, then run this again.
"@
    }
    Write-Ok "$caption (build $build)"

    # -- The graphics card and its driver -----------------------------------
    $cards = @()
    try {
        $cards = @(Get-CimInstance Win32_VideoController -ErrorAction Stop |
            Where-Object { $_.Name -match 'Radeon|AMD' })
    } catch {
        Write-SetupLog "Win32_VideoController query failed: $($_.Exception.Message)"
    }
    if ($cards.Count -eq 0) {
        Write-Note 'No AMD/Radeon graphics card found. Setup carries on - the build works either way - but transcription will run on the processor instead of the GPU, which is much slower.'
    } else {
        foreach ($card in $cards) {
            $when = ''
            if ($card.DriverDate) { $when = ', dated ' + ([datetime]$card.DriverDate).ToString('d MMMM yyyy') }
            Write-Ok "$($card.Name) - driver $($card.DriverVersion)$when"
            if ($card.DriverDate) {
                $age = (Get-Date) - [datetime]$card.DriverDate
                if ($age.TotalDays -gt 550) {
                    Write-Note ('That driver is about {0:N0} years old. Vulkan works with any reasonably current Adrenalin, but update it if the GPU check in step 6 finds nothing.' -f ($age.TotalDays / 365))
                }
            }
        }
    }
    Write-Detail 'To read the Adrenalin version by hand:'
    foreach ($line in $AdrenalinClickPath -split "`n") { Write-Detail "  $($line.TrimEnd())" }

    # -- Disk ---------------------------------------------------------------
    $checked = New-Object System.Collections.Generic.List[string]
    foreach ($path in @($Root, $env:SystemDrive)) {
        if (-not $path) { continue }
        $drive = [System.IO.Path]::GetPathRoot([System.IO.Path]::GetFullPath($path))
        if ($checked.Contains($drive)) { continue }
        $checked.Add($drive)
        $free = Get-FreeSpaceGb -Path $path
        if ($free -lt 0) {
            Write-Note "Could not check the free space on $drive."
            continue
        }
        if ($free -lt $MinFreeGbHard) {
            Stop-Setup -Problem "Only $free GB is free on $drive, and this install needs about $MinFreeGbComfortable GB." `
                -NextAction @"
Free up some space and run this again. The big items are the C++ build tools
(about 7 GB), the Vulkan SDK (about 2 GB), the whisper.cpp build (about 2 GB)
and the two models (about 2 GB).
If another drive has room, you can put the dictate part elsewhere with:
  powershell -ExecutionPolicy Bypass -File setup.ps1 -Root D:\dictate-gpu
(the build tools still go on $($env:SystemDrive))
"@
        }
        if ($free -lt $MinFreeGbComfortable) {
            Write-Note "$free GB free on $drive. That should just fit, but $MinFreeGbComfortable GB would be comfortable."
        } else {
            Write-Ok "$free GB free on $drive"
        }
    }

    # -- The 30-second ROCm reality check (informational only) --------------
    Invoke-RocmDiagnostic
}

function Invoke-RocmDiagnostic {
    <# Stage 0c of the GPU report, kept as a diagnostic and never as a gate.

       It answers "why not faster-whisper on ROCm, like everyone else?" from
       AMD's own package, in about thirty seconds, without downloading a
       gigabyte. Nothing about the install depends on the answer - but if the
       answer ever changes, that is worth knowing. #>
    if ($SkipDiagnostic) {
        Write-Skip 'ROCm diagnostic skipped at your request (it changes nothing either way).'
        return
    }
    $script = Join-Path $RepoRoot 'scripts\check-rocm-gfx1030.py'
    if (-not (Test-Path -LiteralPath $script)) { return }
    $python = Get-PythonCommand -MinimumVersion '3.8'
    if (-not $python) {
        Write-Skip 'ROCm diagnostic skipped - Python is not installed yet. It is only informational.'
        return
    }

    Write-Detail 'Asking AMD whether their maths library has any kernels for this card (30 seconds, informational only)...'
    $out = Join-Path $env:TEMP 'dictate-rocm-check.txt'
    $err = Join-Path $env:TEMP 'dictate-rocm-check.err.txt'
    $text = ''
    try {
        $proc = Start-Process -FilePath $python.Path -ArgumentList @($script) -NoNewWindow -PassThru `
            -RedirectStandardOutput $out -RedirectStandardError $err
        if (-not $proc.WaitForExit(90000)) {
            $proc.Kill()
            Write-Skip 'ROCm diagnostic took too long and was stopped. It is only informational.'
            return
        }
        if (Test-Path -LiteralPath $out) { $text = Get-Content -LiteralPath $out -Raw }
    } catch {
        Write-SetupLog "ROCm diagnostic failed to run: $($_.Exception.Message)"
        Write-Skip 'ROCm diagnostic could not run (it needs the internet). It is only informational.'
        return
    }

    if (-not $text) {
        Write-Skip 'ROCm diagnostic returned nothing (it needs the internet). It is only informational.'
        return
    }
    Write-SetupLog $text
    $m = [regex]::Match($text, 'FILES FOR YOUR CARD \(gfx1030\):\s*(\d+)')
    if (-not $m.Success) {
        Write-Skip 'ROCm diagnostic did not report a number. It is only informational.'
        return
    }
    $count = [int]$m.Groups[1].Value
    if ($count -eq 0) {
        Write-Ok 'AMD ships no maths kernels for this card on Windows (gfx1030: 0 files) - which is exactly why dictate uses Vulkan, not ROCm. Nothing to do.'
    } else {
        Write-Note "AMD now ships $count kernel files for gfx1030, where there were none. That is news: the ROCm route may have become possible since this was researched. It changes nothing about this install - the Vulkan route still works - but it is worth reporting."
    }
}

# ===========================================================================
# 2. Toolchain
# ===========================================================================

function New-WantedPackage {
    <# One tool winget is about to be asked for, with the test that decides
       whether it is really there afterwards.

       That Check is the point of this: winget's exit code cannot be the
       authority on success, because it reports "already installed" and "restart
       required" as failures. So every package carries its own way of answering
       "is this tool now usable?", and every key exists from the start so that
       nothing here can read a field that was never set. #>
    param(
        [Parameter(Mandatory = $true)][string]$Id,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][scriptblock]$Check,
        [string]$Override = '',
        [switch]$Slow
    )
    return @{
        Id       = $Id
        Name     = $Name
        Check    = $Check
        Override = $Override
        Slow     = [bool]$Slow
        Failed   = $false
        ExitCode = 0
        Said     = ''
    }
}

function Confirm-VulkanSdk {
    <# An SDK has been found. Point this window at it and say which of the two
       "it is here" states we are in - because "already set up" and "here, but
       Windows had not told this window" are different things, and the second
       one used to be reported as a failure that needed a reboot. #>
    param([Parameter(Mandatory = $true)]$Found)
    Set-VulkanSdkForSession -Path $Found.Path
    if ($Found.Source -eq 'session') {
        Write-Skip "The Vulkan SDK is already installed ($($Found.Path))"
    } else {
        Write-Ok "The Vulkan SDK is installed at $($Found.Path)"
        Write-Detail 'Windows had not told this window about it, so setup has pointed this run'
        Write-Detail "at it directly - found via $($Found.How). Nothing needs restarting."
    }
    if (-not (Test-VulkanShaderCompiler -Path $Found.Path)) {
        Write-Note "That SDK has no Bin\glslc.exe in it, and the build needs it to compile the graphics shaders."
        Write-Detail 'If step 3 later says it cannot find glslc, run the Vulkan SDK installer'
        Write-Detail 'again from https://vulkan.lunarg.com/sdk/home#windows and leave every'
        Write-Detail 'component ticked.'
    }
}

function Invoke-Toolchain {
    $wanted = New-Object System.Collections.Generic.List[hashtable]

    $python = Get-PythonCommand -MinimumVersion '3.11'
    if ($python) {
        Write-Skip "Python $($python.Version) is already installed"
        $script:Python = $python
    } else {
        $wanted.Add((New-WantedPackage -Id 'Python.Python.3.12' -Name 'Python 3.12' -Check {
                    $script:Python = Get-PythonCommand -MinimumVersion '3.11'
                    return [bool]$script:Python
                }))
    }

    $git = Get-ToolVersion -Command 'git'
    if ($git) {
        Write-Skip "Git $git is already installed"
    } else {
        $wanted.Add((New-WantedPackage -Id 'Git.Git' -Name 'Git' -Check {
                    return [bool](Get-ToolVersion -Command 'git')
                }))
    }

    $cmakeCheck = {
        $found = Get-ToolVersion -Command 'cmake'
        return ($found -and $found -ge ([version]'3.20'))
    }
    $cmake = Get-ToolVersion -Command 'cmake'
    if ($cmake -and $cmake -ge ([version]'3.20')) {
        Write-Skip "CMake $cmake is already installed"
    } else {
        if ($cmake) {
            Write-Detail "CMake $cmake is too old (3.20 or newer is needed); installing a current one."
        }
        $wanted.Add((New-WantedPackage -Id 'Kitware.CMake' -Name 'CMake' -Check $cmakeCheck))
    }

    # The Vulkan SDK is looked for, not deduced from a variable. See the block
    # comment above Test-VulkanSdkDir in scripts/setup-lib.ps1 for why.
    Update-SessionEnvironment
    $vulkan = Resolve-VulkanSdk
    if ($vulkan) {
        Confirm-VulkanSdk $vulkan
    } else {
        $wanted.Add((New-WantedPackage -Id $VulkanWingetId -Name 'Vulkan SDK' -Check {
                    return [bool](Resolve-VulkanSdk)
                }))
    }

    $vs = Get-VisualStudioCppPath
    if ($vs) {
        Write-Skip "The C++ build tools are already installed ($vs)"
    } else {
        $wanted.Add((New-WantedPackage `
                    -Id 'Microsoft.VisualStudio.2022.BuildTools' `
                    -Name 'Visual Studio 2022 Build Tools (the C++ compiler)' `
                    -Override '--quiet --wait --norestart --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended' `
                    -Slow `
                    -Check { return [bool](Get-VisualStudioCppPath) }))
    }

    if ($wanted.Count -eq 0) {
        Write-Ok 'Everything needed to build is already on this PC.'
        return
    }

    Write-Detail ('Missing: ' + (($wanted | ForEach-Object { $_.Name }) -join ', '))

    if (-not (Get-Command 'winget' -ErrorAction SilentlyContinue)) {
        Stop-Setup -Problem 'The tools above are missing, and winget - the Windows installer command this uses to fetch them - is not available.' `
            -NextAction @"
Open the Microsoft Store, search for "App Installer", and install or update it.
That is what provides winget. Then run this setup again.
If the Store is unavailable on this PC, install these by hand and re-run setup:
  Python  https://www.python.org/downloads/windows/
  Git     https://git-scm.com/download/win
  CMake   https://cmake.org/download/
  Vulkan  https://vulkan.lunarg.com/sdk/home#windows
  C++     https://visualstudio.microsoft.com/downloads/  ("Build Tools for
          Visual Studio", then tick "Desktop development with C++")
"@
    }

    if (-not (Test-IsAdministrator)) {
        Stop-Setup -Problem 'Installing the tools above needs administrator rights, and this window does not have them.' `
            -NextAction @"
Close this window. Click Start, type "PowerShell", right-click "Windows
PowerShell" and choose "Run as administrator". Then run the same command again:
  powershell -ExecutionPolicy Bypass -File "$PSCommandPath"
Everything already done is kept - it carries on from here.
"@
    }

    foreach ($package in $wanted) {
        $note = ''
        if ($package.Slow) { $note = ' - this is the slow one, 5-15 minutes' }
        Write-Detail "Installing $($package.Name)$note..."
        $arguments = @('install', '--id', $package.Id, '--exact', '--source', 'winget',
            '--accept-source-agreements', '--accept-package-agreements', '--disable-interactivity')
        if ($package.Override) {
            $arguments += @('--override', $package.Override)
        }
        $run = Invoke-Tool -FilePath 'winget' -Arguments $arguments
        Write-SetupLog "winget exit code for $($package.Id): $(Format-ExitCode $run.ExitCode)"
        Update-SessionEnvironment

        # winget's exit code is not the authority on SUCCESS - whether the tool
        # now exists is. It reports "already installed" and "restart required"
        # as failures, and a zero exit with nothing installed is not a success.
        #
        # But when the tool is not there, the exit code and winget's own words
        # are the evidence for why, and they are said HERE, at the install that
        # failed. Letting them fall on the floor is how a winget that answered
        # "No package found matching input criteria" turned into a claim, four
        # checks later, that the Vulkan SDK had installed and Windows was being
        # slow about it.
        if (& $package.Check) {
            Write-Ok "$($package.Name) installed."
            continue
        }
        $package.Failed = $true
        $package.ExitCode = $run.ExitCode
        $package.Said = Get-WingetFailureSummary -Output $run.Output
        Write-Note (Get-WingetFailureNote -Name $package.Name -Id $package.Id `
                -ExitCode $package.ExitCode -Said $package.Said)
    }

    # -- Be specific about whatever is still missing ------------------------
    # Everything except the Vulkan SDK first: it is the only one with a second
    # route to try, so there is no sense downloading 200 MB of it while the C++
    # compiler is missing anyway.
    $failed = @($wanted | Where-Object { $_.Failed -and $_.Id -ne $VulkanWingetId })
    if ($failed.Count -gt 0) {
        $detail = New-Object System.Collections.Generic.List[string]
        foreach ($package in $failed) {
            $detail.Add((Get-WingetFailureNote -Name $package.Name -Id $package.Id `
                        -ExitCode $package.ExitCode -Said $package.Said))
        }
        $blank = [Environment]::NewLine + [Environment]::NewLine
        Stop-Setup -Problem ($detail -join $blank) `
            -NextAction @"
Install the ones that failed by hand, then run this setup again - it keeps
everything it has already done and carries on:
  Python  https://www.python.org/downloads/windows/
  Git     https://git-scm.com/download/win
  CMake   https://cmake.org/download/
  C++     https://visualstudio.microsoft.com/downloads/  ("Build Tools for
          Visual Studio", then tick "Desktop development with C++")
If winget said a package could not be found, that is the identifier in this
script being out of date rather than anything wrong with your PC - the pages
above install exactly the same thing.
The full output from every installer is in:
  $script:DictateLogPath
"@
    }

    # -- The Vulkan SDK: found, findable, or genuinely not installed ---------
    $vulkanPackage = @($wanted | Where-Object { $_.Id -eq $VulkanWingetId })
    $directReason = ''
    if ($vulkanPackage.Count -eq 1 -and $vulkanPackage[0].Failed) {
        # winget could not install it. Say so, then take the other route rather
        # than leaving him with nothing to do.
        Write-Detail 'Trying the other route: LunarG publish the SDK on their own site.'
        $direct = Install-VulkanSdkFromLunarG -WorkDir $Root
        if (-not $direct.Ok) {
            $directReason = $direct.Reason
            Write-Note "That did not work either: $directReason."
        }
    }

    $vulkan = Resolve-VulkanSdk
    if (-not $vulkan) {
        $id = ''
        $code = 0
        $said = ''
        if ($vulkanPackage.Count -eq 1) {
            $id = $vulkanPackage[0].Id
            $code = $vulkanPackage[0].ExitCode
            $said = $vulkanPackage[0].Said
        }
        $report = Get-VulkanSdkMissingReport -WingetId $id -WingetExitCode $code `
            -WingetSaid $said -DirectReason $directReason
        Stop-Setup -Problem $report.Problem -NextAction $report.NextAction
    }
    Confirm-VulkanSdk $vulkan
    Write-Ok 'All build tools are in place.'
}

# ===========================================================================
# 3. Build whisper.cpp with Vulkan
# ===========================================================================

function Invoke-Build {
    foreach ($tool in @('git', 'cmake')) {
        if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
            Stop-Setup -Problem "$tool is needed to build whisper.cpp and is not available in this window." `
                -NextAction @"
Run the whole setup (without -Only) so step 2 can install it, or restart the PC
if you have just installed it, and run setup again.
"@
        }
    }
    # Same rule as step 2: look for the SDK, do not deduce it from a variable.
    # If it is on disk, point this window at it and get on with the build
    # instead of sending him away to restart a PC that has nothing to gain.
    Update-SessionEnvironment
    $vulkan = Resolve-VulkanSdk
    if (-not $vulkan) {
        $report = Get-VulkanSdkMissingReport
        Stop-Setup -Problem ($report.Problem +
            [Environment]::NewLine + [Environment]::NewLine +
            'Without it CMake cannot find Vulkan, and the build would come out with no GPU support at all.') `
            -NextAction ("Run the whole setup, without -Only, and step 2 installs it for you:" +
                [Environment]::NewLine +
                "  powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`"" +
                [Environment]::NewLine + [Environment]::NewLine +
                'If step 2 has already been tried and could not do it:' +
                [Environment]::NewLine + $report.NextAction)
    }
    Set-VulkanSdkForSession -Path $vulkan.Path
    Write-Detail "Building against the Vulkan SDK at $($vulkan.Path)."

    $src = Get-WhisperSourceDir
    $existing = Get-WhisperServerExe

    if ($existing -and -not $Rebuild) {
        $cache = Join-Path $src 'build\CMakeCache.txt'
        $hasVulkan = $false
        if (Test-Path -LiteralPath $cache) {
            $hasVulkan = @(Select-String -LiteralPath $cache -Pattern '^GGML_VULKAN:BOOL=(ON|1|TRUE)$' -ErrorAction SilentlyContinue).Count -gt 0
        }
        if ($hasVulkan) {
            Write-Skip "whisper.cpp is already built with Vulkan ($existing)"
            Write-Detail 'Pass -Rebuild if you want it compiled again.'
            return
        }
        Write-Note 'whisper.cpp is built, but not with the Vulkan backend. Rebuilding it properly.'
    }

    # -- Source -------------------------------------------------------------
    New-Item -ItemType Directory -Force -Path $Root | Out-Null
    if (-not (Test-Path -LiteralPath (Join-Path $src '.git'))) {
        if (Test-Path -LiteralPath $src) {
            Stop-Setup -Problem "There is a folder at $src that is not a whisper.cpp checkout, so setup will not touch it." `
                -NextAction @"
Rename or delete that folder, then run setup again. Or point setup somewhere
else entirely:
  powershell -ExecutionPolicy Bypass -File setup.ps1 -Root D:\dictate-gpu
"@
        }
        Write-Detail "Downloading the whisper.cpp source into $src ..."
        $clone = Invoke-Tool -FilePath 'git' -Arguments @('clone', '--depth', '1', $WhisperRepo, $src) -Show echo
        Assert-ExitCode -Code $clone.ExitCode -Problem 'The whisper.cpp source could not be downloaded.' `
            -NextAction @"
Check you are online, then run setup again. If your network blocks GitHub, that
is the thing to fix - the source has to come from somewhere.
"@
    } else {
        Write-Skip "whisper.cpp source is already at $src"
    }

    Push-Location $src
    try {
        if ($Ref) {
            Write-Detail "Switching whisper.cpp to $Ref ..."
            $fetch = Invoke-Tool -FilePath 'git' -Arguments @('fetch', '--depth', '1', 'origin', $Ref)
            if ($fetch.ExitCode -ne 0) {
                Invoke-Tool -FilePath 'git' -Arguments @('fetch', '--tags', 'origin') | Out-Null
            }
            $checkout = Invoke-Tool -FilePath 'git' -Arguments @('checkout', '--detach', 'FETCH_HEAD')
            if ($checkout.ExitCode -ne 0) {
                $checkout = Invoke-Tool -FilePath 'git' -Arguments @('checkout', '--detach', $Ref)
            }
            Assert-ExitCode -Code $checkout.ExitCode -Problem "whisper.cpp has no version called '$Ref'." `
                -NextAction @"
Check the spelling. Versions look like v1.9.2, and commits look like a1b2c3d.
The list of versions is at $WhisperRepo/releases
"@
        }
        $commit = Get-LastLine (Invoke-Tool -FilePath 'git' -Arguments @('rev-parse', '--short', 'HEAD')).Output
        $script:BuiltCommit = $commit
        Write-Ok "Building whisper.cpp at commit $commit"
        Write-Detail "Keep that number. If a future build ever produces gibberish, come back to"
        Write-Detail "this one with:  setup.ps1 -Only build,verify -Ref $commit -Rebuild"

        # -- Configure ------------------------------------------------------
        $buildDir = Join-Path $src 'build'
        $cache = Join-Path $buildDir 'CMakeCache.txt'
        $configured = $false
        if ((Test-Path -LiteralPath $cache) -and -not $Rebuild) {
            $vulkanOn = @(Select-String -LiteralPath $cache -Pattern '^GGML_VULKAN:BOOL=(ON|1|TRUE)$' -ErrorAction SilentlyContinue).Count -gt 0
            if ($vulkanOn) {
                Write-Skip 'The build folder is already set up for Vulkan; carrying on from where it got to.'
                $configured = $true
            } else {
                Write-Detail 'The existing build folder was not set up for Vulkan. Starting it fresh.'
            }
        }
        if (-not $configured) {
            if (Test-Path -LiteralPath $buildDir) {
                Write-Detail 'Clearing the previous build folder...'
                Remove-Item -LiteralPath $buildDir -Recurse -Force
            }
            Write-Detail 'Setting up the build with the Vulkan backend turned on...'
            $configure = Invoke-Tool -FilePath 'cmake' -Arguments @('-B', 'build', '-DGGML_VULKAN=1') -Show echo
            Assert-ExitCode -Code $configure.ExitCode -Problem 'CMake could not set up the build.' `
                -NextAction @"
If the message above mentions "Could not find Vulkan", the SDK setup is using
is incomplete rather than absent - setup found it at
$($vulkan.Path). Run the Vulkan SDK installer again from
https://vulkan.lunarg.com/sdk/home#windows, leave every component ticked, and
run setup again.
If it mentions no C++ compiler, the build tools did not install: run the whole
setup (without -Only) as administrator.
The full output is in:
  $script:DictateLogPath
"@
        }

        # -- Compile --------------------------------------------------------
        Write-Detail 'Compiling. This takes 5-15 minutes the first time, and warnings scrolling'
        Write-Detail 'past are normal. Anything already compiled is reused.'
        $clock = [Diagnostics.Stopwatch]::StartNew()
        $compile = Invoke-Tool -FilePath 'cmake' -Arguments @('--build', 'build', '-j', '--config', 'Release') -Show echo
        $clock.Stop()
        Assert-ExitCode -Code $compile.ExitCode -Problem 'The compile failed.' `
            -NextAction @"
Scroll up to the first line containing the word "error" - that one is the real
problem and everything after it is noise. The usual cause is the C++ build
tools being installed without the "Desktop development with C++" part.
Running the whole setup again (without -Only), as administrator, reinstalls
them. The full output is also in:
  $script:DictateLogPath
"@
        Write-Ok ('Compiled in ' + (Format-Duration $clock.Elapsed.TotalSeconds))
    } finally {
        Pop-Location
    }

    # -- What came out ------------------------------------------------------
    $server = Get-WhisperServerExe
    if (-not $server) {
        $binDir = Join-Path $src 'build\bin\Release'
        Stop-Setup -Problem "The compile finished, but whisper-server.exe is not there." `
            -NextAction @"
dictate needs whisper-server.exe specifically: it is the form that keeps the
1.6 GB model loaded on the graphics card between sentences.
Run this and report what it lists:
  Get-ChildItem '$binDir\*.exe'
Then run setup again with -Rebuild to compile it from scratch:
  powershell -ExecutionPolicy Bypass -File setup.ps1 -Only build -Rebuild
"@
    }
    Write-Ok "whisper-server.exe is at $server"
    $binDir = Split-Path -Parent $server
    if (Test-Path -LiteralPath (Join-Path $binDir 'parakeet-cli.exe')) {
        Write-Detail 'parakeet-cli.exe was built too - that is the thing the prebuilt downloads do not have.'
    }
    Write-TextFileNoBom -Path (Join-Path $Root 'whisper-build.txt') -Lines @(
        "whisper.cpp commit: $script:BuiltCommit",
        "built:              $((Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))",
        "server:             $server",
        "backend:            Vulkan (GGML_VULKAN=1)"
    )
}

# ===========================================================================
# 4. Models
# ===========================================================================

function Invoke-Models {
    $models = Get-ModelsDir
    New-Item -ItemType Directory -Force -Path $models | Out-Null

    $free = Get-FreeSpaceGb -Path $models
    if ($free -ge 0 -and $free -lt 3) {
        Stop-Setup -Problem "The two models need about 2 GB and only $free GB is free on that drive." `
            -NextAction 'Free up some space and run setup again. Anything already downloaded is kept.'
    }

    # -- Whisper ------------------------------------------------------------
    $whisperPath = Join-Path $models $WhisperModel.Name
    Write-Detail "$($WhisperModel.Label): $((Format-Bytes $WhisperModel.Bytes)) - this is the big one."
    $result = Get-FileResumable -Uri $WhisperModel.Uri -Destination $whisperPath `
        -ExpectedBytes $WhisperModel.Bytes -ExpectedSha256 $WhisperModel.Sha256 -Label $WhisperModel.Label
    if ($result -eq 'already-here') {
        Write-Skip "$($WhisperModel.Label) is already downloaded and verified"
    } else {
        Write-Ok "$($WhisperModel.Label) downloaded and verified: $whisperPath"
    }

    # -- Live captions ------------------------------------------------------
    if ($SkipCaptions) {
        Write-Skip 'Live-caption model skipped at your request. Set [captions] enabled = false in your config to match.'
        return
    }
    $captionDir = Join-Path $models $CaptionModel.Name
    $complete = $true
    foreach ($file in $CaptionModel.Files) {
        if (-not (Test-Path -LiteralPath (Join-Path $captionDir $file))) { $complete = $false }
    }
    if ($complete) {
        Write-Skip "$($CaptionModel.Label) is already unpacked at $captionDir"
        return
    }

    $archive = Join-Path $models 'caption-model.tar.bz2'
    Write-Detail "$($CaptionModel.Label): $((Format-Bytes $CaptionModel.Bytes))."
    Get-FileResumable -Uri $CaptionModel.Uri -Destination $archive `
        -ExpectedBytes $CaptionModel.Bytes -ExpectedSha256 $CaptionModel.Sha256 -Label $CaptionModel.Label | Out-Null

    Write-Detail 'Unpacking it...'
    if (Test-Path -LiteralPath $captionDir) { Remove-Item -LiteralPath $captionDir -Recurse -Force }
    $unpack = Invoke-Tool -FilePath 'tar' -Arguments @('-xf', $archive, '-C', $models)
    Assert-ExitCode -Code $unpack.ExitCode -Problem 'The live-caption model downloaded but could not be unpacked.' `
        -NextAction @"
Delete this file and run setup again:
  Remove-Item '$archive'
If it fails the same way a second time, run setup with -SkipCaptions. dictate
works without live captions - you just do not see words on screen while you are
speaking, and the text that gets pasted is unaffected.
"@

    $missing = @()
    foreach ($file in $CaptionModel.Files) {
        if (-not (Test-Path -LiteralPath (Join-Path $captionDir $file))) { $missing += $file }
    }
    if ($missing.Count -gt 0) {
        Stop-Setup -Problem ("The live-caption model unpacked, but these files dictate needs are not in it: " + ($missing -join ', ')) `
            -NextAction @"
This means the model has been repackaged differently since setup was written.
See what did arrive with:
  Get-ChildItem '$captionDir'
and report it. In the meantime dictate runs without live captions:
  powershell -ExecutionPolicy Bypass -File setup.ps1 -SkipCaptions
"@
    }
    Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath "$archive.sha256" -Force -ErrorAction SilentlyContinue
    Write-Ok "$($CaptionModel.Label) unpacked at $captionDir"
}

# ===========================================================================
# 5. Install dictate and write its config
# ===========================================================================

function Invoke-Install {
    $python = Get-RequiredPython
    Write-Detail "Installing dictate with Python $($python.Version) ($($python.Path))..."

    Invoke-Tool -FilePath $python.Path -Arguments @('-m', 'pip', 'install', '--upgrade', 'pip') | Out-Null
    $install = Invoke-Tool -FilePath $python.Path -Arguments @('-m', 'pip', 'install', '-e', "$RepoRoot[windows]")
    foreach ($line in ($install.Output -split "`r?`n")) {
        if ($line -match '^\s*(ERROR|error:)') { Write-Detail $line.Trim() }
    }
    Assert-ExitCode -Code $install.ExitCode -Problem 'Installing dictate and the packages it needs failed.' `
        -NextAction @"
The usual cause is no internet connection, or a company proxy blocking
pypi.org. Check you are online and run setup again.
The full output from the installer is in:
  $script:DictateLogPath
"@

    $check = Invoke-Tool -FilePath $python.Path -Arguments @('-c', 'import dictate; print(dictate.__version__)')
    if ($check.ExitCode -ne 0) {
        Stop-Setup -Problem 'dictate installed, but Python cannot load it.' `
            -NextAction @"
Run this and report what it says:
  "$($python.Path)" -c "import dictate"
The full installer output is in:
  $script:DictateLogPath
"@
    }
    Write-Ok "dictate $(Get-LastLine $check.Output) installed"

    # -- Make `dictate` typeable at the prompt -------------------------------
    $scriptsDir = Get-LastLine (Invoke-Tool -FilePath $python.Path `
            -Arguments @('-c', "import sysconfig; print(sysconfig.get_path('scripts'))")).Output
    if ($scriptsDir -and (Test-Path -LiteralPath $scriptsDir)) {
        $wanted = $scriptsDir.TrimEnd('\')
        $userPath = [Environment]::GetEnvironmentVariable('PATH', 'User')
        if (-not $userPath) { $userPath = '' }
        # Check both, so running setup twice cannot put it in the PATH twice.
        $known = @(($env:PATH + ';' + $userPath) -split ';' |
            Where-Object { $_ -and $_.TrimEnd('\') -ieq $wanted })
        if ($known.Count -eq 0) {
            [Environment]::SetEnvironmentVariable('PATH', ($userPath.TrimEnd(';') + ';' + $scriptsDir).TrimStart(';'), 'User')
            Update-SessionEnvironment
            Write-Detail "Added $scriptsDir to your PATH so you can type 'dictate' in any new window."
        }
    }

    # -- The config file ----------------------------------------------------
    # The same place dictate itself looks: DICTATE_CONFIG if it is set,
    # otherwise %APPDATA%\dictate\dictate.toml (config.default_config_path).
    $configPath = $env:DICTATE_CONFIG
    if (-not $configPath) { $configPath = Join-Path $env:APPDATA 'dictate\dictate.toml' }
    $existed = Test-Path -LiteralPath $configPath
    $init = Invoke-Dictate -Arguments @('init')
    if ($init.ExitCode -ne 0) {
        Stop-Setup -Problem 'dictate could not write its config file.' `
            -NextAction @"
It said:
$($init.Output.Trim())
"@
    }
    if (-not (Test-Path -LiteralPath $configPath)) {
        Stop-Setup -Problem "dictate reported writing its config, but there is nothing at $configPath." `
            -NextAction 'Run `dictate init` yourself and report what it prints.'
    }

    $server = Get-WhisperServerExe
    $stamp = "set by setup.ps1 on $((Get-Date).ToString('yyyy-MM-dd'))"
    if ($server) {
        Set-TomlValue -Path $configPath -Section 'whisper' -Key 'server_exe' -Value $server -Comment $stamp
    }
    $whisperModel = Join-Path (Get-ModelsDir) $WhisperModel.Name
    if (Test-Path -LiteralPath $whisperModel) {
        Set-TomlValue -Path $configPath -Section 'whisper' -Key 'model' -Value $whisperModel -Comment $stamp
    }
    $captionDir = Join-Path (Get-ModelsDir) $CaptionModel.Name
    if (Test-Path -LiteralPath $captionDir) {
        Set-TomlValue -Path $configPath -Section 'captions' -Key 'model_dir' -Value $captionDir -Comment $stamp
    } elseif ($SkipCaptions) {
        Set-TomlValue -Path $configPath -Section 'captions' -Key 'enabled' -Value 'false' -Comment "$stamp (-SkipCaptions)"
    }
    Set-TomlValue -Path $configPath -Section 'logging' -Key 'file' -Value (Join-Path $Root 'dictate.log') -Comment $stamp

    if ($existed) {
        Write-Ok "Your existing config at $configPath was kept; only the file paths in it were pointed at what setup installed."
    } else {
        Write-Ok "Config written to $configPath with every path already filled in."
    }
}

# ===========================================================================
# 6. Verify
# ===========================================================================

function Add-VerifyProblem {
    param([Parameter(Mandatory = $true)][string]$Message)
    $script:VerifyProblems.Add($Message)
    Write-Host "  XX  $Message" -ForegroundColor Red
    Write-SetupLog "  XX  $Message"
}

function Invoke-Verify {
    # -- 1. Can Vulkan see the graphics card? -------------------------------
    Update-SessionEnvironment
    $vulkanInfo = ''
    if ($env:VULKAN_SDK) {
        $candidate = Join-Path $env:VULKAN_SDK 'Bin\vulkaninfoSDK.exe'
        if (Test-Path -LiteralPath $candidate) { $vulkanInfo = $candidate }
    }
    if (-not $vulkanInfo) {
        $onPath = Get-Command 'vulkaninfoSDK', 'vulkaninfo' -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($onPath) { $vulkanInfo = $onPath.Source }
    }
    if (-not $vulkanInfo) {
        Write-Note 'Cannot check what Vulkan can see: the Vulkan SDK tool is not installed. The next two checks still tell you whether the GPU is being used.'
    } else {
        $summary = (Invoke-Tool -FilePath $vulkanInfo -Arguments @('--summary')).Output
        $devices = @([regex]::Matches($summary, 'deviceName\s*=\s*(.+)') | ForEach-Object { $_.Groups[1].Value.Trim() })
        if ($devices.Count -eq 0) {
            Add-VerifyProblem 'Vulkan cannot see any graphics card at all, so transcription would run on the processor and be far slower than the 2-4 seconds it is meant to take.'
            Write-Detail 'This is a graphics driver problem, not a dictate problem. Update AMD'
            Write-Detail 'Adrenalin (Home -> check for updates, or amd.com), restart, and run:'
            Write-Detail '  powershell -ExecutionPolicy Bypass -File setup.ps1 -Only verify'
        } else {
            foreach ($device in $devices) { Write-Ok "Vulkan can see: $device" }
            $amd = @($devices | Where-Object { $_ -match 'Radeon|AMD' }).Count
            if ($amd -eq 0) {
                Write-Note 'None of those is the Radeon card. Transcription will use whatever Vulkan picks first, which may be slow.'
            }
        }
    }

    # -- 2. Does dictate itself think it is ready? --------------------------
    $doctor = Invoke-Dictate -Arguments @('doctor')
    Write-SetupLog $doctor.Output
    foreach ($line in ($doctor.Output -split "`r?`n")) {
        if ($line.Trim()) { Write-Host "      $line" }
    }
    if ($doctor.ExitCode -ne 0) {
        Add-VerifyProblem 'dictate checked its own setup and found something missing - the list above says which, and what to do about each one.'
    } else {
        Write-Ok 'dictate has everything it needs.'
    }

    # -- 3. Does the real thing actually transcribe? ------------------------
    if (-not (Test-Path -LiteralPath $TestClip)) {
        Write-Note "The bundled test clip is missing from $TestClip, so the transcription test was skipped."
    } elseif ($script:VerifyProblems.Count -gt 0) {
        Write-Note 'Skipping the transcription test until the problems above are fixed - it would only fail for the same reason.'
    } else {
        Write-Detail 'Starting the transcription process and putting an 11-second test clip through it.'
        Write-Detail 'The first run is slower than normal: the graphics driver compiles its shaders once.'
        $run = Invoke-Dictate -Arguments @('transcribe', $TestClip, '--repeat', '1')

        if ($run.ExitCode -ne 0) {
            Add-VerifyProblem 'The transcription process would not run the test clip.'
            foreach ($line in ($run.Output -split "`r?`n")) {
                if ($line.Trim()) { Write-Detail $line }
            }
        } elseif ($run.Output -notmatch '(?i)country') {
            Add-VerifyProblem 'Transcription ran, but what came back does not match the test clip - the words should include "ask not what your country can do for you".'
            Write-Detail 'There are two things this can be, in the order worth trying:'
            Write-Detail ''
            Write-Detail '1. A damaged model file. Delete it and run setup again - it will'
            Write-Detail '   download it afresh and check it this time:'
            Write-Detail ("     Remove-Item '" + (Join-Path (Get-ModelsDir) $WhisperModel.Name) + "'")
            Write-Detail ''
            Write-Detail '2. A bad version of whisper.cpp. Graphics-card versions of it do'
            Write-Detail '   occasionally ship a bug that turns transcripts into nonsense, and'
            Write-Detail '   they are usually fixed within days. Go back to the last version'
            Write-Detail '   that worked - the number is in whisper-build.txt next to this'
            Write-Detail '   folder, and setup printed it when it built:'
            Write-Detail '     powershell -ExecutionPolicy Bypass -File setup.ps1 -Only build,verify -Ref <that number> -Rebuild'
        } else {
            Write-Ok 'The test clip came back as the right words.'
            $seconds = [regex]::Match($run.Output, 'best of \d+\s+([0-9.]+)s')
            if ($seconds.Success) {
                $value = [double]$seconds.Groups[1].Value
                if ($value -le 2.0) {
                    Write-Ok ("It took {0:N1} seconds for 11 seconds of speech - inside the 2-4 second budget." -f $value)
                } else {
                    Write-Note ("It took {0:N1} seconds for 11 seconds of speech, which is over the 2-4 second budget." -f $value)
                    Write-Detail 'If the line above did not mention Vulkan, it ran on the processor rather'
                    Write-Detail 'than the graphics card. The log says which:'
                    Write-Detail "  $(Join-Path $Root 'dictate.log')"
                }
            }
            # Look for what whisper.cpp actually prints, not merely for the word
            # "Vulkan" - dictate's own warning that it could NOT find Vulkan
            # contains that word too, and matching on it would report the
            # opposite of the truth. (The CI run is where that showed up.)
            if ($run.Output -match '(?i)did not mention Vulkan') {
                Write-Note 'It ran on the processor, not the graphics card, so it will be far slower than the 2-4 second budget.'
                Write-Detail 'Update AMD Adrenalin, restart, and run this again:'
                Write-Detail '  powershell -ExecutionPolicy Bypass -File setup.ps1 -Only verify'
            } elseif ($run.Output -match '(?i)using Vulkan\d+ backend' -or
                      $run.Output -match '(?i)ggml_vulkan:\s*Found\s*[1-9]') {
                Write-Ok 'It used the graphics card (whisper.cpp reported its Vulkan backend).'
            } else {
                Write-Note 'It did not say either way whether the graphics card was used. Look for a "using Vulkan0 backend" line in the log:'
                Write-Detail "  $(Join-Path $Root 'dictate.log')"
            }
        }
    }
}

# ===========================================================================
# Main
# ===========================================================================

$steps = @(
    @{ Key = 'preflight'; Title = 'Checking this PC'; Action = { Invoke-Preflight } },
    @{ Key = 'toolchain'; Title = 'Build tools'; Action = { Invoke-Toolchain } },
    @{ Key = 'build'; Title = 'Building whisper.cpp with the Vulkan backend'; Action = { Invoke-Build } },
    @{ Key = 'models'; Title = 'Downloading the two models'; Action = { Invoke-Models } },
    @{ Key = 'install'; Title = 'Installing dictate and writing its config'; Action = { Invoke-Install } },
    @{ Key = 'verify'; Title = 'Checking it actually works'; Action = { Invoke-Verify } }
)
if ($Only.Count -gt 0) {
    $steps = @($steps | Where-Object { $Only -contains $_.Key })
}

New-Item -ItemType Directory -Force -Path $Root | Out-Null
Set-SetupLogPath (Join-Path $Root 'setup-log.txt')
$global:DictateSetupProblem = $null

Write-Host ''
Write-Host 'dictate_v1 setup' -ForegroundColor White
Write-Host '----------------'
Write-Host "Installing into:  $Root"
Write-Host "Full log:         $script:DictateLogPath"
Write-SetupLog "setup.ps1 started; Root=$Root Only=$($Only -join ',') Ref=$Ref Rebuild=$Rebuild SkipCaptions=$SkipCaptions"
Write-SetupLog "PowerShell $($PSVersionTable.PSVersion); admin=$(Test-IsAdministrator)"

$overall = [Diagnostics.Stopwatch]::StartNew()
$number = 0
try {
    foreach ($step in $steps) {
        $number++
        Write-Phase -Number $number -Of $steps.Count -Title $step.Title
        & $step.Action
    }
} catch {
    $problem = $global:DictateSetupProblem
    Write-Host ''
    Write-Host '-----------------------------------------------------------------------' -ForegroundColor Red
    Write-Host 'Setup stopped.' -ForegroundColor Red
    Write-Host ''
    if ($problem) {
        Write-Host $problem.Problem -ForegroundColor White
        Write-Host ''
        Write-Host 'What to do:' -ForegroundColor Yellow
        foreach ($line in ($problem.NextAction -split "`r?`n")) { Write-Host "  $line" }
    } else {
        # Something this script did not anticipate. The product owner gets the
        # one-line reason, not the stack trace - that goes to the log.
        Write-Host 'Something went wrong that this setup did not expect:' -ForegroundColor White
        Write-Host "  $($_.Exception.Message)"
        Write-Host ''
        Write-Host 'What to do:' -ForegroundColor Yellow
        Write-Host '  Run setup again - it carries on from where it stopped. If it stops in the'
        Write-Host '  same place twice, send this file, which has the full technical detail:'
        Write-Host "    $script:DictateLogPath"
        Write-SetupLog "UNEXPECTED: $($_.Exception.ToString())"
        Write-SetupLog "AT: $($_.ScriptStackTrace)"
    }
    Write-Host ''
    Write-Host 'Nothing already installed or downloaded has been lost.' -ForegroundColor Yellow
    Write-Host '-----------------------------------------------------------------------' -ForegroundColor Red
    exit 1
}

$overall.Stop()
Write-Host ''
if ($script:VerifyProblems.Count -gt 0) {
    Write-Host '-----------------------------------------------------------------------' -ForegroundColor Yellow
    Write-Host 'Installed, but not working yet.' -ForegroundColor Yellow
    Write-Host ''
    foreach ($problem in $script:VerifyProblems) { Write-Host "  - $problem" }
    Write-Host ''
    Write-Host 'Fix those, then re-run just the checks:' -ForegroundColor Yellow
    Write-Host '  powershell -ExecutionPolicy Bypass -File setup.ps1 -Only verify'
    Write-Host '-----------------------------------------------------------------------' -ForegroundColor Yellow
    exit 1
}

Write-Host '-----------------------------------------------------------------------' -ForegroundColor Green
Write-Host ('Done in ' + (Format-Duration $overall.Elapsed.TotalSeconds) + '.') -ForegroundColor Green
Write-Host ''
Write-Host 'To start dictating, open a new PowerShell window and run:'
Write-Host '  dictate run' -ForegroundColor White
Write-Host ''
Write-Host 'Then hold Ctrl + Alt + Space, speak, and let go. Ctrl+C in that window'
Write-Host 'stops it.'
Write-Host ''
Write-Host "Config:  $(Join-Path $env:APPDATA 'dictate\dictate.toml')"
Write-Host "Log:     $(Join-Path $Root 'dictate.log')"
Write-Host '-----------------------------------------------------------------------' -ForegroundColor Green
exit 0
