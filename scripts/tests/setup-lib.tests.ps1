<#
.SYNOPSIS
    Tests for the risky parts of the installer. Run by CI on a real Windows
    machine; also runnable by hand.

.DESCRIPTION
    Nobody on this team has a Windows PC, so setup.ps1 cannot be run end to end
    before it is shipped. What CAN be tested is the logic underneath it - the
    resumable download, the integrity check, the edit to the product owner's
    config file - and that is what this does, against a real HTTP server that
    really drops the connection.

    Plain PowerShell on purpose: no Pester, no modules to install, so it runs on
    a stock Windows PowerShell 5.1 exactly as shipped.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\tests\setup-lib.tests.ps1
#>

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

. (Join-Path (Split-Path -Parent $PSScriptRoot) 'setup-lib.ps1')

$script:Passed = 0
$script:Failed = 0
$script:Skipped = 0

function Test-Case {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][scriptblock]$Body
    )
    try {
        & $Body
        $script:Passed++
        Write-Host "  pass  $Name" -ForegroundColor Green
    } catch {
        $script:Failed++
        Write-Host "  FAIL  $Name" -ForegroundColor Red
        Write-Host "        $($_.Exception.Message)" -ForegroundColor Red
        if ($_.ScriptStackTrace) {
            Write-Host "        $(($_.ScriptStackTrace -split "`n")[0])" -ForegroundColor DarkGray
        }
    }
}

function Skip-Case {
    param([string]$Name, [string]$Why)
    $script:Skipped++
    Write-Host "  skip  $Name - $Why" -ForegroundColor Yellow
}

function Assert-Equal {
    param($Expected, $Actual, [string]$What = 'value')
    if ($Expected -ne $Actual) {
        throw "$What should be [$Expected] but was [$Actual]"
    }
}

function Assert-True {
    param([bool]$Condition, [string]$What = 'condition')
    if (-not $Condition) { throw "$What should have been true" }
}

function Assert-False {
    param([bool]$Condition, [string]$What = 'condition')
    if ($Condition) { throw "$What should have been false" }
}

function Assert-Contains {
    param([string]$Haystack, [string]$Needle)
    if ($Haystack -notlike "*$Needle*") { throw "expected to find [$Needle] in: $Haystack" }
}

$script:TempRoot = Join-Path ([IO.Path]::GetTempPath()) ("dictate-setup-tests-" + [Guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Force -Path $script:TempRoot | Out-Null
Set-SetupLogPath (Join-Path $script:TempRoot 'test-log.txt')

function New-TempPath {
    param([string]$Name)
    return (Join-Path $script:TempRoot $Name)
}

Write-Host ''
Write-Host "setup-lib tests   (PowerShell $($PSVersionTable.PSVersion))" -ForegroundColor White
Write-Host "working in $script:TempRoot"
Write-Host ''

# ===========================================================================
Write-Host 'Versions' -ForegroundColor Cyan
# ===========================================================================

Test-Case 'a cmake banner parses to a version' {
    $v = ConvertTo-VersionOrNull 'cmake version 3.28.1'
    Assert-Equal ([version]'3.28.1') $v 'parsed version'
}

Test-Case 'a git banner with four parts parses' {
    $v = ConvertTo-VersionOrNull 'git version 2.45.1.windows.1'
    Assert-Equal ([version]'2.45.1') $v 'parsed version'
}

Test-Case 'a two-part version parses' {
    Assert-Equal ([version]'3.12') (ConvertTo-VersionOrNull 'Python 3.12') 'parsed version'
}

Test-Case 'text with no version at all gives nothing back' {
    Assert-True ($null -eq (ConvertTo-VersionOrNull 'command not found')) 'null for junk'
    Assert-True ($null -eq (ConvertTo-VersionOrNull '')) 'null for empty'
}

Test-Case 'version comparison decides upgrades correctly' {
    Assert-True (Test-VersionAtLeast 'cmake version 3.28.1' '3.20') 'newer passes'
    Assert-True (Test-VersionAtLeast 'cmake version 3.20.0' '3.20') 'exact passes'
    Assert-False (Test-VersionAtLeast 'cmake version 3.10.2' '3.20') 'older fails'
    Assert-False (Test-VersionAtLeast 'not a version' '3.20') 'unparseable fails'
}

Test-Case 'sizes and durations read as English' {
    Assert-Contains (Format-Bytes 1624555275) 'GB'
    Assert-Contains (Format-Bytes 310414022) 'MB'
    Assert-Contains (Format-Duration 45) 'seconds'
    Assert-Contains (Format-Duration 600) 'minutes'
}

# ===========================================================================
Write-Host ''
Write-Host 'The config file' -ForegroundColor Cyan
# ===========================================================================

$exampleConfig = Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) 'config\dictate.example.toml'

Test-Case 'a Windows path becomes a TOML literal string with no escaping' {
    Assert-Equal "'C:\dictate-gpu\models\ggml-large-v3-turbo.bin'" `
        (ConvertTo-TomlString 'C:\dictate-gpu\models\ggml-large-v3-turbo.bin') 'quoted path'
}

Test-Case 'a path containing a quote falls back to the escaped form' {
    Assert-Equal '"C:\\o''brien\\model.bin"' (ConvertTo-TomlString "C:\o'brien\model.bin") 'quoted path'
}

Test-Case 'setting a value changes only that key in that section' {
    $file = New-TempPath 'basic.toml'
    Set-Content -LiteralPath $file -Encoding UTF8 -Value @(
        '# a comment at the top',
        '[whisper]',
        "model = 'C:\old\model.bin'   # <<< CHECK THIS",
        'port = 8178',
        '',
        '[captions]',
        "model = 'C:\old\captions'"
    )
    Set-TomlValue -Path $file -Section 'whisper' -Key 'model' -Value 'C:\new\model.bin' -Comment 'set by setup'
    Assert-Equal 'C:\new\model.bin' (Get-TomlValue -Path $file -Section 'whisper' -Key 'model') 'whisper model'
    Assert-Equal 'C:\old\captions' (Get-TomlValue -Path $file -Section 'captions' -Key 'model') 'captions model untouched'
    $text = Get-Content -LiteralPath $file -Raw
    Assert-Contains $text 'a comment at the top'
    Assert-Contains $text 'port = 8178'
    Assert-Contains $text 'set by setup'
}

Test-Case 'setting a key that is not there adds it to the right section' {
    $file = New-TempPath 'missing-key.toml'
    Set-Content -LiteralPath $file -Encoding UTF8 -Value @(
        '[whisper]',
        'port = 8178',
        '',
        '[logging]',
        'level = "INFO"'
    )
    Set-TomlValue -Path $file -Section 'whisper' -Key 'server_exe' -Value 'C:\x\whisper-server.exe'
    Assert-Equal 'C:\x\whisper-server.exe' (Get-TomlValue -Path $file -Section 'whisper' -Key 'server_exe') 'added key'
    Assert-Equal 'INFO' (Get-TomlValue -Path $file -Section 'logging' -Key 'level') 'logging survived'
}

Test-Case 'setting a key in a section that is not there adds the section' {
    $file = New-TempPath 'missing-section.toml'
    Set-Content -LiteralPath $file -Encoding UTF8 -Value @('[whisper]', 'port = 8178')
    Set-TomlValue -Path $file -Section 'captions' -Key 'model_dir' -Value 'C:\models\zipformer'
    Assert-Equal 'C:\models\zipformer' (Get-TomlValue -Path $file -Section 'captions' -Key 'model_dir') 'added section'
    Assert-Equal '8178' (Get-TomlValue -Path $file -Section 'whisper' -Key 'port') 'whisper survived'
}

Test-Case 'the config is written without a byte order mark' {
    # Windows PowerShell's Set-Content -Encoding UTF8 puts a three-byte mark at
    # the front of the file, and Python's TOML parser calls that a syntax error
    # on line 1. The config would look perfect in an editor and dictate would
    # refuse to start.
    $file = New-TempPath 'no-bom.toml'
    Set-Content -LiteralPath $file -Encoding UTF8 -Value @('[whisper]', 'port = 8178')
    Set-TomlValue -Path $file -Section 'whisper' -Key 'model' -Value 'C:\models\m.bin'
    $bytes = [IO.File]::ReadAllBytes($file)
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
        throw 'the config was written with a UTF-8 byte order mark'
    }
    Assert-Equal 'C:\models\m.bin' (Get-TomlValue -Path $file -Section 'whisper' -Key 'model') 'value'
}

Test-Case 'the real shipped config can be pointed at real paths' {
    if (-not (Test-Path -LiteralPath $exampleConfig)) { throw "cannot find $exampleConfig" }
    $file = New-TempPath 'dictate.toml'
    Copy-Item -LiteralPath $exampleConfig -Destination $file
    Set-TomlValue -Path $file -Section 'whisper' -Key 'server_exe' -Value 'C:\dictate-gpu\whisper.cpp\build\bin\Release\whisper-server.exe' -Comment 'set by setup.ps1'
    Set-TomlValue -Path $file -Section 'whisper' -Key 'model' -Value 'C:\dictate-gpu\models\ggml-large-v3-turbo.bin' -Comment 'set by setup.ps1'
    Set-TomlValue -Path $file -Section 'captions' -Key 'model_dir' -Value 'C:\dictate-gpu\models\sherpa-onnx-streaming-zipformer-en-2023-06-26' -Comment 'set by setup.ps1'
    Set-TomlValue -Path $file -Section 'logging' -Key 'file' -Value 'C:\dictate-gpu\dictate.log'

    Assert-Equal 'C:\dictate-gpu\models\ggml-large-v3-turbo.bin' (Get-TomlValue -Path $file -Section 'whisper' -Key 'model') 'model path'
    Assert-Equal 'C:\dictate-gpu\dictate.log' (Get-TomlValue -Path $file -Section 'logging' -Key 'file') 'log path'
    # Settings the product owner might have changed must survive untouched.
    Assert-Equal 'ctrl + alt + space' (Get-TomlValue -Path $file -Section 'hotkey' -Key 'combination') 'hotkey'
    Assert-Equal '2' (Get-TomlValue -Path $file -Section 'captions' -Key 'num_threads') 'caption threads'
    Assert-Contains (Get-Content -LiteralPath $file -Raw) 'display-only'

    # And Python must still be able to read what we wrote.
    $python = Get-PythonCommand -MinimumVersion '3.11'
    if ($python) {
        $probe = New-TempPath 'read-config.py'
        Set-Content -LiteralPath $probe -Encoding ASCII -Value @(
            'import sys, tomllib',
            'with open(sys.argv[1], "rb") as fh:',
            '    data = tomllib.load(fh)',
            'print(data["whisper"]["model"])'
        )
        $out = (& $python.Path $probe $file 2>&1 | Out-String).Trim()
        if ($LASTEXITCODE -ne 0) { throw "python could not parse the config we wrote: $out" }
        Assert-Equal 'C:\dictate-gpu\models\ggml-large-v3-turbo.bin' $out 'python-parsed model path'
    }
}

# ===========================================================================
Write-Host ''
Write-Host 'Integrity checks' -ForegroundColor Cyan
# ===========================================================================

$sampleBytes = New-Object byte[] 65536
$rng = New-Object Random(20260813)
$rng.NextBytes($sampleBytes)
$samplePath = New-TempPath 'sample.bin'
[IO.File]::WriteAllBytes($samplePath, $sampleBytes)
$sampleSha = (Get-FileHash -LiteralPath $samplePath -Algorithm SHA256).Hash.ToLowerInvariant()

Test-Case 'a good file passes both checks' {
    Assert-True (Test-FileIntegrity -Path $samplePath -ExpectedBytes 65536 -ExpectedSha256 $sampleSha) 'good file'
}

Test-Case 'a file of the wrong size fails before it is even hashed' {
    Assert-False (Test-FileIntegrity -Path $samplePath -ExpectedBytes 99999 -ExpectedSha256 $sampleSha) 'wrong size'
}

Test-Case 'a file with the right size but the wrong contents fails' {
    $bad = New-TempPath 'corrupt.bin'
    $bytes = [byte[]]$sampleBytes.Clone()
    $bytes[1000] = [byte](($bytes[1000] + 1) % 256)
    [IO.File]::WriteAllBytes($bad, $bytes)
    Assert-False (Test-FileIntegrity -Path $bad -ExpectedBytes 65536 -ExpectedSha256 $sampleSha) 'corrupted file'
}

Test-Case 'a missing file fails rather than throwing' {
    Assert-False (Test-FileIntegrity -Path (New-TempPath 'not-here.bin') -ExpectedBytes 1 -ExpectedSha256 $sampleSha) 'missing file'
}

Test-Case 'a verified file records a sidecar so re-runs do not re-hash it' {
    $copy = New-TempPath 'sidecar.bin'
    [IO.File]::WriteAllBytes($copy, $sampleBytes)
    Assert-True (Test-FileIntegrity -Path $copy -ExpectedBytes 65536 -ExpectedSha256 $sampleSha) 'first check'
    Assert-True (Test-Path -LiteralPath "$copy.sha256") 'sidecar written'
    # A stale sidecar must not be able to vouch for a file of a different size.
    [IO.File]::WriteAllBytes($copy, (New-Object byte[] 100))
    Assert-False (Test-FileIntegrity -Path $copy -ExpectedBytes 65536 -ExpectedSha256 $sampleSha) 'size still checked'
}

# ===========================================================================
Write-Host ''
Write-Host 'Downloads (against a real HTTP server on this machine)' -ForegroundColor Cyan
# ===========================================================================

function Get-FreeTcpPort {
    $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $port = $listener.LocalEndpoint.Port
    $listener.Stop()
    return $port
}

function Test-CanListen {
    param([string]$Prefix)
    try {
        $probe = New-Object System.Net.HttpListener
        $probe.Prefixes.Add($Prefix)
        $probe.Start()
        $probe.Stop()
        $probe.Close()
        return $true
    } catch {
        Write-SetupLog "HttpListener unavailable: $($_.Exception.Message)"
        return $false
    }
}

$serverScript = {
    param($Prefix, $DataPath, $LogPath, $Mode)
    $listener = New-Object System.Net.HttpListener
    $listener.Prefixes.Add($Prefix)
    $listener.Start()
    $data = [IO.File]::ReadAllBytes($DataPath)
    $served = 0
    while ($true) {
        $context = $listener.GetContext()
        $request = $context.Request
        $response = $context.Response
        if ($request.Url.AbsolutePath -eq '/stop') {
            $response.StatusCode = 200
            $response.Close()
            break
        }
        $served++
        $range = $request.Headers['Range']
        Add-Content -LiteralPath $LogPath -Value "request $served range=$range"
        $start = 0
        if ($range -and $Mode -ne 'norange' -and $range -match 'bytes=(\d+)-') {
            $start = [int]$Matches[1]
            $response.StatusCode = 206
            $response.AddHeader('Content-Range', "bytes $start-$($data.Length - 1)/$($data.Length)")
        } else {
            $response.StatusCode = 200
        }
        $length = $data.Length - $start
        try {
            if ($Mode -eq 'droponce' -and $served -eq 1) {
                # Answer as if the whole body is coming, send half of it, then
                # cut the connection - exactly what a flaky link looks like.
                $response.ContentLength64 = $length
                $response.OutputStream.Write($data, $start, [int]($length / 2))
                $response.OutputStream.Flush()
                $response.Abort()
                continue
            }
            $response.ContentLength64 = $length
            $response.OutputStream.Write($data, $start, $length)
            $response.OutputStream.Close()
            $response.Close()
        } catch {
            Add-Content -LiteralPath $LogPath -Value "serve error: $($_.Exception.Message)"
        }
    }
    $listener.Stop()
    $listener.Close()
}

function Start-TestServer {
    param([string]$Mode, [string]$DataPath, [string]$LogPath)
    $port = Get-FreeTcpPort
    $prefix = "http://localhost:$port/"
    if (-not (Test-CanListen -Prefix $prefix)) { return $null }
    $job = Start-Job -ScriptBlock $serverScript -ArgumentList $prefix, $DataPath, $LogPath, $Mode
    # Wait for it to actually be listening before anything asks it for a file.
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline) {
        try {
            $probe = New-Object System.Net.Sockets.TcpClient
            $probe.Connect('localhost', $port)
            $probe.Close()
            return @{ Job = $job; Uri = ($prefix + 'payload.bin'); StopUri = ($prefix + 'stop'); Port = $port }
        } catch {
            Start-Sleep -Milliseconds 200
        }
    }
    Stop-Job $job -ErrorAction SilentlyContinue
    Remove-Job $job -Force -ErrorAction SilentlyContinue
    return $null
}

function Stop-TestServer {
    param($Server)
    if (-not $Server) { return }
    try {
        $request = [Net.HttpWebRequest][Net.WebRequest]::Create($Server.StopUri)
        $request.Timeout = 5000
        $request.GetResponse().Close()
    } catch {
        Write-SetupLog "test server would not stop cleanly: $($_.Exception.Message)"
    }
    Stop-Job $Server.Job -ErrorAction SilentlyContinue
    Remove-Job $Server.Job -Force -ErrorAction SilentlyContinue
}

# A 3 MB payload, big enough that a half-delivered copy is genuinely partial.
$payloadBytes = New-Object byte[] (3 * 1024 * 1024)
(New-Object Random(1963)).NextBytes($payloadBytes)
$payloadPath = New-TempPath 'payload.bin'
[IO.File]::WriteAllBytes($payloadPath, $payloadBytes)
$payloadSha = (Get-FileHash -LiteralPath $payloadPath -Algorithm SHA256).Hash.ToLowerInvariant()
$payloadSize = $payloadBytes.Length

$probePrefix = "http://localhost:$(Get-FreeTcpPort)/"
if (-not (Test-CanListen -Prefix $probePrefix)) {
    Skip-Case 'download tests' 'this account may not open an HTTP listener (try an elevated prompt)'
} else {

    Test-Case 'a straightforward download arrives complete and verified' {
        $server = Start-TestServer -Mode 'range' -DataPath $payloadPath -LogPath (New-TempPath 'server1.log')
        if (-not $server) { throw 'the test server would not start' }
        try {
            $target = New-TempPath 'download1.bin'
            $result = Get-FileResumable -Uri $server.Uri -Destination $target -ExpectedBytes $payloadSize `
                -ExpectedSha256 $payloadSha -Label 'test payload'
            Assert-Equal 'downloaded' $result 'result'
            Assert-Equal $payloadSize (Get-Item -LiteralPath $target).Length 'size'
            Assert-Equal $payloadSha (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant() 'sha256'
            Assert-True (Test-Path -LiteralPath "$target.sha256") 'sidecar written'
            Assert-False (Test-Path -LiteralPath "$target.part") 'part file cleaned up'
        } finally {
            Stop-TestServer $server
        }
    }

    Test-Case 'a file already downloaded is not downloaded again' {
        $server = Start-TestServer -Mode 'range' -DataPath $payloadPath -LogPath (New-TempPath 'server2.log')
        if (-not $server) { throw 'the test server would not start' }
        try {
            $target = New-TempPath 'download2.bin'
            Get-FileResumable -Uri $server.Uri -Destination $target -ExpectedBytes $payloadSize `
                -ExpectedSha256 $payloadSha -Label 'test payload' | Out-Null
            $again = Get-FileResumable -Uri $server.Uri -Destination $target -ExpectedBytes $payloadSize `
                -ExpectedSha256 $payloadSha -Label 'test payload'
            Assert-Equal 'already-here' $again 'second call'
            $requests = @(Get-Content -LiteralPath (New-TempPath 'server2.log'))
            Assert-Equal 1 $requests.Count 'the server was asked exactly once'
        } finally {
            Stop-TestServer $server
        }
    }

    Test-Case 'a download that dies half way carries on from where it stopped' {
        $log = New-TempPath 'server3.log'
        $server = Start-TestServer -Mode 'droponce' -DataPath $payloadPath -LogPath $log
        if (-not $server) { throw 'the test server would not start' }
        try {
            $target = New-TempPath 'download3.bin'
            $result = Get-FileResumable -Uri $server.Uri -Destination $target -ExpectedBytes $payloadSize `
                -ExpectedSha256 $payloadSha -Label 'test payload'
            Assert-Equal 'downloaded' $result 'result'
            Assert-Equal $payloadSha (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant() 'sha256'
            $requests = @(Get-Content -LiteralPath $log | Where-Object { $_ -like 'request*' })
            Assert-Equal 2 $requests.Count 'it took two requests'
            # The second one must have asked to resume rather than start again.
            Assert-Contains $requests[1] 'range=bytes='
            if ($requests[1] -match 'range=bytes=(\d+)-') {
                Assert-True ([int]$Matches[1] -gt 0) 'the resume started part way in'
            } else {
                throw "the second request did not carry a Range header: $($requests[1])"
            }
        } finally {
            Stop-TestServer $server
        }
    }

    Test-Case 'a server that cannot resume still ends up with the right file' {
        $log = New-TempPath 'server4.log'
        $server = Start-TestServer -Mode 'norange' -DataPath $payloadPath -LogPath $log
        if (-not $server) { throw 'the test server would not start' }
        try {
            $target = New-TempPath 'download4.bin'
            # Pretend a previous attempt left a third of the file behind.
            $partial = New-Object byte[] (1024 * 1024)
            [Array]::Copy($payloadBytes, $partial, $partial.Length)
            [IO.File]::WriteAllBytes("$target.part", $partial)

            $result = Get-FileResumable -Uri $server.Uri -Destination $target -ExpectedBytes $payloadSize `
                -ExpectedSha256 $payloadSha -Label 'test payload'
            Assert-Equal 'downloaded' $result 'result'
            Assert-Equal $payloadSha (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant() 'sha256'
        } finally {
            Stop-TestServer $server
        }
    }

    Test-Case 'a download whose contents do not match the published fingerprint is refused' {
        $server = Start-TestServer -Mode 'range' -DataPath $payloadPath -LogPath (New-TempPath 'server5.log')
        if (-not $server) { throw 'the test server would not start' }
        try {
            $target = New-TempPath 'download5.bin'
            $global:DictateSetupProblem = $null
            $threw = $false
            try {
                Get-FileResumable -Uri $server.Uri -Destination $target -ExpectedBytes $payloadSize `
                    -ExpectedSha256 ('0' * 64) -Label 'test payload' -MaxAttempts 2 | Out-Null
            } catch {
                $threw = $true
            }
            Assert-True $threw 'it gave up'
            Assert-False (Test-Path -LiteralPath $target) 'nothing was moved into place'
            Assert-True ($null -ne $global:DictateSetupProblem) 'it recorded a problem'
            Assert-Contains $global:DictateSetupProblem.Problem 'fingerprint'
            Assert-True ($global:DictateSetupProblem.NextAction.Length -gt 20) 'the problem says what to do next'
        } finally {
            $global:DictateSetupProblem = $null
            Stop-TestServer $server
        }
    }

    Test-Case 'a download that keeps stopping early gives up with something to do about it' {
        # Nothing is listening on this port at all, so every attempt fails.
        $deadPort = Get-FreeTcpPort
        $target = New-TempPath 'download6.bin'
        $global:DictateSetupProblem = $null
        $threw = $false
        try {
            Get-FileResumable -Uri "http://localhost:$deadPort/payload.bin" -Destination $target `
                -ExpectedBytes $payloadSize -ExpectedSha256 $payloadSha -Label 'test payload' `
                -MaxAttempts 2 -TimeoutSeconds 5 | Out-Null
        } catch {
            $threw = $true
        }
        Assert-True $threw 'it gave up'
        Assert-True ($null -ne $global:DictateSetupProblem) 'it recorded a problem'
        Assert-True ($global:DictateSetupProblem.NextAction.Length -gt 20) 'the problem says what to do next'
        $global:DictateSetupProblem = $null
    }
}

# ===========================================================================
Write-Host ''
Write-Host 'Failure reporting' -ForegroundColor Cyan
# ===========================================================================

Test-Case 'every failure carries a problem and a next action' {
    $global:DictateSetupProblem = $null
    $threw = $false
    try {
        Stop-Setup -Problem 'The model file is only 40 MB.' -NextAction 'Delete it and run setup again.'
    } catch {
        $threw = $true
    }
    Assert-True $threw 'Stop-Setup throws'
    Assert-Equal 'The model file is only 40 MB.' $global:DictateSetupProblem.Problem 'problem'
    Assert-Equal 'Delete it and run setup again.' $global:DictateSetupProblem.NextAction 'next action'
    $global:DictateSetupProblem = $null
}

Test-Case 'refreshing the environment keeps what this session already had' {
    # An installer writes the new tool into the registry, but this window still
    # has the PATH it started with. Picking the new one up must not lose the old
    # one: that is how a build ends up unable to find something it had a moment
    # ago.
    $marker = Join-Path $script:TempRoot 'a-folder-only-this-session-knows'
    $before = $env:PATH
    try {
        $env:PATH = "$marker;$env:PATH"
        Update-SessionEnvironment
        Assert-Contains $env:PATH $marker
    } finally {
        $env:PATH = $before
    }
}

Test-Case 'a program writing to stderr does not stop the install' {
    # git, cmake and pip all write ordinary progress to stderr. Windows
    # PowerShell will turn that into a terminating error under
    # $ErrorActionPreference = 'Stop' unless it is handled, which would abort a
    # perfectly good build. Invoke-Tool exists to stop that happening.
    $python = Get-PythonCommand -MinimumVersion '3.8'
    if (-not $python) { throw 'no Python to test with' }
    $ErrorActionPreference = 'Stop'
    $run = Invoke-Tool -FilePath $python.Path -Arguments @('-c',
        'import sys; sys.stderr.write("Cloning into whisper.cpp...\n"); print("done")')
    Assert-Equal 0 $run.ExitCode 'exit code'
    Assert-Contains $run.Output 'Cloning into'
    Assert-Contains $run.Output 'done'
}

Test-Case 'a program that fails reports its exit code rather than throwing' {
    $python = Get-PythonCommand -MinimumVersion '3.8'
    if (-not $python) { throw 'no Python to test with' }
    $ErrorActionPreference = 'Stop'
    $run = Invoke-Tool -FilePath $python.Path -Arguments @('-c', 'import sys; sys.exit(3)')
    Assert-Equal 3 $run.ExitCode 'exit code'
}

Test-Case 'a non-zero exit code from a tool becomes a plain-language failure' {
    $global:DictateSetupProblem = $null
    $threw = $false
    try {
        Assert-ExitCode -Code 1 -Problem 'The compile failed.' -NextAction 'Look for the first line with the word error in it.'
    } catch {
        $threw = $true
    }
    Assert-True $threw 'it throws on a non-zero code'
    Assert-Contains $global:DictateSetupProblem.Problem 'The compile failed.'
    $global:DictateSetupProblem = $null

    # And a zero exit code must sail straight through.
    Assert-ExitCode -Code 0 -Problem 'never seen' -NextAction 'never seen'
}

# ===========================================================================

Write-Host ''
Write-Host "passed $script:Passed, failed $script:Failed, skipped $script:Skipped"
try {
    Remove-Item -LiteralPath $script:TempRoot -Recurse -Force -ErrorAction SilentlyContinue
} catch {
    Write-Host "  (could not clean up $script:TempRoot)" -ForegroundColor DarkGray
}
if ($script:Failed -gt 0) { exit 1 }
exit 0
