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
       hash we have already checked, so re-running setup does not re-hash 1.7 GB
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
            # It used to say this is "almost always the internet connection
            # rather than your PC", which is a guess about whose fault it is.
            # What setup knows is how far it got, how many times, and what the
            # last error was - so that is what it says.
            Stop-Setup -Problem ("The download of $Label kept stopping early. After $MaxAttempts tries " +
                'it had got ' + (Format-Bytes $got) + ' of ' + (Format-Bytes $ExpectedBytes) + '.' +
                $(if ($lastError) { " The last thing that went wrong was: $lastError" } else { '' })) `
                -NextAction ("Setup cannot tell from here whether that is this PC, the network`n" +
                    "between here and the download site, or the site itself. Running setup`n" +
                    "again is worth a try either way - it carries on from where it stopped`n" +
                    "rather than starting the download again - and trying it on a different`n" +
                    "network, or with a VPN paused, is what separates the three.`n" +
                    "The part-finished file it was writing is: $part")
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
                # The established fact is the mismatch, after $MaxAttempts goes.
                # WHAT altered it is not something setup can see, so the three
                # usual suspects are offered as things to try, not asserted.
                Stop-Setup -Problem ("$Label downloaded $MaxAttempts times, and every time the contents " +
                    'came out different from the fingerprint published for it. Setup will not install ' +
                    'a file it cannot verify, so nothing was changed.') `
                    -NextAction ("Setup cannot see what is altering it. In order of how often each`n" +
                        "one turns out to be the answer, try: a different network, pausing any`n" +
                        "VPN, and pausing antivirus for the download. Run setup again after`n" +
                        "each - it starts this download afresh, and everything else already`n" +
                        "done is kept.")
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
# Files another program is holding open
#
# `pip install -e` REPLACES the console-script launcher it wrote last time -
# Scripts\dictate.exe - and Windows will not let it while any process has that
# file open. The product owner's install stopped there:
#
#     OSError: [WinError 5] Access is denied: c:\python311\scripts\dictate.exe
#
# The step before it had run `dictate stop --stale-only`, and that had answered
# "dictate is not running". Both were true. `stop --stale-only` looks at the
# instance lock and the transcription port (src/dictate/recovery.py), and
# neither of those is a handle on a file: the lock is held by the PYTHON process
# and vanishes the moment it dies, while dictate.exe is the separate launcher
# process ABOVE it, and antivirus and the Windows indexer hold newly written
# executables without being dictate at all. A process check can never answer
# "can this file be replaced". Only asking the file can.
#
# So that is what these do: ask the file, wait a few seconds for a transient
# holder to let go, and if it does not, name the holder as precisely as Windows
# is willing to - through the Restart Manager, which is the API Windows gives
# installers for exactly this question.
# ---------------------------------------------------------------------------

#: How long to wait for something holding an install target to let go. The
#: comparable measurement in this project is the port: CI showed a killed
#: process leaves the process list roughly two seconds before Windows finishes
#: releasing its TCP port (recovery.PORT_RELEASE_TIMEOUT_S). A file handle is
#: the same class of gap, and an antivirus scan of a freshly written executable
#: is a little longer, so this is generous by comparison and still short enough
#: that nobody thinks setup has hung.
$script:FileUnlockTimeoutSeconds = 20

# Set-StrictMode 2.0 is on for this file, so both of these are declared here
# rather than sprung into existence on first use inside Add-RestartManagerType.
$script:RestartManagerReady = $false
$script:RestartManagerFailed = $false

function Get-InnerException {
    <# The exception a .NET call actually threw, out of the wrapper PowerShell
       puts round it.

       Calling a .NET method from PowerShell and having it throw gives back a
       MethodInvocationException whose Message begins 'Exception calling "Open"
       with "4" argument(s)' and whose HResult is the wrapper's, not Windows'.
       Both halves of the report here depend on the real one. #>
    param([Parameter(Mandatory = $true)]$ErrorRecord)
    $ex = $ErrorRecord.Exception
    while ($ex -and ($ex -is [System.Management.Automation.MethodInvocationException]) -and
        $ex.InnerException) {
        $ex = $ex.InnerException
    }
    return $ex
}

function Get-Win32ErrorCode {
    <# The Windows error number inside a .NET exception, or 0 if it carries
       none. .NET wraps them as HRESULTs of the form 0x8007xxxx, where xxxx is
       the number Windows itself reported - 5 for access denied, 32 for a
       sharing violation - and those two mean different things to the person
       reading the message, so the number is kept rather than flattened. #>
    param([Parameter(Mandatory = $false)][AllowNull()][object]$Exception)
    if ($null -eq $Exception) { return 0 }
    $hr = 0
    try { $hr = [int]$Exception.HResult } catch { return 0 }
    # HResult is a SIGNED 32-bit value and every 0x8007xxxx code has the top bit
    # set, so it arrives negative. Widen it to 64 bits before masking: the
    # obvious `$hr -band 0xFFFF0000` does not work, because PowerShell reads a
    # hex literal that fits in 32 bits as a signed Int32 and 0xFFFF0000 is
    # -65536 there. These are the same three masks written as decimals, which
    # PowerShell reads as Int64 because they do not fit.
    $wide = ([int64]$hr) -band 4294967295          # 0xFFFFFFFF
    if (($wide -band 4294901760) -eq 2147942400) { # 0xFFFF0000 -eq 0x80070000
        return [int]($wide -band 65535)            # 0xFFFF
    }
    return 0
}

function Test-FileReplaceable {
    <# Can the installer replace this exact file right now?

       Returns @{ Path; Replaceable; Reason; Code; Said }, where Reason is one
       of:
         'absent'  there is no such file, so there is nothing to be held
         'ok'      it opened for writing, so nothing has it
         'held'    another process has it open (a sharing violation)
         'denied'  Windows refused the access itself (permissions, or the
                   read-only attribute, or a filter driver saying no)
         'unknown' something else went wrong; Said carries what

       The probe asks for ReadWrite access while GRANTING ReadWrite sharing.
       That combination is deliberate and is the least strict probe that still
       answers the question: it fails only when some other handle is denying
       write sharing, which is exactly the condition that stops pip. A stricter
       probe (FileShare.None) would also fail for a harmless reader and would
       make setup wait, and then refuse, for an install that would have worked. #>
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return @{ Path = $Path; Replaceable = $true; Reason = 'absent'; Code = 0; Said = '' }
    }
    $stream = $null
    try {
        $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::ReadWrite)
        return @{ Path = $Path; Replaceable = $true; Reason = 'ok'; Code = 0; Said = '' }
    } catch {
        # One catch, and the type is read off the unwrapped exception, rather
        # than three typed catches: whether PowerShell matches `catch
        # [IOException]` against the inner exception of the wrapper it puts
        # round a failed .NET call is not something this should rest on.
        $ex = Get-InnerException $_
        $code = Get-Win32ErrorCode $ex
        $said = ''
        if ($ex) { $said = $ex.Message }
        $reason = 'unknown'
        if ($ex -is [System.UnauthorizedAccessException]) {
            $reason = 'denied'
            if ($code -eq 0) { $code = 5 }
        } elseif ($ex -is [System.IO.IOException]) {
            $reason = 'held'
        }
        return @{ Path = $Path; Replaceable = $false; Reason = $reason; Code = $code; Said = $said }
    } finally {
        if ($stream) { $stream.Dispose() }
    }
}

function Test-FolderWritable {
    <# Can anything at all be written into this folder?

       This is what separates the two causes that look identical from the error
       message alone: one file being held open by a program, and a folder the
       account simply may not write to (a Python installed for all users under
       C:\, with setup run without administrator rights). Setup can establish
       which, so it does, rather than guessing in the message. #>
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        return @{ Writable = $false; Reason = 'absent'; Said = "there is no folder at $Path" }
    }
    $probe = Join-Path $Path ('.dictate-write-test-' + [System.IO.Path]::GetRandomFileName())
    try {
        [System.IO.File]::WriteAllText($probe, 'x')
        Remove-Item -LiteralPath $probe -Force -ErrorAction SilentlyContinue
        return @{ Writable = $true; Reason = 'ok'; Said = '' }
    } catch {
        Remove-Item -LiteralPath $probe -Force -ErrorAction SilentlyContinue
        $ex = Get-InnerException $_
        $said = ''
        if ($ex) { $said = $ex.Message }
        return @{ Writable = $false; Reason = 'denied'; Said = $said }
    }
}

function Add-RestartManagerType {
    <# Compile the four Restart Manager calls, once per session.

       Windows ships no command that answers "which program has this file open",
       and Sysinternals handle.exe is not something to make an install depend on
       - but rstrtmgr.dll is in every Windows, needs no administrator rights,
       and exists precisely so that an installer can name what is holding a file
       it must replace. It is what Windows Installer itself uses.

       Returns $true if the type is available. Every failure here is survivable:
       the report falls back to matching running processes by their image path,
       and says which of the two answers it is giving. #>
    if ($script:RestartManagerReady) { return $true }
    if ($script:RestartManagerFailed) { return $false }
    $source = @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;

public static class DictateRestartManager
{
    [StructLayout(LayoutKind.Sequential)]
    private struct RM_UNIQUE_PROCESS
    {
        public int dwProcessId;
        public System.Runtime.InteropServices.ComTypes.FILETIME ProcessStartTime;
    }

    private const int CCH_RM_MAX_APP_NAME = 255;
    private const int CCH_RM_MAX_SVC_NAME = 63;
    private const int ERROR_MORE_DATA = 234;

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct RM_PROCESS_INFO
    {
        public RM_UNIQUE_PROCESS Process;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = CCH_RM_MAX_APP_NAME + 1)]
        public string strAppName;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = CCH_RM_MAX_SVC_NAME + 1)]
        public string strServiceShortName;
        public int ApplicationType;
        public uint AppStatus;
        public uint TSSessionId;
        [MarshalAs(UnmanagedType.Bool)]
        public bool bRestartable;
    }

    [DllImport("rstrtmgr.dll", CharSet = CharSet.Unicode)]
    private static extern int RmStartSession(out uint pSessionHandle, int dwSessionFlags,
        StringBuilder strSessionKey);

    [DllImport("rstrtmgr.dll")]
    private static extern int RmEndSession(uint pSessionHandle);

    [DllImport("rstrtmgr.dll", CharSet = CharSet.Unicode)]
    private static extern int RmRegisterResources(uint pSessionHandle, uint nFiles,
        string[] rgsFilenames, uint nApplications, RM_UNIQUE_PROCESS[] rgApplications,
        uint nServices, string[] rgsServiceNames);

    [DllImport("rstrtmgr.dll")]
    private static extern int RmGetList(uint dwSessionHandle, out uint pnProcInfoNeeded,
        ref uint pnProcInfo, [In, Out] RM_PROCESS_INFO[] rgAffectedApps,
        ref uint lpdwRebootReasons);

    // One string per holder: "<pid>|<name as the Restart Manager describes it>".
    // An empty array means Windows named nobody, which is a real answer and not
    // the same as the call having failed - that throws.
    public static string[] WhoIsUsing(string path)
    {
        uint session;
        StringBuilder key = new StringBuilder(64);
        int rc = RmStartSession(out session, 0, key);
        if (rc != 0) { throw new InvalidOperationException("RmStartSession returned " + rc); }
        try
        {
            rc = RmRegisterResources(session, 1, new string[] { path }, 0, null, 0, null);
            if (rc != 0) { throw new InvalidOperationException("RmRegisterResources returned " + rc); }

            uint needed = 0;
            uint got = 0;
            uint reasons = 0;
            rc = RmGetList(session, out needed, ref got, null, ref reasons);
            // Nobody has it: that is an answer, not a failure.
            if (rc == 0 && needed == 0) { return new string[0]; }
            // Anything other than "you need a bigger array" is a failure, and
            // the caller falls back rather than reporting an empty list as if
            // Windows had said nothing holds the file.
            if (rc != 0 && rc != ERROR_MORE_DATA)
            {
                throw new InvalidOperationException("RmGetList returned " + rc);
            }

            RM_PROCESS_INFO[] info = new RM_PROCESS_INFO[needed];
            got = needed;
            rc = RmGetList(session, out needed, ref got, info, ref reasons);
            if (rc != 0) { throw new InvalidOperationException("RmGetList returned " + rc); }
            if (got > info.Length) { got = (uint)info.Length; }

            List<string> found = new List<string>();
            for (int i = 0; i < got; i++)
            {
                found.Add(info[i].Process.dwProcessId + "|" + (info[i].strAppName ?? ""));
            }
            return found.ToArray();
        }
        finally
        {
            RmEndSession(session);
        }
    }
}
'@
    try {
        if (-not ('DictateRestartManager' -as [type])) {
            Add-Type -TypeDefinition $source -Language CSharp -ErrorAction Stop
        }
        $script:RestartManagerReady = $true
        return $true
    } catch {
        Write-SetupLog "Restart Manager unavailable: $($_.Exception.Message)"
        $script:RestartManagerFailed = $true
        return $false
    }
}

function Get-FileHolder {
    <# Who has this file open, as precisely as Windows will say.

       Returns an array of @{ Pid; Name; Path; Source }. Source is 'windows'
       when the Restart Manager named it and 'image' when the fallback found a
       running program whose own executable IS this file - a launcher like
       Scripts\dictate.exe, which Windows keeps open for as long as it runs.
       Nothing here is ever presented as a complete list; see
       Get-FileLockReport, which says which of the two answers it got. #>
    param([Parameter(Mandatory = $true)][string]$Path)

    $holders = New-Object System.Collections.Generic.List[object]
    $full = $Path
    try { $full = (Resolve-Path -LiteralPath $Path -ErrorAction Stop).ProviderPath } catch { }

    $seen = New-Object System.Collections.Generic.List[int]
    if (Add-RestartManagerType) {
        try {
            foreach ($entry in [DictateRestartManager]::WhoIsUsing($full)) {
                $parts = $entry -split '\|', 2
                $processId = 0
                [void][int]::TryParse($parts[0], [ref]$processId)
                $name = ''
                if ($parts.Count -gt 1) { $name = $parts[1] }
                $exe = ''
                try {
                    $proc = Get-Process -Id $processId -ErrorAction Stop
                    if ($proc.Path) { $exe = $proc.Path }
                    if (-not $name) { $name = $proc.ProcessName }
                } catch { }
                $holders.Add(@{ Pid = $processId; Name = $name; Path = $exe; Source = 'windows' })
                $seen.Add($processId)
            }
        } catch {
            Write-SetupLog "Restart Manager could not answer for ${full}: $($_.Exception.Message)"
        }
    }

    # Run this whether or not the Restart Manager answered, and merge - it is
    # not only a fallback. A program whose own image this file IS - a launcher
    # like Scripts\dictate.exe, which Windows keeps open for as long as it runs
    # - is the holder that matters most here, and it costs one query to name it
    # rather than rest on the Restart Manager choosing to.
    try {
        $running = @(Get-CimInstance -ClassName Win32_Process -ErrorAction Stop |
            Where-Object { $_.ExecutablePath -and ($_.ExecutablePath -ieq $full) })
        foreach ($proc in $running) {
            $processId = [int]$proc.ProcessId
            if ($seen.Contains($processId)) { continue }
            $holders.Add(@{ Pid = $processId; Name = $proc.Name
                Path = $proc.ExecutablePath; Source = 'image' })
        }
    } catch {
        Write-SetupLog "could not scan running processes for ${full}: $($_.Exception.Message)"
    }
    return $holders.ToArray()
}

function Get-DictateProcessList {
    <# Every running program that could plausibly be a copy of dictate, for the
       "look in Task Manager" step - filled in, rather than left as a search.

       Deliberately reported and never acted on: setup does not end another
       program's process. `dictate stop` is the command for that, and it is what
       the next action names. #>
    $found = New-Object System.Collections.Generic.List[object]
    try {
        $procs = @(Get-CimInstance -ClassName Win32_Process -ErrorAction Stop |
            Where-Object { $_.Name -match '(?i)^(dictate|python|pythonw|whisper-server)\.exe$' })
        foreach ($proc in $procs) {
            $path = ''
            if ($proc.ExecutablePath) { $path = $proc.ExecutablePath }
            $found.Add(@{ Pid = [int]$proc.ProcessId; Name = $proc.Name; Path = $path })
        }
    } catch {
        Write-SetupLog "could not list running processes: $($_.Exception.Message)"
    }
    return $found.ToArray()
}

function Wait-ForFilesReplaceable {
    <# Wait for every one of these files to become replaceable, and say what
       happened.

       Returns @{ Ok; WaitedSeconds; Blocked }, where Blocked is the
       Test-FileReplaceable result for each file still held when time ran out.

       The wait exists because the most likely single explanation for "our check
       passed and the very next operation was refused" is a holder that was on
       its way out: a launcher process finishing after the copy it started, or
       an antivirus scanner reading an executable that has just been written.
       Both let go on their own within a second or two. Nothing here can tell
       those apart from a holder that will never leave - which is why it waits
       first and only then reports. #>
    param(
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][string[]]$Paths,
        [int]$TimeoutSeconds = 0,
        [double]$FirstPollSeconds = 0.25,
        [scriptblock]$OnWaiting = $null
    )
    if ($TimeoutSeconds -le 0) { $TimeoutSeconds = $script:FileUnlockTimeoutSeconds }

    $clock = [Diagnostics.Stopwatch]::StartNew()
    $poll = $FirstPollSeconds
    $announced = $false
    $blocked = @()
    while ($true) {
        $blocked = @($Paths | ForEach-Object { Test-FileReplaceable -Path $_ } |
            Where-Object { -not $_.Replaceable })
        if ($blocked.Count -eq 0) {
            $clock.Stop()
            return @{ Ok = $true; WaitedSeconds = $clock.Elapsed.TotalSeconds; Blocked = @() }
        }
        if ($clock.Elapsed.TotalSeconds -ge $TimeoutSeconds) { break }
        if (-not $announced -and $OnWaiting) {
            $announced = $true
            & $OnWaiting $blocked $TimeoutSeconds
        }
        # Back off rather than hammering the file: a scanner that is reading it
        # finishes sooner if nothing keeps interrupting. Clamped to what is left
        # of the budget as well as to 2 seconds, so that a run which says "up to
        # 20 seconds" does not then report having waited 22.
        $left = $TimeoutSeconds - $clock.Elapsed.TotalSeconds
        $nap = [math]::Min([math]::Min(2.0, $poll), $left)
        if ($nap -gt 0) { Start-Sleep -Milliseconds ([int]($nap * 1000)) }
        $poll = $poll * 2
    }
    $clock.Stop()
    return @{ Ok = $false; WaitedSeconds = $clock.Elapsed.TotalSeconds; Blocked = $blocked }
}

function Get-FileLockReport {
    <# The words for "Windows will not let the installer replace this file".

       Returns @{ Problem; NextAction }. Every sentence in it is something setup
       established: the file it could not open, the number Windows gave, whether
       the folder itself is writable, and who Windows named as holding it. Where
       it does not know, it says so - the failure this replaces asserted a cause
       (the internet) that had nothing to do with the evidence. #>
    param(
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][object[]]$Blocked,
        [double]$WaitedSeconds = 0,
        [string]$LogPath = ''
    )

    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add('Windows would not let the installer replace a file dictate had already')
    $lines.Add('installed, so nothing was changed.')
    $lines.Add('')

    $folders = New-Object System.Collections.Generic.List[string]
    $anyHolderNamed = $false
    $anyFolderUnwritable = $false
    $anyReleased = $false
    foreach ($item in $Blocked) {
        $lines.Add("  $($item.Path)")

        if ($item.Reason -eq 'released') {
            # It is free NOW. Saying "it is being held" would be describing a
            # state that has already gone, and would send him looking for
            # something that is no longer there.
            $anyReleased = $true
            $lines.Add('    Whatever had this open has since let go: it is free now.')
        } else {
            $said = 'Windows refused'
            if ($item.Code -eq 5) { $said = 'Windows said "Access is denied" (error 5)' }
            elseif ($item.Code -eq 32) { $said = 'Windows said the file is in use by another program (error 32)' }
            elseif ($item.Code -eq 33) { $said = 'Windows said part of the file is locked (error 33)' }
            elseif ($item.Code -gt 0) { $said = "Windows refused with error $($item.Code)" }
            $lines.Add("    $said.")
        }

        $folder = Split-Path -Parent $item.Path
        if ($folder -and -not $folders.Contains($folder)) { $folders.Add($folder) }

        $holders = @()
        if (Test-Path -LiteralPath $item.Path -PathType Leaf) {
            $holders = @(Get-FileHolder -Path $item.Path)
        }
        if ($holders.Count -gt 0) {
            $anyHolderNamed = $true
            $lines.Add('    Windows names this as holding it:')
            foreach ($holder in $holders) {
                $where = ''
                if ($holder.Path) { $where = " - $($holder.Path)" }
                $lines.Add("      $($holder.Name) (process $($holder.Pid))$where")
            }
        } elseif ($item.Reason -ne 'released') {
            $lines.Add('    Windows would not say what is holding it.')
        }
    }

    $allReleased = $anyReleased -and -not (@($Blocked | Where-Object { $_.Reason -ne 'released' }).Count)
    foreach ($folder in $folders) {
        $writable = Test-FolderWritable -Path $folder
        $lines.Add('')
        if ($writable.Writable -and $allReleased) {
            $lines.Add("Setup CAN write into $folder, so this was one file being held open")
            $lines.Add('for a moment rather than a permissions problem.')
        } elseif ($writable.Writable) {
            $lines.Add("Setup CAN write into $folder, so this is one file being held open")
            $lines.Add('by a running program rather than a permissions problem.')
        } else {
            $anyFolderUnwritable = $true
            $lines.Add("Setup cannot write into $folder at all, so this is a permissions")
            $lines.Add('problem with that folder rather than a program holding one file.')
            if ($writable.Said) { $lines.Add("Windows said: $($writable.Said)") }
        }
    }

    if ($WaitedSeconds -gt 0) {
        $lines.Add('')
        $lines.Add(('Setup waited {0:N0} seconds for it to be let go, and it was not.' -f $WaitedSeconds))
    }

    # -- What to do about it -------------------------------------------------
    $next = New-Object System.Collections.Generic.List[string]
    if ($anyFolderUnwritable) {
        $next.Add('This is a folder your account may not write to, so:')
        $next.Add('')
        $next.Add('  1. Close this window, open PowerShell again with "Run as administrator",')
        $next.Add('     and run setup from there.')
        $next.Add('  2. If that is not something you can do on this PC, install Python for')
        $next.Add('     yourself rather than for all users - its Scripts folder is then')
        $next.Add('     under your own account and needs no administrator rights.')
    } elseif ($anyReleased) {
        $next.Add('Nothing is holding it now, so running setup again is very likely to work,')
        $next.Add('and it carries on from here - everything already installed, built and')
        $next.Add('downloaded is kept:')
        $next.Add('')
        $next.Add('  powershell -ExecutionPolicy Bypass -File setup.ps1')
        $next.Add('')
        $next.Add('If it stops in the same place a second time, something is taking hold of')
        $next.Add('that file every time setup writes it - antivirus is the usual one, and an')
        if ($folders.Count -gt 0) {
            $next.Add('exclusion for these folders is what settles it:')
            foreach ($folder in $folders) { $next.Add("  $folder") }
        } else {
            $next.Add('exclusion for the folder named above is what settles it.')
        }
    } else {
        $next.Add('Something is holding that file open. In this order:')
        $next.Add('')
        $next.Add('  1. Close any window that is running dictate, and ask a copy that is')
        $next.Add('     still going to stop:')
        $next.Add('       dictate stop')
        $next.Add('  2. Look in Task Manager for anything called dictate, python, pythonw')
        $next.Add('     or whisper-server, and End task on it.')
        if ($anyHolderNamed) {
            $next.Add('     The programs named above are the ones to look for first.')
        } else {
            # Windows would not name the holder, so the next best thing is the
            # list he would otherwise be scrolling Task Manager for. Reported,
            # never acted on: setup does not end anyone else's process.
            $running = @(Get-DictateProcessList)
            if ($running.Count -gt 0) {
                $next.Add('     These are running right now:')
                foreach ($proc in $running) {
                    $where = ''
                    if ($proc.Path) { $where = " - $($proc.Path)" }
                    $next.Add("       $($proc.Name) (process $($proc.Pid))$where")
                }
            } else {
                $next.Add('     Setup could not see any of those running, so it may be')
                $next.Add('     something else entirely - step 3 is the next thing to try.')
            }
        }
        if ($folders.Count -gt 0) {
            $next.Add('  3. If you run antivirus, it may be scanning the file setup has just')
            $next.Add('     written. Add an exclusion for these folders and try again:')
            foreach ($folder in $folders) { $next.Add("       $folder") }
        } else {
            # No folder to name, so do not print a list header with nothing
            # under it - that reads as setup having lost the answer.
            $next.Add('  3. If you run antivirus, it may be scanning the file setup has just')
            $next.Add('     written. Add an exclusion for the folder named above and try')
            $next.Add('     again.')
        }
        $next.Add('  4. Run setup again. Everything already installed, built and downloaded')
        $next.Add('     is kept - it carries on from here.')
        $next.Add('')
        $next.Add('If it happens every time and nothing above is running, restarting the PC')
        $next.Add('clears any handle that is left, and setup will carry on afterwards.')
    }
    $next.Add('')
    $next.Add('This is nothing to do with your internet connection: replacing a file that')
    $next.Add('is already on this PC does not use the network.')
    if ($LogPath) {
        $next.Add('')
        $next.Add('The full output is in:')
        $next.Add("  $LogPath")
    }

    return @{
        Problem    = ($lines -join [Environment]::NewLine)
        NextAction = ($next -join [Environment]::NewLine)
    }
}

# ---------------------------------------------------------------------------
# Reading a failure rather than assuming one
# ---------------------------------------------------------------------------

function Get-PipFailurePath {
    <# The file pip named in an access-denied message, or ''. #>
    param([string]$Output)
    if (-not $Output) { return '' }
    $patterns = @(
        "(?im)\[WinError\s+(?:5|32|33)\][^:\r\n]*:\s*'?([A-Za-z]:\\[^'\r\n]+?)'?\s*$",
        "(?im)(?:Access is denied|being used by another process)[^:\r\n]*:\s*'?([A-Za-z]:\\[^'\r\n]+?)'?\s*$"
    )
    foreach ($pattern in $patterns) {
        $match = [regex]::Match($Output, $pattern)
        if ($match.Success) { return $match.Groups[1].Value.Trim() }
    }
    return ''
}

function Get-PipFailureKind {
    <# What pip's own output SHOWS went wrong. One of:

         'locked'   Windows refused access to a file, or said it is in use
         'network'  pip reported it could not reach or resolve an index
         'disk'     pip reported it ran out of room
         'unknown'  nothing in the output establishes a cause

       'unknown' is a real answer and the most important one here. The message
       this replaced told the product owner to check his internet connection and
       his proxy for a Windows file-lock error, and he went and looked. A step
       that asserts a cause it has not established costs more time than one that
       says plainly what the tool reported. #>
    param([string]$Output)
    if (-not $Output) { return 'unknown' }

    # Order matters: pip prints its retry banner while a proxy is refusing it,
    # but it also prints "Access is denied" with no retries at all. The file
    # error is the specific one, so it is tested first.
    if ($Output -match '(?im)\[WinError\s+(?:5|32|33)\]' -or
        $Output -match '(?im)Access is denied' -or
        $Output -match '(?im)being used by another process' -or
        $Output -match '(?im)^\s*PermissionError') { return 'locked' }

    if ($Output -match '(?im)No space left on device' -or
        $Output -match '(?im)There is not enough space on the disk' -or
        $Output -match '(?im)\[Errno 28\]') { return 'disk' }

    if ($Output -match '(?im)Could not find a version that satisfies' -or
        $Output -match '(?im)No matching distribution found' -or
        $Output -match '(?im)Temporary failure in name resolution' -or
        $Output -match '(?im)Failed to establish a new connection' -or
        $Output -match '(?im)getaddrinfo failed' -or
        $Output -match '(?im)Network is unreachable' -or
        $Output -match '(?im)Tunnel connection failed' -or
        $Output -match '(?im)(Connection|Proxy|SSL|ReadTimeout)Error' -or
        $Output -match '(?im)Read timed out' -or
        $Output -match '(?im)Retrying \(Retry\(') { return 'network' }

    return 'unknown'
}

function Get-ToolErrorLines {
    <# The lines of a tool's output that actually say something went wrong, most
       recent last, capped so the screen stays readable.

       This is how a step reports a cause it could not establish: it does not
       invent one, it shows what the tool said. #>
    param([string]$Output, [int]$Limit = 8)
    if (-not $Output) { return @() }
    $interesting = New-Object System.Collections.Generic.List[string]
    foreach ($line in ($Output -split "`r?`n")) {
        $text = $line.TrimEnd()
        if (-not $text.Trim()) { continue }
        if ($text -match '(?i)(^\s*(ERROR|error:|WARNING: Ignoring|Traceback)|Error\b|failed|cannot|could not|denied|refused|No such|not found)') {
            $interesting.Add($text.Trim())
        }
    }
    if ($interesting.Count -eq 0) {
        # Nothing matched, so show the tail rather than nothing: the last thing a
        # tool printed before it gave up is usually the reason it did.
        $all = @($Output -split "`r?`n" | Where-Object { $_.Trim() } | ForEach-Object { $_.Trim() })
        if ($all.Count -eq 0) { return @() }
        $start = [math]::Max(0, $all.Count - $Limit)
        return @($all[$start..($all.Count - 1)])
    }
    if ($interesting.Count -le $Limit) { return $interesting.ToArray() }
    $keep = $interesting.ToArray()
    return @($keep[($keep.Count - $Limit)..($keep.Count - 1)])
}

function Get-InstallFailureReport {
    <# The words for a failed `pip install -e`, decided by what pip printed.

       Returns @{ Problem; NextAction; Kind }. Kind is Get-PipFailureKind's
       answer, so the caller and the tests can see which branch was taken.

       The network branch is now reached only when pip's own output shows a
       network failure. It used to be the only branch there was, which is how
       "check your internet connection and proxy settings" came to be printed
       for a file that a program on his own PC had open. #>
    param(
        [Parameter(Mandatory = $true)][int]$ExitCode,
        [string]$Output = '',
        [string]$LogPath = '',
        [string[]]$Targets = @()
    )
    $kind = Get-PipFailureKind -Output $Output

    if ($kind -eq 'locked') {
        $named = Get-PipFailurePath -Output $Output
        $paths = @()
        if ($named) { $paths = @($named) }
        elseif ($Targets) { $paths = @($Targets) }
        $blocked = @()
        foreach ($path in $paths) {
            $state = Test-FileReplaceable -Path $path
            if ($state.Replaceable) {
                # It is free NOW. Say that rather than describing it as held:
                # a holder that has since let go is a different situation, and
                # running setup again is very likely to work.
                $state = @{ Path = $path; Replaceable = $false; Reason = 'released'
                    Code = 0; Said = '' }
            }
            $blocked += $state
        }
        if ($blocked.Count -eq 0) {
            $blocked = @(@{ Path = '(pip did not name the file)'; Replaceable = $false
                    Reason = 'unknown'; Code = 5; Said = '' })
        }
        $report = Get-FileLockReport -Blocked $blocked -LogPath $LogPath
        $report.Kind = 'locked'
        $said = @(Get-ToolErrorLines -Output $Output -Limit 4)
        if ($said.Count -gt 0) {
            $report.Problem = $report.Problem + [Environment]::NewLine + [Environment]::NewLine +
                'pip said:' + [Environment]::NewLine +
                (($said | ForEach-Object { '  ' + $_ }) -join [Environment]::NewLine)
        }
        return $report
    }

    $where = ''
    if ($LogPath) {
        $where = [Environment]::NewLine + 'The full output from the installer is in:' +
            [Environment]::NewLine + "  $LogPath"
    }

    if ($kind -eq 'network') {
        $next = "Check this PC is online - open a web page in a browser to be sure - and if you`n" +
            "are on a company network or a VPN, that it is not blocking pypi.org. Then run`n" +
            "setup again; everything already installed, built and downloaded is kept." + $where
        return @{
            Kind    = 'network'
            Problem = ("Installing dictate and the packages it needs failed, and pip reported " +
                "a problem reaching pypi.org (it stopped with error code $ExitCode).")
            NextAction = $next
        }
    }

    if ($kind -eq 'disk') {
        $next = "Free up some space and run setup again; everything already installed, built`n" +
            "and downloaded is kept." + $where
        return @{
            Kind    = 'disk'
            Problem = ("Installing dictate and the packages it needs failed: pip reported it " +
                "ran out of room on the disk (it stopped with error code $ExitCode).")
            NextAction = $next
        }
    }

    # Nothing in the output establishes a cause. Say exactly that, and show what
    # pip said, rather than picking the most common one and being wrong.
    $said = @(Get-ToolErrorLines -Output $Output)
    $problem = "Installing dictate and the packages it needs failed (it stopped with error code $ExitCode)."
    if ($said.Count -gt 0) {
        $problem = $problem + [Environment]::NewLine + [Environment]::NewLine +
            'Setup does not know why. This is what pip said:' + [Environment]::NewLine +
            (($said | ForEach-Object { '  ' + $_ }) -join [Environment]::NewLine)
    } else {
        $problem = $problem + [Environment]::NewLine + [Environment]::NewLine +
            'Setup does not know why, and pip printed nothing that says.'
    }
    $next = "Run setup again first - some of these do not happen twice, and everything`n" +
        "already installed, built and downloaded is kept.`n" +
        "If it stops in the same place, report the lines above." + $where
    return @{
        Kind       = 'unknown'
        Problem    = $problem
        NextAction = $next
    }
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
       the paths and model file names in that file and nothing else, and a
       re-run must not quietly revert a hotkey or a font size he changed. #>
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

# ---------------------------------------------------------------------------
# Starting when he logs in
#
# The feature itself is `dictate autostart enable`, and it works. What did not
# work is that it is a typed command he was never shown: he spent an evening
# starting dictate by hand in a PowerShell window and asking for a feature that
# was already installed on his machine. So setup settles the question at the
# moment he is at the keyboard - which is the START of the run, not the end of
# it, because the end is half an hour later and he is not watching by then.
#
# Two rules bound all of it. A question that can hang is worse than no question
# at all, so this can always answer without him. And a product does not add
# itself to Windows startup unless it was asked to, so every path that is not a
# clear yes leaves the machine exactly as it found it.
# ---------------------------------------------------------------------------

function Test-CanAskQuestion {
    <# Is there somebody at a keyboard to answer? #>
    try {
        if ([Console]::IsInputRedirected) { return $false }
        $null = [Console]::KeyAvailable
        return $true
    } catch {
        # A host with no console behind it - the ISE, a scheduled run, a remote
        # session that pipes its input. Not an error, just nobody to ask.
        return $false
    }
}

function Get-AutostartPlan {
    <# What this run should do about starting at logon, decided before anything
       is installed and carried out after everything has been verified.

       Returns 'enable', 'ask' or 'leave'. 'leave' means exactly that: it never
       disables anything, so a re-run cannot take away a logon task he asked for
       the first time. #>
    param(
        [Parameter(Mandatory = $true)][ValidateSet('ask', 'yes', 'no')][string]$Requested,
        [Parameter(Mandatory = $true)][bool]$Installing,
        [Parameter(Mandatory = $true)][bool]$CanAsk
    )
    # -Only verify (or -Only build) is somebody checking an installation, not
    # somebody installing one. It may not change what happens at logon.
    if (-not $Installing) { return 'leave' }
    if ($Requested -eq 'yes') { return 'enable' }
    if ($Requested -eq 'no') { return 'leave' }
    if ($CanAsk) { return 'ask' }
    # Unattended, and nobody said yes. The one answer that is always safe.
    return 'leave'
}

function Get-AutostartQuestion {
    <# The question itself. Both answers are spelled out, because "no" is a real
       answer here and not a way of getting out of a dialog. #>
    return @'
Should dictate start by itself when you log in?

  Yes  It starts with your Windows session - no PowerShell window, nothing on
       screen until you speak. The dictate icon by the clock is how you stop
       it, change the hotkey, or turn this back off.
  No   You start it yourself with `dictate run` each time, and leave that
       window open while you use it.

Either way you can change your mind whenever you like, from that icon or with
`dictate autostart enable` / `dictate autostart disable`.
'@
}

function Read-YesNoWithTimeout {
    <# A yes/no question that cannot hang.

       Setup runs unattended for half an hour, and it is often started and
       walked away from. A `Read-Host` here would leave it sitting at a prompt
       until somebody came back to the machine - which is the one failure a
       question at the start of a long install must not have. So this reads a
       single key, gives up after $TimeoutSeconds, and treats no answer as
       $Default. #>
    param(
        [Parameter(Mandatory = $true)][string]$Prompt,
        [int]$TimeoutSeconds = 30,
        [bool]$Default = $false
    )
    $shown = 'y/N'
    if ($Default) { $shown = 'Y/n' }
    Write-Host ''
    foreach ($line in ($Prompt -split "`r?`n")) { Write-Host -Object $line }
    Write-Host ''
    $answer = 'No'
    if ($Default) { $answer = 'Yes' }
    Write-Host ("[$shown]  (no answer within $TimeoutSeconds seconds means $answer, " +
                'and setup carries on)') -ForegroundColor Yellow -NoNewline
    Write-Host ' ' -NoNewline

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    try {
        while ((Get-Date) -lt $deadline) {
            if ([Console]::KeyAvailable) {
                $key = [Console]::ReadKey($true)
                $char = ([string]$key.KeyChar).ToLowerInvariant()
                if ($char -eq 'y') { Write-Host 'yes'; return $true }
                if ($char -eq 'n') { Write-Host 'no'; return $false }
                if ($key.Key -eq [ConsoleKey]::Enter) {
                    Write-Host $answer.ToLowerInvariant()
                    return $Default
                }
            }
            Start-Sleep -Milliseconds 100
        }
    } catch {
        # No console to read a key from after all. The default is the answer,
        # and the run carries on - this is never a reason to stop an install.
        Write-SetupLog "the yes/no question could not be asked here: $($_.Exception.Message)"
    }
    Write-Host ("no answer - taking that as $answer")
    return $Default
}

function Test-AutostartStatusOn {
    <# Read `dictate autostart status` output back: $true, $false, or $null when
       it did not say.

       Setup does not keep its own idea of whether the logon task exists. It
       asks dictate, and dictate asks Windows, so the line setup prints at the
       end and the answer `dictate autostart status` gives cannot disagree. #>
    param([string]$Output)
    if (-not $Output) { return $null }
    if ($Output -match '(?im)^\s*start at logon:\s*ON\s*$') { return $true }
    if ($Output -match '(?im)^\s*start at logon:\s*OFF\s*$') { return $false }
    # REGISTERED BUT DISABLED, "not available on ...", or a line this does not
    # know: report nothing rather than a guess.
    return $null
}
