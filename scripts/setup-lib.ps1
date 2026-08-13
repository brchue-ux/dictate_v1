<#
.SYNOPSIS
    Helpers for setup.ps1. Dot-source it; it defines functions and nothing else.

.DESCRIPTION
    This file is separate from setup.ps1 for two reasons. The first is that
    setup.ps1 reads as a list of six steps rather than a wall of plumbing. The
    second is that the plumbing is the risky part - a resumable download, a
    version comparison, an edit to the user's config file - and it is the part
    that CI can actually test on a real Windows machine
    (scripts/tests/setup-lib.tests.ps1, run by .github/workflows/ci.yml).

    Written for Windows PowerShell 5.1, which is what ships with Windows and
    what the setup instructions tell the product owner to use. No PowerShell 7
    syntax (no ??, no ?:, no ternaries), no classes, no third-party modules.
#>

Set-StrictMode -Version 2.0

# PowerShell 7 can be configured to turn a native command's non-zero exit into a
# terminating error, which 5.1 never does. Pin it off so this behaves the same
# on both, and every native call is checked explicitly with Assert-ExitCode.
if (Test-Path variable:PSNativeCommandUseErrorActionPreference) {
    $PSNativeCommandUseErrorActionPreference = $false
}

# ---------------------------------------------------------------------------
# Output. One voice for the whole install: a phase header, indented detail
# under it, and problems that always name a next action.
# ---------------------------------------------------------------------------

$script:DictateLogPath = ''

function Set-SetupLogPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    $script:DictateLogPath = $Path
    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
    }
}

function Write-SetupLog {
    <# Detail for the log file only. This is where anything that looks like a
       stack trace belongs - never on the product owner's screen. #>
    param([Parameter(Mandatory = $true)][string]$Message)
    if (-not $script:DictateLogPath) { return }
    $stamp = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
    try {
        Add-Content -LiteralPath $script:DictateLogPath -Value "$stamp  $Message" -Encoding UTF8
    } catch {
        # A log that cannot be written must never be the thing that stops an
        # install. Carry on silently.
    }
}

function Write-Phase {
    param(
        [Parameter(Mandatory = $true)][int]$Number,
        [Parameter(Mandatory = $true)][int]$Of,
        [Parameter(Mandatory = $true)][string]$Title
    )
    Write-Host ''
    Write-Host ("[$Number/$Of] $Title") -ForegroundColor Cyan
    Write-SetupLog "=== [$Number/$Of] $Title"
}

function Write-Detail {
    param([Parameter(Mandatory = $true)][string]$Message)
    Write-Host "      $Message"
    Write-SetupLog "      $Message"
}

function Write-Ok {
    param([Parameter(Mandatory = $true)][string]$Message)
    Write-Host "  ok  $Message" -ForegroundColor Green
    Write-SetupLog "  ok  $Message"
}

function Write-Note {
    param([Parameter(Mandatory = $true)][string]$Message)
    Write-Host "  --  $Message" -ForegroundColor Yellow
    Write-SetupLog "  --  $Message"
}

function Write-Skip {
    param([Parameter(Mandatory = $true)][string]$Message)
    Write-Host "  ok  $Message" -ForegroundColor DarkGray
    Write-SetupLog "  skip  $Message"
}

function Stop-Setup {
    <# The only way this script is allowed to give up.

       Every call names the concrete problem and the next action, both in
       language someone who is not a developer can act on. setup.ps1 catches
       this, prints it as the last thing on screen, and exits non-zero - it
       never continues past a step that failed. #>
    param(
        [Parameter(Mandatory = $true)][string]$Problem,
        [Parameter(Mandatory = $true)][string]$NextAction
    )
    $global:DictateSetupProblem = New-Object psobject -Property @{
        Problem    = $Problem
        NextAction = $NextAction
    }
    Write-SetupLog "STOP: $Problem"
    Write-SetupLog "NEXT: $NextAction"
    throw $Problem
}

function Get-NativeOutputLine {
    <# One line of a native program's output, as the program wrote it.

       This exists because of what `2>&1` does to a native command's stderr:
       every line arrives as an ErrorRecord whose Exception is a RemoteException
       carrying the line as its Message. `$record.ToString()` gives that Message
       back - EXCEPT when the line is blank. Then the Message is empty, and
       ErrorRecord.ToString() falls through to Exception.ToString(), which for an
       exception that was never thrown is nothing but its type name. So a blank
       line on stderr came out as:

           System.Management.Automation.RemoteException

       and that is what the product owner was shown, twice, in the middle of an
       install report - once for each blank line dictate printed around its own
       error message. The message itself was there; the two blanks framing it
       were not blank.

       Reading the Message directly is the fix: an empty line stays an empty
       line. ErrorDetails comes first because that is what PowerShell itself
       prefers when a cmdlet has supplied a friendlier message. #>
    param([Parameter(Mandatory = $false)][AllowNull()][object]$Item)
    if ($null -eq $Item) { return '' }
    if ($Item -is [System.Management.Automation.ErrorRecord]) {
        if ($Item.ErrorDetails -and $Item.ErrorDetails.Message) {
            return [string]$Item.ErrorDetails.Message
        }
        if ($Item.Exception) { return [string]$Item.Exception.Message }
        return ''
    }
    return [string]$Item
}

function Invoke-Tool {
    <# Run an external program, keep every line of its output in the log, and
       give back its exit code.

       This exists because of a trap in Windows PowerShell: with
       $ErrorActionPreference = 'Stop', a program writing an ordinary progress
       line to stderr - which git, cmake and pip all do constantly - can be
       turned into a terminating error and stop the install for no reason. So
       the preference is relaxed for the duration of the call and the exit code
       is checked instead, which is the only thing that actually says whether
       the program worked.

       -Show echo   prints the program's output as it goes (use it for the long
                    steps, so the screen is not silent for ten minutes)
       -Show log    keeps it to the log file only #>
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @(),
        [ValidateSet('echo', 'log')][string]$Show = 'log'
    )
    $name = Split-Path -Leaf $FilePath
    if (-not (Test-Path -LiteralPath $FilePath) -and
        -not (Get-Command $FilePath -ErrorAction SilentlyContinue)) {
        # Without this, a missing program would leave $LASTEXITCODE holding
        # whatever the PREVIOUS command set - very often zero - and the step
        # would sail past a failure. 9009 is what the Windows shell itself
        # returns for a command it cannot find.
        Write-SetupLog "not found: $FilePath"
        return @{ ExitCode = 9009; Output = "$name was not found on this PC." }
    }
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $lines = New-Object System.Collections.Generic.List[string]
    try {
        Write-SetupLog "run: $FilePath $($Arguments -join ' ')"
        # Start from a known value so a stale one can never be read back as
        # success if the call itself produces no exit code.
        $global:LASTEXITCODE = 0
        & $FilePath @Arguments 2>&1 | ForEach-Object {
            # Get-NativeOutputLine, never .ToString(): the latter turns a BLANK
            # stderr line into the text "System.Management.Automation.Remote-
            # Exception", which is how a .NET type name ended up on the product
            # owner's screen in the middle of an install report.
            $text = Get-NativeOutputLine $_
            $lines.Add($text)
            Write-SetupLog "${name}: $text"
            if ($Show -eq 'echo') { Write-Host "      $text" }
        }
        $code = $LASTEXITCODE
        if ($null -eq $code) { $code = 0 }
        return @{ ExitCode = $code; Output = ($lines -join [Environment]::NewLine) }
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Assert-ExitCode {
    <# Native commands do not raise on failure in PowerShell, so every one of
       them is checked by hand. #>
    param(
        [Parameter(Mandatory = $true)][int]$Code,
        [Parameter(Mandatory = $true)][string]$Problem,
        [Parameter(Mandatory = $true)][string]$NextAction
    )
    if ($Code -ne 0) {
        Stop-Setup -Problem "$Problem (it stopped with error code $Code)" -NextAction $NextAction
    }
}

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

function Test-IsAdministrator {
    try {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $principal = New-Object Security.Principal.WindowsPrincipal($identity)
        return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch {
        return $false
    }
}

function Update-SessionEnvironment {
    <# An installer writes PATH and VULKAN_SDK into the registry, but this
       already-running PowerShell keeps the copy it started with. That is the
       whole reason the manual instructions say "close and reopen PowerShell".
       Re-reading them here is what lets the install run start to finish in one
       window. #>
    $names = @('VULKAN_SDK', 'VK_SDK_PATH', 'CMAKE_PREFIX_PATH')
    foreach ($name in $names) {
        $machine = [Environment]::GetEnvironmentVariable($name, 'Machine')
        $user = [Environment]::GetEnvironmentVariable($name, 'User')
        $value = $machine
        if ($user) { $value = $user }
        if ($value) { Set-Item -Path "env:$name" -Value $value }
    }
    # Merge rather than replace: the registry has the newly installed tools, and
    # this session may have entries of its own that must not be thrown away.
    $seen = New-Object System.Collections.Generic.HashSet[string]
    $merged = New-Object System.Collections.Generic.List[string]
    foreach ($scope in @('Machine', 'User')) {
        $registry = [Environment]::GetEnvironmentVariable('PATH', $scope)
        if (-not $registry) { continue }
        foreach ($entry in ($registry -split ';')) {
            if ($entry -and $seen.Add($entry.TrimEnd('\').ToLowerInvariant())) { $merged.Add($entry) }
        }
    }
    if ($env:PATH) {
        foreach ($entry in ($env:PATH -split ';')) {
            if ($entry -and $seen.Add($entry.TrimEnd('\').ToLowerInvariant())) { $merged.Add($entry) }
        }
    }
    if ($merged.Count -gt 0) {
        $env:PATH = ($merged -join ';')
    }
    Write-SetupLog "refreshed PATH and VULKAN_SDK from the registry"
}

function Get-FreeSpaceGb {
    <# Free space on the drive a path lives on, in GB. Returns -1 when it
       cannot be worked out, so callers can say "could not check" rather than
       guessing. #>
    param([Parameter(Mandatory = $true)][string]$Path)
    try {
        $full = [System.IO.Path]::GetFullPath($Path)
        $root = [System.IO.Path]::GetPathRoot($full)
        $drive = New-Object System.IO.DriveInfo($root)
        return [math]::Round($drive.AvailableFreeSpace / 1GB, 1)
    } catch {
        Write-SetupLog "free space check failed for ${Path}: $($_.Exception.Message)"
        return -1
    }
}

function Format-Bytes {
    param([Parameter(Mandatory = $true)][double]$Bytes)
    if ($Bytes -ge 1GB) { return ('{0:N2} GB' -f ($Bytes / 1GB)) }
    if ($Bytes -ge 1MB) { return ('{0:N0} MB' -f ($Bytes / 1MB)) }
    if ($Bytes -ge 1KB) { return ('{0:N0} KB' -f ($Bytes / 1KB)) }
    return ('{0:N0} bytes' -f $Bytes)
}

function Format-Duration {
    param([Parameter(Mandatory = $true)][double]$Seconds)
    if ($Seconds -lt 1) { return 'a moment' }
    if ($Seconds -lt 90) { return ('{0:N0} seconds' -f $Seconds) }
    $minutes = [math]::Round($Seconds / 60)
    if ($minutes -lt 90) { return ('{0} minutes' -f $minutes) }
    return ('{0:N1} hours' -f ($Seconds / 3600))
}

# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------

function ConvertTo-VersionOrNull {
    <# Pull the first dotted number out of a tool's --version banner.
       "cmake version 3.28.1" -> 3.28.1 ; anything unparseable -> $null. #>
    param([string]$Text)
    if (-not $Text) { return $null }
    $m = [regex]::Match($Text, '(\d+)\.(\d+)(\.(\d+))?(\.(\d+))?')
    if (-not $m.Success) { return $null }
    $parts = @($m.Groups[1].Value, $m.Groups[2].Value)
    if ($m.Groups[4].Success) { $parts += $m.Groups[4].Value }
    if ($m.Groups[6].Success) { $parts += $m.Groups[6].Value }
    try { return [version]($parts -join '.') } catch { return $null }
}

function Test-VersionAtLeast {
    param([string]$Found, [Parameter(Mandatory = $true)][string]$Minimum)
    $have = ConvertTo-VersionOrNull $Found
    if ($null -eq $have) { return $false }
    return $have -ge ([version]$Minimum)
}

function Get-ToolVersion {
    <# Run `<tool> --version` and return the version, or $null if the tool is
       not on PATH or would not run. Never throws. #>
    param(
        [Parameter(Mandatory = $true)][string]$Command,
        [string[]]$Arguments = @('--version')
    )
    $found = Get-Command $Command -ErrorAction SilentlyContinue
    if (-not $found) { return $null }
    $run = Invoke-Tool -FilePath $Command -Arguments $Arguments
    if ($run.ExitCode -ne 0) {
        Write-SetupLog "$Command $($Arguments -join ' ') exited $($run.ExitCode)"
        return $null
    }
    return ConvertTo-VersionOrNull $run.Output
}

function Get-LastLine {
    param([string]$Text)
    if (-not $Text) { return '' }
    $lines = @($Text -split "`r?`n" | Where-Object { $_.Trim() })
    if ($lines.Count -eq 0) { return '' }
    return $lines[$lines.Count - 1].Trim()
}

function Get-PythonCommand {
    <# Find a real Python of at least $MinimumVersion.

       Windows ships a fake `python.exe` under WindowsApps that does nothing but
       open the Microsoft Store. Treating that as an install is a classic way to
       get a baffling failure three steps later, so it is filtered out here.

       Returns a hashtable @{ Path = ...; Version = ... } or $null. #>
    param([string]$MinimumVersion = '3.11')

    $candidates = New-Object System.Collections.Generic.List[string]
    foreach ($name in @('python', 'python3')) {
        foreach ($cmd in @(Get-Command $name -All -ErrorAction SilentlyContinue)) {
            if ($cmd.CommandType -eq 'Application' -and $cmd.Source) { $candidates.Add($cmd.Source) }
        }
    }
    # The launcher knows about installs that are not on PATH at all.
    $py = Get-Command 'py' -ErrorAction SilentlyContinue
    if ($py) {
        $probe = Invoke-Tool -FilePath 'py' -Arguments @('-3', '-c', 'import sys; print(sys.executable)')
        if ($probe.ExitCode -eq 0) {
            $path = Get-LastLine $probe.Output
            if ($path) { $candidates.Add($path) }
        }
    }

    foreach ($path in $candidates) {
        if ($path -like '*\WindowsApps\*') {
            Write-SetupLog "ignoring the Microsoft Store stub at $path"
            continue
        }
        if (-not (Test-Path -LiteralPath $path)) { continue }
        $probe = Invoke-Tool -FilePath $path -Arguments @('-c', "import sys; print('%d.%d.%d' % sys.version_info[:3])")
        if ($probe.ExitCode -ne 0) { continue }
        $version = ConvertTo-VersionOrNull (Get-LastLine $probe.Output)
        if ($null -ne $version -and $version -ge ([version]$MinimumVersion)) {
            return @{ Path = $path; Version = $version }
        }
    }
    return $null
}

function Get-VisualStudioCppPath {
    <# The C++ build tools, found the way Microsoft's own tooling finds them.
       Returns the install path, or $null. #>
    $programFiles = ${env:ProgramFiles(x86)}
    if (-not $programFiles) { $programFiles = $env:ProgramFiles }
    if (-not $programFiles) { return $null }
    $vswhere = Join-Path $programFiles 'Microsoft Visual Studio\Installer\vswhere.exe'
    if (-not (Test-Path -LiteralPath $vswhere)) { return $null }
    $run = Invoke-Tool -FilePath $vswhere -Arguments @('-products', '*', '-latest',
        '-requires', 'Microsoft.VisualStudio.Component.VC.Tools.x86.x64',
        '-property', 'installationPath')
    if ($run.ExitCode -ne 0) { return $null }
    $path = Get-LastLine $run.Output
    if ($path -and (Test-Path -LiteralPath $path)) { return $path }
    return $null
}

# ---------------------------------------------------------------------------
# The Vulkan SDK
#
# Finding it is deliberately NOT "is VULKAN_SDK set?". That variable is written
# by the SDK's own installer and only published to processes Windows starts
# afterwards, so an unset variable means one of two completely different things:
#
#   the SDK is installed and this window has not been told   -> carry on
#   the SDK is not installed at all                          -> install it
#
# Those have different remedies, and the installer used to report the second as
# the first: it told the product owner the SDK had installed and to restart his
# PC. Restarting cannot conjure up software that was never installed, so that
# message was an infinite loop as well as a lie. Everything below exists to tell
# the two states apart by looking for the SDK itself.
# ---------------------------------------------------------------------------

function Test-VulkanSdkDir {
    <# Is this directory an actual Vulkan SDK, rather than a leftover folder or
       a stale variable pointing at nothing?

       Judged by the two files CMake's FindVulkan has to locate for the
       whisper.cpp build to configure at all, so a directory that passes this is
       one the build can really use. #>
    param([string]$Path)
    if (-not $Path) { return $false }
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) { return $false }
    foreach ($needed in @('Include\vulkan\vulkan.h', 'Lib\vulkan-1.lib')) {
        if (-not (Test-Path -LiteralPath (Join-Path $Path $needed))) { return $false }
    }
    return $true
}

function Test-VulkanShaderCompiler {
    <# whisper.cpp compiles its own shaders, so the SDK also has to carry glslc.
       Separate from Test-VulkanSdkDir because a headers-and-library SDK is
       still a real SDK - it just cannot build this - and the two get different
       messages. #>
    param([string]$Path)
    if (-not $Path) { return $false }
    return (Test-Path -LiteralPath (Join-Path $Path 'Bin\glslc.exe'))
}

function Get-DefaultVulkanSearchRoots {
    <# Where LunarG's installer puts the SDK when nobody tells it otherwise:
       "The default SDK install location is C:\VulkanSDK\<version>"
       (vulkan.lunarg.com/doc/view/latest/windows/getting_started.html). #>
    $roots = New-Object System.Collections.Generic.List[string]
    $seen = New-Object System.Collections.Generic.HashSet[string]
    foreach ($drive in @($env:SystemDrive, 'C:')) {
        if (-not $drive) { continue }
        # Built by hand rather than with Join-Path: Join-Path asks the provider
        # to resolve "C:", so a drive letter that does not exist on the machine
        # running this is an error rather than a path that simply does not
        # exist. That difference stops these functions being exercised anywhere
        # but Windows, and there is nothing to work out here anyway.
        $root = $drive.TrimEnd('\') + '\VulkanSDK'
        if ($seen.Add($root.ToLowerInvariant())) { $roots.Add($root) }
    }
    # The leading comma keeps this an array. Without it PowerShell unrolls a
    # one-element result into a bare string on the way out, and callers that
    # index or count it get the wrong answer.
    return , $roots.ToArray()
}

function Get-VulkanSdkFromDisk {
    <# The SDK as it sits on disk, whatever Windows has or has not published.
       Newest version wins; a folder that is not really an SDK is ignored. #>
    param([string[]]$SearchRoots = @())
    if (-not $SearchRoots -or $SearchRoots.Count -eq 0) { $SearchRoots = Get-DefaultVulkanSearchRoots }

    $best = ''
    $bestVersion = $null
    foreach ($root in $SearchRoots) {
        if (-not $root) { continue }
        if (-not (Test-Path -LiteralPath $root -PathType Container)) { continue }
        foreach ($dir in @(Get-ChildItem -LiteralPath $root -Directory -ErrorAction SilentlyContinue)) {
            if (-not (Test-VulkanSdkDir $dir.FullName)) { continue }
            $version = ConvertTo-VersionOrNull $dir.Name
            if ($null -eq $bestVersion) {
                # Either the first hit, or the first one with a version number -
                # a versioned folder always beats an unversioned one like
                # "current", because that is the one the installer made.
                if (-not $best -or $null -ne $version) {
                    $best = $dir.FullName
                    $bestVersion = $version
                }
            } elseif ($null -ne $version -and $version -gt $bestVersion) {
                $best = $dir.FullName
                $bestVersion = $version
            }
        }
    }
    if ($best) { return $best }
    return $null
}

function Get-VulkanSdkFromRegistry {
    <# The variable as Windows has recorded it, rather than as this window has
       it. This is the re-read that was already here, kept as one source among
       several instead of being the only one. VK_SDK_PATH is checked too: the
       SDK sets both. #>
    foreach ($name in @('VULKAN_SDK', 'VK_SDK_PATH')) {
        foreach ($scope in @('Machine', 'User')) {
            $value = ''
            try {
                $value = [Environment]::GetEnvironmentVariable($name, $scope)
            } catch {
                Write-SetupLog "could not read $name from the $scope environment: $($_.Exception.Message)"
                continue
            }
            if (Test-VulkanSdkDir $value) { return $value }
        }
    }
    return $null
}

function Get-VulkanSdkFromInstalledPrograms {
    <# Where the SDK's own entry in Windows' installed-programs list says it put
       itself. This is what finds an SDK installed somewhere other than the
       default folder, which no amount of scanning C:\VulkanSDK ever will. #>
    $keys = @(
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall',
        'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall'
    )
    foreach ($key in $keys) {
        if (-not (Test-Path -LiteralPath $key)) { continue }
        $entries = @()
        try {
            $entries = @(Get-ChildItem -LiteralPath $key -ErrorAction SilentlyContinue)
        } catch {
            Write-SetupLog "could not read ${key}: $($_.Exception.Message)"
            continue
        }
        foreach ($entry in $entries) {
            $item = $null
            try {
                $item = Get-ItemProperty -LiteralPath $entry.PSPath -ErrorAction SilentlyContinue
            } catch {
                continue
            }
            if (-not $item) { continue }
            $name = ''
            if ($item.PSObject.Properties.Match('DisplayName').Count -gt 0) { $name = [string]$item.DisplayName }
            if ($name -notmatch '(?i)vulkan.*sdk') { continue }
            $location = ''
            if ($item.PSObject.Properties.Match('InstallLocation').Count -gt 0) { $location = [string]$item.InstallLocation }
            if (Test-VulkanSdkDir $location) { return $location.TrimEnd('\') }
        }
    }
    return $null
}

function Resolve-VulkanSdk {
    <# Is there a Vulkan SDK on this PC, and where?

       Returns @{ Path; Source; How } or $null. `How` is the phrase the
       messages use, so what the product owner is told matches how it was
       actually found.

       -Sources exists so each route can be exercised on its own; the default is
       all four, in the order that answers fastest. #>
    param(
        [string[]]$SearchRoots = @(),
        [ValidateSet('session', 'registry', 'disk', 'programs')]
        [string[]]$Sources = @('session', 'registry', 'disk', 'programs')
    )
    foreach ($source in $Sources) {
        $path = $null
        $how = ''
        switch ($source) {
            'session' {
                foreach ($value in @($env:VULKAN_SDK, $env:VK_SDK_PATH)) {
                    if (-not $path -and (Test-VulkanSdkDir $value)) { $path = $value }
                }
                $how = 'the VULKAN_SDK setting this window already had'
            }
            'registry' {
                $path = Get-VulkanSdkFromRegistry
                $how = 'the VULKAN_SDK setting Windows has recorded'
            }
            'disk' {
                $path = Get-VulkanSdkFromDisk -SearchRoots $SearchRoots
                $how = 'the folder it is installed in'
            }
            'programs' {
                $path = Get-VulkanSdkFromInstalledPrograms
                $how = "Windows' own list of installed programs"
            }
        }
        if ($path) {
            $path = ([string]$path).TrimEnd('\')
            Write-SetupLog "Vulkan SDK found via ${source}: $path"
            return @{ Path = $path; Source = $source; How = $how }
        }
    }
    Write-SetupLog ('no Vulkan SDK found; looked in ' + (($Sources) -join ', '))
    return $null
}

function Set-VulkanSdkForSession {
    <# Point this running window at an SDK that is already on disk.

       This is exactly what Windows does for processes it starts after the SDK
       is installed - set VULKAN_SDK, set VK_SDK_PATH, put its Bin folder on
       PATH. Doing it here is what turns "restart the PC and run setup again"
       into "carry on", and it is why the install no longer has a step that can
       only be got past by rebooting. #>
    param([Parameter(Mandatory = $true)][string]$Path)
    $clean = $Path.TrimEnd('\')
    $env:VULKAN_SDK = $clean
    $env:VK_SDK_PATH = $clean
    $bin = Join-Path $clean 'Bin'
    if (Test-Path -LiteralPath $bin) {
        $already = @(($env:PATH -split ';') | Where-Object { $_ -and $_.TrimEnd('\') -ieq $bin })
        if ($already.Count -eq 0) { $env:PATH = $bin + ';' + $env:PATH }
    }
    Write-SetupLog "VULKAN_SDK set for this session: $clean"
}

# ---------------------------------------------------------------------------
# winget: what it actually said, in words that can be shown to someone
# ---------------------------------------------------------------------------

function Format-ExitCode {
    <# winget documents its failures in hex and PowerShell hands them back as a
       signed number, so neither on its own can be looked up. Show both. #>
    param([Parameter(Mandatory = $true)][int]$Code)
    return ('{0} (0x{1:X8})' -f $Code, $Code)
}

function Get-WingetFailureSummary {
    <# The one line of winget's output that says why it failed, with its
       progress bars, banners and licence boilerplate dropped. #>
    param([string]$Output)
    if (-not $Output) { return '' }

    $interesting = New-Object System.Collections.Generic.List[string]
    foreach ($line in ($Output -split "`r?`n")) {
        $text = $line.Trim()
        if (-not $text) { continue }
        # Progress bars and percentages carry no letters and say nothing.
        if ($text -notmatch '[A-Za-z]') { continue }
        if ($text -match '^(Found |Downloading |Successfully verified|Starting package install|This application is licensed|The publisher|You agree)') { continue }
        $interesting.Add($text)
    }
    if ($interesting.Count -eq 0) { return '' }

    $telling = '(?i)(no package found|no applicable|no sources|could not|cannot|not found|fail|error|abandon|cancel|declin|requires|restart|reboot)'
    for ($i = $interesting.Count - 1; $i -ge 0; $i--) {
        if ($interesting[$i] -match $telling) { return $interesting[$i] }
    }
    return $interesting[$interesting.Count - 1]
}

function Get-WingetFailureNote {
    <# One sentence naming the tool that did not install and the evidence for
       it, said at the install that failed rather than inferred from a symptom
       several steps later. #>
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Id,
        [Parameter(Mandatory = $true)][int]$ExitCode,
        [string]$Said = ''
    )
    $text = "$Name did not install. winget was asked for $Id and stopped with " + (Format-ExitCode $ExitCode) + '.'
    if ($Said) { $text += " It said: $Said" }
    if ($Said -match '(?i)no package found') {
        $text += " That is winget saying it has no package by that name at all, so the identifier is wrong or this PC's winget source does not carry it."
    }
    return $text
}

# ---------------------------------------------------------------------------
# Installing the Vulkan SDK when winget cannot
# ---------------------------------------------------------------------------

# LunarG publish these two endpoints themselves, on the page that documents how
# to script an SDK download (vulkan.lunarg.com/content/view/latest-sdk-version-api):
#   the current version, as plain text
#   the Windows installer for a given version, or for "latest"
$script:LunarGVersionUri = 'https://vulkan.lunarg.com/sdk/latest/windows.txt'
$script:LunarGDownloadPage = 'https://vulkan.lunarg.com/sdk/home#windows'

function Get-LunarGSdkVersionUri { return $script:LunarGVersionUri }
function Get-LunarGSdkDownloadPage { return $script:LunarGDownloadPage }

function Get-LunarGSdkInstallerUri {
    param([string]$Version = '')
    if ($Version) { return "https://sdk.lunarg.com/sdk/download/$Version/windows/vulkan_sdk.exe" }
    return 'https://sdk.lunarg.com/sdk/download/latest/windows/vulkan_sdk.exe'
}

function ConvertTo-LunarGSdkVersion {
    <# LunarG's version endpoint answers with the bare version and a newline.
       Anything else - an error page, a redirect, HTML - is not a version, and
       saying so gets the "latest" URL used instead of a nonsense one. #>
    param([string]$Text)
    if (-not $Text) { return '' }
    $trimmed = $Text.Trim()
    if ($trimmed -match '^\d+\.\d+\.\d+(\.\d+)?$') { return $trimmed }
    return ''
}

function Test-SignerSubject {
    param([string]$Subject, [string]$Expected)
    if (-not $Subject) { return $false }
    if (-not $Expected) { return $true }
    return ($Subject -like "*$Expected*")
}

function Test-InstallerSignature {
    <# Before running a downloaded installer as administrator, ask Windows
       whether it really came from who it claims and arrived whole. A truncated
       download fails this too, which is what stands in for the SHA-256 that a
       "latest" URL cannot have.

       Returns @{ Ok; Reason; Subject } and never throws. #>
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [string]$ExpectedSubject = ''
    )
    if (-not (Test-Path -LiteralPath $Path)) {
        return @{ Ok = $false; Reason = 'the downloaded file is not there'; Subject = '' }
    }
    $signature = $null
    try {
        $signature = Get-AuthenticodeSignature -LiteralPath $Path -ErrorAction Stop
    } catch {
        Write-SetupLog "signature check failed for ${Path}: $($_.Exception.Message)"
        return @{ Ok = $false; Reason = "Windows could not check the file's signature"; Subject = '' }
    }
    $status = [string]$signature.Status
    $subject = ''
    if ($signature.SignerCertificate) { $subject = [string]$signature.SignerCertificate.Subject }
    if ($status -ne 'Valid') {
        return @{ Ok = $false; Reason = "Windows reports its signature as $status"; Subject = $subject }
    }
    if (-not (Test-SignerSubject -Subject $subject -Expected $ExpectedSubject)) {
        return @{ Ok = $false; Reason = "it is signed, but by $subject rather than $ExpectedSubject"; Subject = $subject }
    }
    return @{ Ok = $true; Reason = "signed by $subject, checked by Windows"; Subject = $subject }
}

function Install-VulkanSdkFromLunarG {
    <# The route that works when winget cannot install the SDK - which is the
       situation that produced this whole function, winget having had no package
       called LunarG.VulkanSDK to install.

       Returns @{ Ok; Path; Reason } and never throws: "that did not work
       either" is one part of a larger message, and the caller is the one that
       knows the rest of it. #>
    param(
        [Parameter(Mandatory = $true)][string]$WorkDir,
        [string[]]$SearchRoots = @()
    )

    # -- Which version is current, from LunarG's own version endpoint --------
    $version = ''
    try {
        $answer = Invoke-WebRequest -Uri (Get-LunarGSdkVersionUri) -UseBasicParsing -TimeoutSec 30
        $version = ConvertTo-LunarGSdkVersion ([string]$answer.Content)
    } catch {
        Write-SetupLog "LunarG version lookup failed: $($_.Exception.Message)"
    }
    $uri = Get-LunarGSdkInstallerUri -Version $version
    if ($version) {
        Write-Detail "Downloading the Vulkan SDK $version straight from LunarG."
    } else {
        Write-Detail 'Downloading the current Vulkan SDK straight from LunarG.'
    }
    Write-SetupLog "LunarG installer URI: $uri"

    if (-not (Test-Path -LiteralPath $WorkDir)) {
        New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
    }
    $suffix = ''
    if ($version) { $suffix = "-$version" }
    $installer = Join-Path $WorkDir "vulkan-sdk$suffix.exe"

    # -- Download it, and refuse to run anything Windows will not vouch for ---
    # Two goes: the first can inherit a part-finished file from an earlier run,
    # and the signature check is what catches that.
    $verified = $false
    for ($attempt = 1; $attempt -le 2; $attempt++) {
        try {
            Get-FileResumable -Uri $uri -Destination $installer -Label 'Vulkan SDK installer' -MaxAttempts 3 | Out-Null
        } catch {
            return @{ Ok = $false; Path = ''
                Reason = "the download from LunarG did not finish ($($_.Exception.Message))"
            }
        }
        $signature = Test-InstallerSignature -Path $installer -ExpectedSubject 'LunarG'
        Write-SetupLog "LunarG installer signature: $($signature.Reason)"
        if ($signature.Ok) {
            Write-Detail "The download is $($signature.Reason)."
            $verified = $true
            break
        }
        Remove-Item -LiteralPath $installer -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath "$installer.part" -Force -ErrorAction SilentlyContinue
        if ($attempt -ge 2) {
            return @{ Ok = $false; Path = ''
                Reason = "the file that came back from LunarG could not be confirmed as theirs ($($signature.Reason)), so setup would not run it"
            }
        }
        Write-Note 'That download did not check out as LunarG''s own file. Fetching it once more.'
    }
    if (-not $verified) {
        return @{ Ok = $false; Path = ''; Reason = 'the installer could not be downloaded and verified' }
    }

    # -- Run it, with the switches LunarG's installer documents ---------------
    # Start-Process, not Invoke-Tool: this is a windowed program, and PowerShell
    # does not wait for one of those to finish when it is called directly. It
    # would look like an instant success and the check below would then be
    # racing the installer.
    Write-Detail 'Installing it. This takes a few minutes and asks nothing.'
    $code = -1
    try {
        $process = Start-Process -FilePath $installer -Wait -PassThru -ArgumentList @(
            'install', '--accept-licenses', '--default-answer', '--confirm-command')
        $code = $process.ExitCode
    } catch {
        return @{ Ok = $false; Path = ''
            Reason = "LunarG's installer would not start ($($_.Exception.Message))"
        }
    }
    Write-SetupLog "LunarG installer exit code: $(Format-ExitCode $code)"

    Update-SessionEnvironment
    $found = Resolve-VulkanSdk -SearchRoots $SearchRoots
    if ($found) { return @{ Ok = $true; Path = $found.Path; Reason = '' } }
    return @{ Ok = $false; Path = ''
        Reason = ("LunarG's installer ran and stopped with " + (Format-ExitCode $code) +
            ', and there is still no Vulkan SDK on this PC afterwards')
    }
}

function Get-VulkanSdkMissingReport {
    <# The words for the one state that used to be reported as its exact
       opposite: there is no Vulkan SDK on this PC.

       Returns @{ Problem; NextAction }. It never says the SDK installed, and
       the next action is one a non-developer can carry out and that can
       actually work - unlike "restart the PC", which cannot install software
       that was never downloaded. #>
    param(
        [string]$WingetId = '',
        [int]$WingetExitCode = 0,
        [string]$WingetSaid = '',
        [string]$DirectReason = '',
        [string[]]$SearchedRoots = @()
    )
    if (-not $SearchedRoots -or $SearchedRoots.Count -eq 0) { $SearchedRoots = Get-DefaultVulkanSearchRoots }

    $lines = New-Object System.Collections.Generic.List[string]
    # Only claim an install was attempted if one was. Step 3 calls this without
    # having tried anything, and "setup could not install it" would be as untrue
    # there as the message this replaced.
    if ($WingetId -or $DirectReason) {
        $lines.Add('The Vulkan SDK is not installed on this PC, and setup could not install it.')
    } else {
        $lines.Add('The Vulkan SDK is not installed on this PC.')
    }
    $lines.Add('')
    $lines.Add('Setup looked in all of these, and it is in none of them:')
    foreach ($root in $SearchedRoots) { $lines.Add("  the folder $root") }
    $lines.Add('  the VULKAN_SDK setting Windows has recorded')
    $lines.Add("  Windows' own list of installed programs")
    if ($WingetId) {
        $lines.Add('')
        $lines.Add((Get-WingetFailureNote -Name 'The Vulkan SDK' -Id $WingetId -ExitCode $WingetExitCode -Said $WingetSaid))
    }
    if ($DirectReason) {
        $lines.Add('')
        $lines.Add("Downloading it straight from LunarG instead did not work: $DirectReason.")
    }

    $next = @"
Install it by hand. It is one download and an ordinary Next-Next-Finish
installer, and it takes about five minutes:

  1. Open this page:
       $(Get-LunarGSdkDownloadPage)
  2. Under Windows, click the SDK Installer download. The file is called
     vulkan_sdk.exe, or vulkansdk-windows-X64-<version>.exe.
  3. Run it and accept everything it offers. It installs itself into
     $($SearchedRoots[0])\<version>.
  4. Run this setup again.

You do NOT need to restart the PC. Setup now finds the SDK by looking for the
folder it is in, so it picks it up in the same window.

If that page will not open, the same file is at:
  $(Get-LunarGSdkInstallerUri)

Everything already installed and downloaded is kept.
"@

    return @{ Problem = ($lines -join [Environment]::NewLine); NextAction = $next.Trim() }
}

# ---------------------------------------------------------------------------
# Downloads: resumable, verified, with a progress line
# ---------------------------------------------------------------------------

function Get-Sha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Test-FileIntegrity {
    <# Is this file the exact file we meant to download?

       Size first (it is free and catches the truncated-download case that looks
       like "the app is broken"), then SHA-256. A `.sha256` sidecar records a
       hash we have already checked, so re-running setup does not re-hash 1.9 GB
       every time; the size is still re-checked, and any mismatch re-hashes. #>
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [long]$ExpectedBytes = 0,
        [string]$ExpectedSha256 = '',
        [switch]$Rehash
    )
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    $size = (Get-Item -LiteralPath $Path).Length
    if ($ExpectedBytes -gt 0 -and $size -ne $ExpectedBytes) { return $false }
    if (-not $ExpectedSha256) { return $true }

    $sidecar = "$Path.sha256"
    if (-not $Rehash -and (Test-Path -LiteralPath $sidecar)) {
        $recorded = (Get-Content -LiteralPath $sidecar -Raw -ErrorAction SilentlyContinue)
        if ($recorded) {
            $fields = $recorded.Trim() -split '\s+'
            if ($fields.Count -ge 2 -and $fields[0] -eq $ExpectedSha256.ToLowerInvariant() `
                    -and $fields[1] -eq [string]$size) {
                return $true
            }
        }
    }
    $actual = Get-Sha256 -Path $Path
    if ($actual -ne $ExpectedSha256.ToLowerInvariant()) {
        Write-SetupLog "sha256 mismatch for ${Path}: got $actual, expected $ExpectedSha256"
        return $false
    }
    Set-Content -LiteralPath $sidecar -Value "$actual $size" -Encoding ASCII
    return $true
}

function Write-DownloadProgress {
    param(
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][long]$Done,
        [long]$Total = 0,
        [double]$ElapsedSeconds = 0
    )
    $rate = 0
    if ($ElapsedSeconds -gt 0.5) { $rate = $Done / $ElapsedSeconds }
    $text = "      $Label  " + (Format-Bytes $Done)
    if ($Total -gt 0) {
        $percent = [math]::Floor(100 * $Done / $Total)
        $text = "      $Label  $percent%  " + (Format-Bytes $Done) + ' of ' + (Format-Bytes $Total)
    }
    if ($rate -gt 0) {
        $text += '   ' + (Format-Bytes $rate) + '/s'
        if ($Total -gt $Done) {
            $text += '   about ' + (Format-Duration (($Total - $Done) / $rate)) + ' left'
        }
    }
    if ([Console]::IsOutputRedirected) {
        Write-Host $text
    } else {
        Write-Host ("`r" + $text.PadRight(78)) -NoNewline
    }
}

function Get-FileResumable {
    <# Download $Uri to $Destination, surviving a dropped connection.

       Bytes land in "<destination>.part". If the connection dies, the next
       attempt asks the server for a Range starting at whatever arrived, so a
       1.6 GB download that fails at 90% resumes at 90% rather than starting
       again. If the server will not do ranges it starts over, which is correct
       but slower, and it says so.

       Nothing is moved into place until both the byte count and the SHA-256
       match. That is the difference between "the model is missing" (obvious)
       and "the model is half a model" (looks like the app is broken). #>
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [Parameter(Mandatory = $true)][string]$Destination,
        [long]$ExpectedBytes = 0,
        [string]$ExpectedSha256 = '',
        [string]$Label = 'download',
        [int]$MaxAttempts = 4,
        [int]$TimeoutSeconds = 120
    )

    if (Test-FileIntegrity -Path $Destination -ExpectedBytes $ExpectedBytes -ExpectedSha256 $ExpectedSha256) {
        return 'already-here'
    }
    if (Test-Path -LiteralPath $Destination) {
        Write-Note "$Label is already there but is not the right file. Downloading it again."
        Remove-Item -LiteralPath $Destination -Force
    }

    $dir = Split-Path -Parent $Destination
    if ($dir -and -not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
    }
    $part = "$Destination.part"

    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    } catch {
        Write-SetupLog "could not pin TLS 1.2: $($_.Exception.Message)"
    }

    $lastError = ''
    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
        $have = 0
        if (Test-Path -LiteralPath $part) { $have = (Get-Item -LiteralPath $part).Length }
        if ($ExpectedBytes -gt 0 -and $have -gt $ExpectedBytes) {
            Write-SetupLog "partial file is larger than expected; starting again"
            Remove-Item -LiteralPath $part -Force
            $have = 0
        }
        if ($have -gt 0) {
            Write-Detail ("resuming $Label from " + (Format-Bytes $have) + ' already downloaded')
        }

        $response = $null
        $stream = $null
        $output = $null
        try {
            $request = [Net.HttpWebRequest][Net.WebRequest]::Create($Uri)
            $request.Timeout = $TimeoutSeconds * 1000
            $request.ReadWriteTimeout = $TimeoutSeconds * 1000
            $request.UserAgent = 'dictate-setup'
            $request.AllowAutoRedirect = $true
            if ($have -gt 0) { $request.AddRange($have) }

            $response = $request.GetResponse()
            $resumed = ($have -gt 0 -and $response.StatusCode -eq [Net.HttpStatusCode]::PartialContent)
            if ($have -gt 0 -and -not $resumed) {
                Write-Note "This server will not resume a part-finished download, so it starts again."
                Remove-Item -LiteralPath $part -Force -ErrorAction SilentlyContinue
                $have = 0
            }
            $total = $have + $response.ContentLength
            if ($ExpectedBytes -gt 0) { $total = $ExpectedBytes }

            $mode = [IO.FileMode]::Create
            if ($resumed) { $mode = [IO.FileMode]::Append }
            $stream = $response.GetResponseStream()
            $output = New-Object IO.FileStream($part, $mode, [IO.FileAccess]::Write, [IO.FileShare]::None)

            $buffer = New-Object byte[] 1048576
            $done = $have
            $clock = [Diagnostics.Stopwatch]::StartNew()
            $lastDraw = -1.0
            while ($true) {
                $read = $stream.Read($buffer, 0, $buffer.Length)
                if ($read -le 0) { break }
                $output.Write($buffer, 0, $read)
                $done += $read
                if ($clock.Elapsed.TotalSeconds - $lastDraw -ge 1.0) {
                    Write-DownloadProgress -Label $Label -Done $done -Total $total `
                        -ElapsedSeconds $clock.Elapsed.TotalSeconds
                    $lastDraw = $clock.Elapsed.TotalSeconds
                }
            }
            $output.Flush()
            Write-DownloadProgress -Label $Label -Done $done -Total $total `
                -ElapsedSeconds $clock.Elapsed.TotalSeconds
        } catch {
            $lastError = $_.Exception.Message
            Write-SetupLog "download attempt ${attempt} failed: $lastError"
        } finally {
            if ($output) { $output.Dispose() }
            if ($stream) { $stream.Dispose() }
            if ($response) { $response.Close() }
        }

        if (-not [Console]::IsOutputRedirected) { Write-Host '' }

        $got = 0
        if (Test-Path -LiteralPath $part) { $got = (Get-Item -LiteralPath $part).Length }

        if ($ExpectedBytes -gt 0 -and $got -ne $ExpectedBytes) {
            if ($attempt -lt $MaxAttempts) {
                if ($got -eq 0) {
                    Write-Note "Could not reach the download site. Trying again (try $($attempt + 1) of $MaxAttempts)."
                } else {
                    Write-Note ("The download stopped early at " + (Format-Bytes $got) + ' of ' +
                        (Format-Bytes $ExpectedBytes) + ". Carrying on from there (try $($attempt + 1) of $MaxAttempts).")
                }
                Start-Sleep -Seconds ([math]::Min(15, 3 * $attempt))
                continue
            }
            if ($got -eq 0) {
                Stop-Setup -Problem "$Label could not be downloaded at all. The last thing that went wrong was: $lastError" `
                    -NextAction ("Check this PC is online - open a web page in a browser to be sure -`n" +
                        "and if you are on a company network or a VPN, that it is not blocking the`n" +
                        "download. Then run setup again; nothing already done is lost.")
            }
            Stop-Setup -Problem ("The download of $Label kept stopping early. " +
                'It got ' + (Format-Bytes $got) + ' of ' + (Format-Bytes $ExpectedBytes) + '.') `
                -NextAction ("This is almost always the internet connection rather than your PC.`n" +
                    "Check you are online, then run setup again - it carries on from where it`n" +
                    "stopped rather than starting the download again.`n" +
                    "The file it was writing is: $part")
        }

        if ($ExpectedSha256) {
            $actual = Get-Sha256 -Path $part
            if ($actual -ne $ExpectedSha256.ToLowerInvariant()) {
                Write-SetupLog "sha256 mismatch on $part : $actual"
                Remove-Item -LiteralPath $part -Force -ErrorAction SilentlyContinue
                if ($attempt -lt $MaxAttempts) {
                    Write-Note "The downloaded file arrived damaged, so it is being fetched again."
                    continue
                }
                Stop-Setup -Problem "$Label downloaded, but it is not the file it should be (the contents do not match the published fingerprint)." `
                    -NextAction ("Something between here and the download site is altering the file -`n" +
                        "usually a proxy, a VPN, or antivirus. Try again on a different network,`n" +
                        "or pause any VPN, then run setup again.")
            }
        }

        if (Test-Path -LiteralPath $Destination) { Remove-Item -LiteralPath $Destination -Force }
        Move-Item -LiteralPath $part -Destination $Destination
        if ($ExpectedSha256) {
            Set-Content -LiteralPath "$Destination.sha256" `
                -Value ($ExpectedSha256.ToLowerInvariant() + ' ' + (Get-Item -LiteralPath $Destination).Length) `
                -Encoding ASCII
        }
        return 'downloaded'
    }

    Stop-Setup -Problem "$Label could not be downloaded. The last thing that went wrong was: $lastError" `
        -NextAction ("Check you are online and run setup again. It resumes rather than`n" +
            "starting over, so nothing already downloaded is wasted.")
}

# ---------------------------------------------------------------------------
# The config file
# ---------------------------------------------------------------------------

function Write-TextFileNoBom {
    <# Write a text file as UTF-8 with NO byte order mark.

       `Set-Content -Encoding UTF8` cannot be used for this. In Windows
       PowerShell 5.1 - the version that ships with Windows, and the one this
       installer runs under - that writes a three-byte mark at the front of the
       file, and Python's TOML parser reports the mark as a syntax error on line
       1. The config would look fine in any editor and dictate would refuse to
       start. #>
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        # A config file has blank lines in it, and a mandatory string array
        # rejects empty elements unless it is told not to.
        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [AllowEmptyCollection()]
        [string[]]$Lines
    )
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllLines($Path, $Lines, $utf8NoBom)
}

function ConvertTo-TomlString {
    <# A Windows path as a TOML string. Literal (single-quoted) form, which
       needs no backslash escaping - unless the value itself contains a single
       quote, which a literal string cannot represent at all, in which case the
       basic form with escapes is used. #>
    param([Parameter(Mandatory = $true)][string]$Value)
    if ($Value -notmatch "'") {
        return "'" + $Value + "'"
    }
    $escaped = $Value.Replace('\', '\\').Replace('"', '\"')
    return '"' + $escaped + '"'
}

function Set-TomlValue {
    <# Change one `key = value` inside one `[section]` of a TOML file, leaving
       every other line - including the comments the product owner may have
       edited around - exactly as it was.

       This is deliberately a line edit and not a parse-and-rewrite: setup owns
       three paths in that file and nothing else, and a re-run must not quietly
       revert a hotkey or a font size he changed. #>
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Section,
        [Parameter(Mandatory = $true)][string]$Key,
        [Parameter(Mandatory = $true)][string]$Value,
        [string]$Comment = ''
    )
    $lines = @(Get-Content -LiteralPath $Path)
    $literal = ConvertTo-TomlString $Value
    $suffix = ''
    if ($Comment) { $suffix = "  # $Comment" }
    $replacement = "$Key = $literal$suffix"

    $out = New-Object System.Collections.Generic.List[string]
    $current = ''
    $written = $false
    $sectionEnd = -1
    foreach ($line in $lines) {
        $trim = $line.Trim()
        if ($trim -match '^\[([^\[\]]+)\]\s*$') {
            $current = $Matches[1].Trim()
        } elseif ($current -eq $Section -and -not $written -and
                  $trim -match ('^' + [regex]::Escape($Key) + '\s*=')) {
            $out.Add($replacement)
            $written = $true
            continue
        }
        $out.Add($line)
        if ($current -eq $Section) { $sectionEnd = $out.Count }
    }

    if (-not $written) {
        if ($sectionEnd -ge 0) {
            $out.Insert($sectionEnd, $replacement)
        } else {
            $out.Add('')
            $out.Add("[$Section]")
            $out.Add($replacement)
        }
    }
    Write-TextFileNoBom -Path $Path -Lines $out.ToArray()
}

function Get-TomlValue {
    <# Read one `key = value` back out of one `[section]`. Only understands the
       quoted-scalar forms this setup writes; returns $null for anything else. #>
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Section,
        [Parameter(Mandatory = $true)][string]$Key
    )
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    $current = ''
    foreach ($line in @(Get-Content -LiteralPath $Path)) {
        $trim = $line.Trim()
        if ($trim -match '^\[([^\[\]]+)\]\s*$') {
            $current = $Matches[1].Trim()
            continue
        }
        if ($current -ne $Section) { continue }
        $m = [regex]::Match($trim, ('^' + [regex]::Escape($Key) + '\s*=\s*(.+?)\s*(#.*)?$'))
        if (-not $m.Success) { continue }
        $raw = $m.Groups[1].Value.Trim()
        if ($raw.StartsWith("'") -and $raw.EndsWith("'") -and $raw.Length -ge 2) {
            return $raw.Substring(1, $raw.Length - 2)
        }
        if ($raw.StartsWith('"') -and $raw.EndsWith('"') -and $raw.Length -ge 2) {
            return $raw.Substring(1, $raw.Length - 2).Replace('\\', '\')
        }
        return $raw
    }
    return $null
}
