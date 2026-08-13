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
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $name = Split-Path -Leaf $FilePath
    $lines = New-Object System.Collections.Generic.List[string]
    try {
        Write-SetupLog "run: $FilePath $($Arguments -join ' ')"
        & $FilePath @Arguments 2>&1 | ForEach-Object {
            $text = ''
            if ($_ -is [System.Management.Automation.ErrorRecord]) {
                $text = $_.ToString()
            } else {
                $text = [string]$_
            }
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
    $names = @('VULKAN_SDK', 'CMAKE_PREFIX_PATH')
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
    Set-Content -LiteralPath $Path -Value $out.ToArray() -Encoding UTF8
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
