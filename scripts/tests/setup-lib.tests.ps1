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
Write-Host 'Finding the Vulkan SDK' -ForegroundColor Cyan
# ===========================================================================
#
# These cover the bug that stopped the product owner installing: setup told him
# the SDK had installed and to restart his PC, when winget had in fact refused
# to install anything and there was no SDK on the machine at all. "Is
# VULKAN_SDK set?" cannot tell those two states apart; everything below is
# about telling them apart by looking for the SDK itself.

function New-FakeVulkanSdk {
    <# The shape of a real SDK on disk: the two files CMake's FindVulkan needs,
       plus the shader compiler the whisper.cpp build needs. #>
    param([string]$Path, [switch]$NoShaderCompiler, [switch]$Empty)
    New-Item -ItemType Directory -Force -Path $Path | Out-Null
    if ($Empty) { return $Path }
    New-Item -ItemType Directory -Force -Path (Join-Path $Path 'Include\vulkan') | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $Path 'Lib') | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $Path 'Bin') | Out-Null
    Set-Content -LiteralPath (Join-Path $Path 'Include\vulkan\vulkan.h') -Value '/* header */' -Encoding ASCII
    Set-Content -LiteralPath (Join-Path $Path 'Lib\vulkan-1.lib') -Value 'lib' -Encoding ASCII
    if (-not $NoShaderCompiler) {
        Set-Content -LiteralPath (Join-Path $Path 'Bin\glslc.exe') -Value 'exe' -Encoding ASCII
    }
    return $Path
}

Test-Case 'a real SDK folder is recognised and an empty one is not' {
    $good = New-FakeVulkanSdk (New-TempPath 'sdk-good')
    Assert-True (Test-VulkanSdkDir $good) 'a real SDK'
    Assert-True (Test-VulkanShaderCompiler $good) 'glslc found'

    # This is the case that mattered: a folder that exists but is not an SDK.
    # Test-Path alone says yes to it, which is how a stale VULKAN_SDK pointing
    # at nothing useful could read as "installed".
    $empty = New-FakeVulkanSdk (New-TempPath 'sdk-empty') -Empty
    Assert-False (Test-VulkanSdkDir $empty) 'an empty folder is not an SDK'
    Assert-False (Test-VulkanSdkDir (New-TempPath 'sdk-not-there')) 'a missing folder is not an SDK'
    Assert-False (Test-VulkanSdkDir '') 'an unset path is not an SDK'
}

Test-Case 'an SDK without the shader compiler is still an SDK, and says so separately' {
    $partial = New-FakeVulkanSdk (New-TempPath 'sdk-no-glslc') -NoShaderCompiler
    Assert-True (Test-VulkanSdkDir $partial) 'headers and library are there'
    Assert-False (Test-VulkanShaderCompiler $partial) 'but glslc is not'
}

Test-Case 'the SDK is found on disk with no environment variable involved' {
    # The whole point of the fix: Windows has published nothing, and the SDK is
    # still found, because it is right there in the folder its installer uses.
    $root = New-TempPath 'VulkanSDK-disk'
    New-FakeVulkanSdk (Join-Path $root '1.3.290.0') | Out-Null
    $found = Get-VulkanSdkFromDisk -SearchRoots @($root)
    Assert-Equal (Join-Path $root '1.3.290.0') $found 'found on disk'
}

Test-Case 'when several SDK versions are installed the newest one wins' {
    $root = New-TempPath 'VulkanSDK-many'
    foreach ($version in @('1.3.290.0', '1.4.357.0', '1.4.313.2')) {
        New-FakeVulkanSdk (Join-Path $root $version) | Out-Null
    }
    # A folder that is not an SDK at all must not win by sorting highest.
    New-FakeVulkanSdk (Join-Path $root '9.9.9.9') -Empty | Out-Null
    Assert-Equal (Join-Path $root '1.4.357.0') (Get-VulkanSdkFromDisk -SearchRoots @($root)) 'newest real SDK'
}

Test-Case 'a versioned SDK beats an unversioned folder next to it' {
    $root = New-TempPath 'VulkanSDK-mixed'
    New-FakeVulkanSdk (Join-Path $root 'current') | Out-Null
    New-FakeVulkanSdk (Join-Path $root '1.4.341.1') | Out-Null
    Assert-Equal (Join-Path $root '1.4.341.1') (Get-VulkanSdkFromDisk -SearchRoots @($root)) 'the installer-made folder'
}

Test-Case 'an unversioned folder is still found when it is the only SDK there' {
    $root = New-TempPath 'VulkanSDK-only-current'
    New-FakeVulkanSdk (Join-Path $root 'current') | Out-Null
    Assert-Equal (Join-Path $root 'current') (Get-VulkanSdkFromDisk -SearchRoots @($root)) 'the only SDK'
}

Test-Case 'nothing installed means nothing found - not a guess' {
    $root = New-TempPath 'VulkanSDK-absent'
    Assert-True ($null -eq (Get-VulkanSdkFromDisk -SearchRoots @($root))) 'no SDK on disk'
    Assert-True ($null -eq (Resolve-VulkanSdk -SearchRoots @($root) -Sources @('disk'))) 'and none resolved'
}

Test-Case 'the session variable is used when it points at a real SDK, and ignored when it does not' {
    $good = New-FakeVulkanSdk (New-TempPath 'sdk-session')
    $empty = New-FakeVulkanSdk (New-TempPath 'sdk-session-stale') -Empty
    $before = $env:VULKAN_SDK
    $beforeAlt = $env:VK_SDK_PATH
    try {
        $env:VULKAN_SDK = $good
        $env:VK_SDK_PATH = ''
        $found = Resolve-VulkanSdk -Sources @('session')
        Assert-True ($null -ne $found) 'a real SDK in the variable is used'
        Assert-Equal $good $found.Path 'path'
        Assert-Equal 'session' $found.Source 'source'

        # A variable left pointing at a folder that is not an SDK must not count
        # as an install.
        $env:VULKAN_SDK = $empty
        Assert-True ($null -eq (Resolve-VulkanSdk -Sources @('session'))) 'a stale variable is not an install'
    } finally {
        $env:VULKAN_SDK = $before
        $env:VK_SDK_PATH = $beforeAlt
    }
}

Test-Case 'the disk is searched when the variable is unset, which is the case that was broken' {
    $root = New-TempPath 'VulkanSDK-unset'
    $sdk = New-FakeVulkanSdk (Join-Path $root '1.4.357.0')
    $before = $env:VULKAN_SDK
    $beforeAlt = $env:VK_SDK_PATH
    try {
        $env:VULKAN_SDK = ''
        $env:VK_SDK_PATH = ''
        $found = Resolve-VulkanSdk -SearchRoots @($root) -Sources @('session', 'disk')
        Assert-True ($null -ne $found) 'found without any variable set'
        Assert-Equal $sdk $found.Path 'path'
        Assert-Equal 'disk' $found.Source 'source'
        # Named How, not Where: a hashtable's `.Where` resolves to PowerShell's
        # built-in Where member rather than the key, which would have put a
        # method reference into a message meant for the product owner.
        Assert-True ($found.How.Length -gt 0) 'it can say how it found it'
    } finally {
        $env:VULKAN_SDK = $before
        $env:VK_SDK_PATH = $beforeAlt
    }
}

Test-Case 'the session is pointed at a found SDK rather than the user being sent to reboot' {
    $sdk = New-FakeVulkanSdk (New-TempPath 'sdk-adopt')
    $before = $env:VULKAN_SDK
    $beforeAlt = $env:VK_SDK_PATH
    $beforePath = $env:PATH
    try {
        $env:VULKAN_SDK = ''
        $env:VK_SDK_PATH = ''
        Set-VulkanSdkForSession -Path $sdk
        Assert-Equal $sdk $env:VULKAN_SDK 'VULKAN_SDK set'
        Assert-Equal $sdk $env:VK_SDK_PATH 'VK_SDK_PATH set too - the SDK sets both'
        Assert-Contains $env:PATH (Join-Path $sdk 'Bin')
        # Running setup twice must not put the same folder on PATH twice.
        Set-VulkanSdkForSession -Path $sdk
        $hits = @(($env:PATH -split ';') | Where-Object { $_ -and $_.TrimEnd('\') -ieq (Join-Path $sdk 'Bin') })
        Assert-Equal 1 $hits.Count 'Bin appears once'
    } finally {
        $env:VULKAN_SDK = $before
        $env:VK_SDK_PATH = $beforeAlt
        $env:PATH = $beforePath
    }
}

Test-Case 'the registry and installed-programs routes answer without throwing' {
    # Both read the real machine, so what they return depends on it. What is
    # tested is that they answer at all rather than blowing up the install, and
    # that whatever they do return is a real SDK.
    foreach ($source in @('registry', 'programs')) {
        $found = Resolve-VulkanSdk -Sources @($source)
        if ($null -ne $found) { Assert-True (Test-VulkanSdkDir $found.Path) "$source returned a real SDK" }
    }
}

# ===========================================================================
Write-Host ''
Write-Host 'Reporting what winget actually did' -ForegroundColor Cyan
# ===========================================================================

Test-Case 'winget exit codes are shown in both the forms they get looked up in' {
    # PowerShell hands back the signed number; Microsoft document them in hex.
    # This is the code the product owner's machine really returned.
    Assert-Contains (Format-ExitCode -1978335212) '-1978335212'
    Assert-Contains (Format-ExitCode -1978335212) '0x8A150014'
    Assert-Contains (Format-ExitCode 0) '0x00000000'
}

Test-Case 'the reason winget failed is pulled out of its output' {
    $output = @(
        'Found Vulkan SDK [KhronosGroup.VulkanSDK] Version 1.4.357.0',
        '  --------------------------  100%',
        'Downloading https://sdk.lunarg.com/sdk/download/1.4.357.0/windows/vulkan_sdk.exe',
        'Successfully verified installer hash',
        'Starting package install...',
        'Installer failed with exit code: 1603'
    ) -join "`r`n"
    Assert-Equal 'Installer failed with exit code: 1603' (Get-WingetFailureSummary -Output $output) 'the telling line'
}

Test-Case "winget's no-such-package answer survives to the screen" {
    # This is exactly what his machine printed for LunarG.VulkanSDK.
    $output = "   -`r   \`r   |`r`nNo package found matching input criteria.`r`n"
    Assert-Equal 'No package found matching input criteria.' (Get-WingetFailureSummary -Output $output) 'the answer'
    Assert-Equal '' (Get-WingetFailureSummary -Output '') 'nothing in, nothing out'
    Assert-Equal '' (Get-WingetFailureSummary -Output "  ---`r`n  50%`r`n") 'progress bars are not a reason'
}

Test-Case 'a failed install is reported as a failed install, with the evidence' {
    $note = Get-WingetFailureNote -Name 'Vulkan SDK' -Id 'LunarG.VulkanSDK' `
        -ExitCode -1978335212 -Said 'No package found matching input criteria.'
    Assert-Contains $note 'did not install'
    Assert-Contains $note 'LunarG.VulkanSDK'
    Assert-Contains $note '0x8A150014'
    Assert-Contains $note 'No package found matching input criteria.'
    # And it explains what that particular answer means, because "no package
    # found" is winget saying the identifier is wrong - not that the PC is.
    Assert-Contains $note 'identifier'
    # It must never claim the opposite of what happened.
    if ($note -match '(?i)installed, but|has not made it visible|restart') {
        throw "the failure note suggested the SDK installed: $note"
    }
}

# ===========================================================================
Write-Host ''
Write-Host 'What the user is told when the SDK is genuinely absent' -ForegroundColor Cyan
# ===========================================================================

Test-Case 'the not-installed message says not installed, and never says restart' {
    $report = Get-VulkanSdkMissingReport -WingetId 'KhronosGroup.VulkanSDK' `
        -WingetExitCode -1978335212 -WingetSaid 'No package found matching input criteria.' `
        -SearchedRoots @('C:\VulkanSDK')

    Assert-Contains $report.Problem 'not installed on this PC'
    Assert-Contains $report.Problem 'C:\VulkanSDK'
    Assert-Contains $report.Problem 'KhronosGroup.VulkanSDK'
    Assert-Contains $report.Problem 'No package found matching input criteria.'

    # The defect being fixed, asserted directly: this message used to claim the
    # SDK had installed and ask for a reboot that could never help.
    if ($report.Problem -match '(?i)installed, but Windows|has not made it visible') {
        throw "the message still claims the SDK installed: $($report.Problem)"
    }
    # The next action may only MENTION restarting in order to say it is
    # unnecessary. The instruction that sent him round the loop - "Restart the
    # PC and run this setup again" - must be gone.
    if ($report.NextAction -match '(?i)restart the pc[\s,]*(and|then)\s+run') {
        throw "the next action still tells the user to restart the PC: $($report.NextAction)"
    }
    Assert-Contains $report.NextAction 'do NOT need to restart'
}

Test-Case 'the not-installed message gives a route a non-developer can follow' {
    $report = Get-VulkanSdkMissingReport -SearchedRoots @('C:\VulkanSDK')
    Assert-Contains $report.NextAction 'https://vulkan.lunarg.com/sdk/home#windows'
    Assert-Contains $report.NextAction 'https://sdk.lunarg.com/sdk/download/latest/windows/vulkan_sdk.exe'
    Assert-Contains $report.NextAction 'vulkan_sdk.exe'
    Assert-Contains $report.NextAction 'run this setup again'
    # Numbered steps, not "go and figure it out".
    Assert-Contains $report.NextAction '1.'
    Assert-Contains $report.NextAction '4.'
    Assert-Contains $report.NextAction 'Everything already installed and downloaded is kept.'
}

Test-Case 'when the direct download failed too, the message says so as well' {
    $report = Get-VulkanSdkMissingReport -WingetId 'KhronosGroup.VulkanSDK' -WingetExitCode 1 `
        -WingetSaid 'Installer failed with exit code: 1603' `
        -DirectReason 'the download from LunarG did not finish (the remote name could not be resolved)'
    Assert-Contains $report.Problem 'straight from LunarG'
    Assert-Contains $report.Problem 'could not be resolved'
    # Both attempts are named, so it is clear what was tried.
    Assert-Contains $report.Problem 'Installer failed with exit code: 1603'
}

Test-Case 'the LunarG download addresses are the ones LunarG publish' {
    Assert-Equal 'https://sdk.lunarg.com/sdk/download/latest/windows/vulkan_sdk.exe' `
        (Get-LunarGSdkInstallerUri) 'the latest-installer address'
    Assert-Equal 'https://sdk.lunarg.com/sdk/download/1.4.357.0/windows/vulkan_sdk.exe' `
        (Get-LunarGSdkInstallerUri -Version '1.4.357.0') 'a pinned-version address'
    Assert-Equal 'https://vulkan.lunarg.com/sdk/latest/windows.txt' (Get-LunarGSdkVersionUri) 'the version address'
}

Test-Case "a version answer that is not a version is not used to build a URL" {
    # If LunarG's endpoint ever answers with an error page or a redirect, the
    # "latest" address is used rather than a nonsense one.
    Assert-Equal '1.4.357.0' (ConvertTo-LunarGSdkVersion "1.4.357.0`n") 'a real answer'
    Assert-Equal '1.3.290' (ConvertTo-LunarGSdkVersion '  1.3.290  ') 'three parts is fine'
    Assert-Equal '' (ConvertTo-LunarGSdkVersion '<html><body>404</body></html>') 'an error page'
    Assert-Equal '' (ConvertTo-LunarGSdkVersion '') 'nothing at all'
    Assert-Equal 'https://sdk.lunarg.com/sdk/download/latest/windows/vulkan_sdk.exe' `
        (Get-LunarGSdkInstallerUri -Version (ConvertTo-LunarGSdkVersion 'not a version')) 'falls back to latest'
}

Test-Case 'a downloaded installer is not run unless Windows vouches for it' {
    # Nothing downloaded from the internet gets run as administrator on the
    # strength of the URL alone. A truncated download fails this check too,
    # which is what stands in for the SHA-256 a "latest" URL cannot have.
    $unsigned = New-TempPath 'pretend-installer.exe'
    Set-Content -LiteralPath $unsigned -Value 'not really an installer' -Encoding ASCII
    $result = Test-InstallerSignature -Path $unsigned -ExpectedSubject 'LunarG'
    Assert-False $result.Ok 'an unsigned file is refused'
    Assert-True ($result.Reason.Length -gt 0) 'and it says why'

    $missing = Test-InstallerSignature -Path (New-TempPath 'never-downloaded.exe') -ExpectedSubject 'LunarG'
    Assert-False $missing.Ok 'a file that is not there is refused'
}

Test-Case 'a signature by the wrong publisher is refused' {
    Assert-True (Test-SignerSubject -Subject 'CN=LunarG, Inc., O=LunarG, C=US' -Expected 'LunarG') 'the right publisher'
    Assert-False (Test-SignerSubject -Subject 'CN=Some Other Company' -Expected 'LunarG') 'the wrong publisher'
    Assert-False (Test-SignerSubject -Subject '' -Expected 'LunarG') 'no publisher at all'
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
        $run = Invoke-Tool -FilePath $python.Path -Arguments @($probe, $file)
        if ($run.ExitCode -ne 0) {
            throw ("python could not parse the config we wrote: " + (Get-LastLine $run.Output))
        }
        Assert-Equal 'C:\dictate-gpu\models\ggml-large-v3-turbo.bin' (Get-LastLine $run.Output) 'python-parsed model path'
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
    # No double quotes in that argument: Windows PowerShell does not escape them
    # when it hands an argument to a native program, so the receiving program
    # gets something it cannot parse. setup.ps1 avoids them for the same reason.
    $run = Invoke-Tool -FilePath $python.Path -Arguments @('-c',
        'import sys; print(''Cloning into whisper.cpp...'', file=sys.stderr); print(''done'')')
    Assert-Equal 0 $run.ExitCode 'exit code'
    Assert-Contains $run.Output 'Cloning into'
    Assert-Contains $run.Output 'done'
}

Test-Case 'a missing program is a failure, not a silent success' {
    # The hazard: PowerShell leaves $LASTEXITCODE holding whatever the previous
    # command set, so a program that is not installed at all can look like it
    # succeeded and the install carries on past a step that never ran.
    $python = Get-PythonCommand -MinimumVersion '3.8'
    if (-not $python) { throw 'no Python to test with' }
    $ok = Invoke-Tool -FilePath $python.Path -Arguments @('-c', 'pass')
    Assert-Equal 0 $ok.ExitCode 'a real command still succeeds'

    $missing = Invoke-Tool -FilePath 'a-program-that-is-not-installed-anywhere' -Arguments @('--version')
    Assert-True ($missing.ExitCode -ne 0) 'a missing program reports a failure'
    Assert-Contains $missing.Output 'was not found'
}

Test-Case 'a program that fails reports its exit code rather than throwing' {
    $python = Get-PythonCommand -MinimumVersion '3.8'
    if (-not $python) { throw 'no Python to test with' }
    $ErrorActionPreference = 'Stop'
    $run = Invoke-Tool -FilePath $python.Path -Arguments @('-c', 'import sys; sys.exit(3)')
    Assert-Equal 3 $run.ExitCode 'exit code'
}

Test-Case 'a blank line on stderr stays blank instead of naming a .NET type' {
    # The bug: `2>&1` wraps every stderr line in an ErrorRecord whose Exception
    # is a RemoteException carrying the line as its Message. For a BLANK line
    # that Message is empty, and ErrorRecord.ToString() then falls through to
    # Exception.ToString() - which, for an exception that was never thrown, is
    # nothing but its type name. The product owner was shown
    # "System.Management.Automation.RemoteException" twice in the middle of an
    # install report: once for each blank line dictate printed around its own
    # error message.
    $empty = New-Object System.Exception('')
    $record = New-Object System.Management.Automation.ErrorRecord(
        $empty, 'NativeCommandError',
        [System.Management.Automation.ErrorCategory]::NotSpecified, $null)

    # What .ToString() makes of it is recorded rather than asserted - it is
    # PowerShell's behaviour, not ours, and the end-to-end case below is the
    # real proof. What IS asserted is that our own reader keeps it blank.
    Write-Host "        (ErrorRecord.ToString() on a blank line: [$($record.ToString())])" -ForegroundColor DarkGray
    Assert-Equal '' (Get-NativeOutputLine $record) 'a blank stderr line'

    $real = New-Object System.Management.Automation.ErrorRecord(
        (New-Object System.Exception('could not find the model file')),
        'NativeCommandError',
        [System.Management.Automation.ErrorCategory]::NotSpecified, $null)
    Assert-Equal 'could not find the model file' (Get-NativeOutputLine $real) 'a real stderr line'

    Assert-Equal 'plain text' (Get-NativeOutputLine 'plain text') 'ordinary stdout'
    Assert-Equal '' (Get-NativeOutputLine $null) 'nothing at all'
}

Test-Case 'a program whose message is framed by blank lines comes through whole' {
    # The same thing end to end, through a real native command on this real
    # Windows PowerShell - which is the only place the wrapping happens at all.
    # dictate prints exactly this shape: a blank line, the message, a blank
    # line. Every word of it has to survive, and no type name may appear.
    $python = Get-PythonCommand -MinimumVersion '3.8'
    if (-not $python) { throw 'no Python to test with' }
    # chr(10) and single quotes rather than "\n" and double quotes: Windows
    # PowerShell 5.1 mangles double quotes on their way into a native command's
    # argument list, and this test is about the OUTPUT, not about that.
    $snippet = "import sys; sys.stderr.write(chr(10) + '127.0.0.1:8178 is already in use' + chr(10) + '  -> dictate stop' + chr(10) + chr(10))"
    $run = Invoke-Tool -FilePath $python.Path -Arguments @('-c', $snippet)

    Assert-Contains $run.Output '127.0.0.1:8178 is already in use'
    Assert-Contains $run.Output 'dictate stop'
    if ($run.Output -match 'System\.Management\.Automation') {
        throw "a .NET type name leaked into what the user is shown: $($run.Output)"
    }
    if ($run.Output -match 'RemoteException') {
        throw "RemoteException leaked into what the user is shown: $($run.Output)"
    }
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


# ---------------------------------------------------------------------------
# Starting when he logs in: the decision, not the Task Scheduler call
# ---------------------------------------------------------------------------

Test-Case 'an install with nobody at the keyboard never registers a logon task' {
    # The rule: a product does not add itself to Windows startup unless it was
    # asked to. An unattended run is not an answer of yes.
    Assert-Equal 'leave' (Get-AutostartPlan -Requested 'ask' -Installing $true -CanAsk $false)
}

Test-Case 'an install with somebody there is asked the question' {
    Assert-Equal 'ask' (Get-AutostartPlan -Requested 'ask' -Installing $true -CanAsk $true)
}

Test-Case '-Autostart yes and no are taken at their word and never ask' {
    Assert-Equal 'enable' (Get-AutostartPlan -Requested 'yes' -Installing $true -CanAsk $true)
    Assert-Equal 'enable' (Get-AutostartPlan -Requested 'yes' -Installing $true -CanAsk $false)
    Assert-Equal 'leave' (Get-AutostartPlan -Requested 'no' -Installing $true -CanAsk $true)
}

Test-Case 'a run that installs nothing cannot change what happens at logon' {
    # -Only verify is somebody checking an installation, not installing one.
    foreach ($requested in @('ask', 'yes', 'no')) {
        Assert-Equal 'leave' (Get-AutostartPlan -Requested $requested -Installing $false -CanAsk $true)
    }
}

Test-Case 'the question names both answers and how to change your mind' {
    $question = Get-AutostartQuestion
    Assert-Contains $question 'start by itself when you log in'
    Assert-Contains $question 'dictate run'
    Assert-Contains $question 'dictate autostart disable'
}

Test-Case 'a question nobody answers gives up rather than hanging the install' {
    # Setup runs unattended for half an hour and is often walked away from. A
    # prompt that waits forever is the one failure this must not have.
    $started = Get-Date
    $answer = Read-YesNoWithTimeout -Prompt 'Answer nothing at all.' -TimeoutSeconds 1 -Default $false
    $elapsed = ((Get-Date) - $started).TotalSeconds
    Assert-False $answer 'no answer means no'
    if ($elapsed -gt 20) { throw "it waited $elapsed seconds for an answer nobody gave" }
}

Test-Case 'setup reads the state back out of dictate rather than keeping its own' {
    # Whatever the tray tick says and whatever `dictate autostart status` says
    # are the same answer, because this is the only thing setup reads.
    Assert-True (Test-AutostartStatusOn -Output "start at logon:  ON`r`n  task: ...") 'ON is on'
    Assert-False (Test-AutostartStatusOn -Output "start at logon:  OFF`r`n") 'OFF is off'
}

Test-Case 'an answer setup cannot read is not turned into "it is off"' {
    foreach ($output in @('', 'start at logon:  REGISTERED BUT DISABLED',
                          'start at logon:  not available on linux - it is a Windows scheduled task',
                          'something else entirely')) {
        $state = Test-AutostartStatusOn -Output $output
        if ($null -ne $state) { throw "[$output] should have been unreadable, was [$state]" }
    }
}

# ===========================================================================
Write-Host ''
Write-Host 'A file another program is holding open' -ForegroundColor Cyan
# ===========================================================================
#
# This is the whole of the product owner's install failure, in the one form a
# machine with no Windows can never produce: a real handle, held by a real
# second process, on a real file. Everything below runs on a GitHub Windows
# runner under the same Windows PowerShell 5.1 he has.

# File locks are MANDATORY on Windows and advisory everywhere else, so a test
# that opens a file and expects the next opener to be refused only means
# something on Windows. Those are skipped elsewhere rather than failed: a red
# line that only says "this is not Windows" trains people to ignore red lines.
$script:IsRealWindows = ([System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT)

function Test-WindowsCase {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][scriptblock]$Body
    )
    if (-not $script:IsRealWindows) {
        Skip-Case $Name 'needs real Windows - file locks are only mandatory there'
        return
    }
    Test-Case $Name $Body
}

Test-Case 'a file nothing is holding is replaceable' {
    $path = New-TempPath 'free.exe'
    [IO.File]::WriteAllText($path, 'x')
    $state = Test-FileReplaceable -Path $path
    Assert-True $state.Replaceable 'nothing holds it'
    Assert-Equal 'ok' $state.Reason 'reason'
}

Test-Case 'a file that is not there is not reported as held' {
    # The first install on a new PC has no Scripts\dictate.exe yet, and a wait
    # that treated "absent" as "held" would refuse every clean install.
    $state = Test-FileReplaceable -Path (New-TempPath 'never-existed.exe')
    Assert-True $state.Replaceable 'absent is replaceable'
    Assert-Equal 'absent' $state.Reason 'reason'
}

Test-WindowsCase 'a file this process is holding open is reported as held, with the number Windows gave' {
    $path = New-TempPath 'held.exe'
    [IO.File]::WriteAllText($path, 'x')
    # FileShare.Read: readers welcome, writers refused - which is what a running
    # program's own image, and an antivirus scanner reading a freshly written
    # executable, both look like from the outside.
    $handle = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    try {
        $state = Test-FileReplaceable -Path $path
        Assert-False $state.Replaceable 'it is held'
        Assert-Equal 'held' $state.Reason 'reason'
        # 32 is ERROR_SHARING_VIOLATION. The number is kept rather than
        # flattened because 5 and 32 mean different things to the person reading
        # the message.
        Assert-Equal 32 $state.Code 'the Windows error number'
    } finally {
        $handle.Dispose()
    }
}

Test-WindowsCase 'the wait gives up rather than hanging, and hands back what is still held' {
    $path = New-TempPath 'stuck.exe'
    [IO.File]::WriteAllText($path, 'x')
    $handle = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    try {
        $clock = [Diagnostics.Stopwatch]::StartNew()
        $wait = Wait-ForFilesReplaceable -Paths @($path) -TimeoutSeconds 2
        $clock.Stop()
        Assert-False $wait.Ok 'it never became replaceable'
        Assert-Equal 1 $wait.Blocked.Count 'the one file that is still held'
        Assert-Equal $path $wait.Blocked[0].Path 'and it is named'
        if ($clock.Elapsed.TotalSeconds -gt 15) {
            throw "it waited $($clock.Elapsed.TotalSeconds) seconds for a 2 second timeout"
        }
    } finally {
        $handle.Dispose()
    }
}

function Start-FileHolderProcess {
    <# Another process, holding a real handle on $Path, letting go after
       $HoldMilliseconds. This is the shape of the thing that broke his install
       - a holder that is on its way out - and it has to be a separate process
       to be that shape. Returns the process, or $null if it never took hold. #>
    param([string]$Path, [int]$HoldMilliseconds)
    $command = "`$h = [IO.File]::Open('$Path', 'Open', 'Read', 'Read'); " +
    "Start-Sleep -Milliseconds $HoldMilliseconds; `$h.Dispose()"
    $proc = Start-Process -FilePath 'powershell.exe' -PassThru -WindowStyle Hidden `
        -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', $command)
    # Do not start timing until it really has the file: a PowerShell that takes
    # a second to start would otherwise make this test measure process startup.
    $deadline = [DateTime]::UtcNow.AddSeconds(20)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (-not (Test-FileReplaceable -Path $Path).Replaceable) { return $proc }
        Start-Sleep -Milliseconds 100
    }
    return $null
}

Test-WindowsCase 'the wait is what makes a transient holder survivable' {
    # "Our check passed, and the very next operation was refused" is the
    # signature of a holder on its way out. Here one lets go after two and a
    # half seconds, and the install carries on instead of stopping - which is
    # the whole reason the wait exists.
    $path = New-TempPath 'transient.exe'
    [IO.File]::WriteAllText($path, 'x')
    $proc = Start-FileHolderProcess -Path $path -HoldMilliseconds 2500
    if (-not $proc) { throw 'could not get a second process to hold the file' }
    try {
        $wait = Wait-ForFilesReplaceable -Paths @($path) -TimeoutSeconds 30
        Assert-True $wait.Ok 'it let go and the wait noticed'
        if ($wait.WaitedSeconds -lt 0.5) {
            throw "it should have waited for the holder, but returned after $($wait.WaitedSeconds)s"
        }
    } finally {
        try { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue } catch { }
    }
}

Test-WindowsCase 'Windows is asked who is holding it, and it names the process that is' {
    # The Restart Manager is the only thing on a stock Windows that can answer
    # "which program has this file open". If it ever stops working the report
    # falls back rather than failing, so this test proves the primary route
    # works - and names a SEPARATE process, which is the case that matters.
    $path = New-TempPath 'named.dat'
    [IO.File]::WriteAllText($path, 'x')
    $proc = Start-FileHolderProcess -Path $path -HoldMilliseconds 30000
    if (-not $proc) { throw 'could not get a second process to hold the file' }
    try {
        if (-not (Add-RestartManagerType)) {
            throw 'the Restart Manager would not load on this Windows'
        }
        $holders = @(Get-FileHolder -Path $path)
        $named = @($holders | Where-Object { $_.Pid -eq $proc.Id })
        if ($named.Count -ne 1) {
            throw ("Windows should have named process $($proc.Id) as holding $path; it named: " +
                (($holders | ForEach-Object { "$($_.Name)/$($_.Pid)" }) -join ', '))
        }
        Assert-Equal 'windows' $named[0].Source 'it came from the Restart Manager, not the fallback'
        if (-not $named[0].Name) { throw 'Windows named a process with no name at all' }
    } finally {
        try { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue } catch { }
    }
}

Test-Case 'a folder that can be written to is told apart from one that cannot' {
    Assert-True (Test-FolderWritable -Path $script:TempRoot).Writable 'the temp folder is writable'
    $absent = Test-FolderWritable -Path (New-TempPath 'no-such-folder')
    Assert-False $absent.Writable 'a folder that is not there'
}

Test-WindowsCase 'the file-lock message says what is holding it and never mentions the internet' {
    $path = New-TempPath 'reported.exe'
    [IO.File]::WriteAllText($path, 'x')
    $handle = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    try {
        $wait = Wait-ForFilesReplaceable -Paths @($path) -TimeoutSeconds 2
        $report = Get-FileLockReport -Blocked $wait.Blocked -WaitedSeconds $wait.WaitedSeconds
        Assert-Contains $report.Problem $path
        Assert-Contains $report.Problem 'in use by another program'
        Assert-Contains $report.NextAction 'dictate stop'
        Assert-Contains $report.NextAction 'Task Manager'
        Assert-Contains $report.NextAction 'antivirus'
        # The whole point. The message this replaced sent him to look at his
        # internet connection and his proxy settings for this exact failure.
        if ($report.Problem -match '(?i)internet|proxy|online|pypi') {
            throw "the file-lock problem blamed the network: $($report.Problem)"
        }
        if ($report.NextAction -notmatch '(?i)nothing to do with your internet') {
            throw 'the file-lock message does not rule the network out by name'
        }
        # It knows the difference between one held file and an unwritable folder.
        Assert-Contains $report.Problem 'this is one file being held open'
    } finally {
        $handle.Dispose()
    }
}

# ===========================================================================
Write-Host ''
Write-Host 'Reading a failure rather than assuming one' -ForegroundColor Cyan
# ===========================================================================

Test-Case "the product owner's own pip output is read as a locked file, not a network problem" {
    # Verbatim from the run that stopped him, with the path generalised.
    $output = @'
Obtaining file:///C:/dictate-gpu/dictate
  Installing build dependencies: started
  Installing build dependencies: finished with status 'done'
Installing collected packages: dictate
  Attempting uninstall: dictate
    Found existing installation: dictate 0.1.0
    Uninstalling dictate-0.1.0:
ERROR: Could not install packages due to an OSError: [WinError 5] Access is denied: 'c:\python311\scripts\dictate.exe'
Check the permissions.
'@
    Assert-Equal 'locked' (Get-PipFailureKind -Output $output) 'what pip showed'
    Assert-Equal 'c:\python311\scripts\dictate.exe' (Get-PipFailurePath -Output $output) 'the file it named'
}

Test-Case 'a sharing violation is read the same way' {
    $output = "ERROR: Could not install packages due to an OSError: [WinError 32] The process cannot access " +
    "the file because it is being used by another process: 'C:\Py\Scripts\dictate.exe'"
    Assert-Equal 'locked' (Get-PipFailureKind -Output $output) 'kind'
    Assert-Equal 'C:\Py\Scripts\dictate.exe' (Get-PipFailurePath -Output $output) 'path'
}

Test-Case 'a real network failure is still called a network failure' {
    foreach ($output in @(
            "WARNING: Retrying (Retry(total=4, connect=None, read=None, redirect=None, status=None)) after connection broken by 'NewConnectionError'",
            'ERROR: Could not find a version that satisfies the requirement pywin32 (from versions: none)',
            "ProxyError('Cannot connect to proxy.', NewConnectionError(...))",
            'ERROR: Could not install packages due to an OSError: HTTPSConnectionPool(host=pypi.org, port=443): Read timed out.')) {
        Assert-Equal 'network' (Get-PipFailureKind -Output $output) "[$output]"
    }
}

Test-Case 'a failure that establishes nothing is called unknown rather than guessed at' {
    foreach ($output in @(
            'ERROR: Failed building wheel for something',
            "error: subprocess-exited-with-error",
            '')) {
        Assert-Equal 'unknown' (Get-PipFailureKind -Output $output) "[$output]"
    }
}

Test-WindowsCase 'the install report for a locked file names the file and rules the network out' {
    $path = New-TempPath 'target.exe'
    [IO.File]::WriteAllText($path, 'x')
    $handle = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    try {
        $output = "ERROR: Could not install packages due to an OSError: [WinError 5] Access is denied: '$path'"
        $report = Get-InstallFailureReport -ExitCode 1 -Output $output -LogPath 'C:\log.txt'
        Assert-Equal 'locked' $report.Kind 'kind'
        Assert-Contains $report.Problem $path
        Assert-Contains $report.Problem 'pip said:'
        if ($report.Problem -match '(?i)internet|proxy|pypi') {
            throw "it blamed the network for a locked file: $($report.Problem)"
        }
        Assert-Contains $report.NextAction 'nothing to do with your internet'
    } finally {
        $handle.Dispose()
    }
}

Test-Case 'a file that has since been let go is reported as such rather than as still held' {
    # Describing a lock that has already gone would send him hunting for
    # something that is not there. Nothing has this file open by the time the
    # report is written, and the report says so.
    $path = New-TempPath 'let-go.exe'
    [IO.File]::WriteAllText($path, 'x')
    # -Targets is how setup.ps1 calls this: the files it was about to replace,
    # so the report works even when pip's message names no path at all.
    $output = 'ERROR: Could not install packages due to an OSError: [WinError 5] Access is denied'
    $report = Get-InstallFailureReport -ExitCode 1 -Output $output -Targets @($path)
    Assert-Equal 'locked' $report.Kind 'kind'
    Assert-Contains $report.Problem 'has since let go'
    Assert-Contains $report.Problem 'this was one file being held open'
    Assert-Contains $report.NextAction 'running setup again is very likely to work'
    Assert-Contains $report.NextAction 'nothing to do with your internet'
}

Test-Case 'the network branch is reached only by evidence, and says what it is for' {
    $report = Get-InstallFailureReport -ExitCode 1 -LogPath 'C:\log.txt' `
        -Output 'ERROR: Could not find a version that satisfies the requirement pywin32'
    Assert-Equal 'network' $report.Kind 'kind'
    Assert-Contains $report.NextAction 'pypi.org'
    Assert-Contains $report.NextAction 'C:\log.txt'
}

Test-Case 'a cause setup cannot establish is reported as one it cannot establish' {
    $report = Get-InstallFailureReport -ExitCode 2 -LogPath 'C:\log.txt' `
        -Output "Building wheel for pywin32 ...`nerror: command 'cl.exe' failed: No such file or directory"
    Assert-Equal 'unknown' $report.Kind 'kind'
    Assert-Contains $report.Problem 'Setup does not know why'
    Assert-Contains $report.Problem "cl.exe"
    # No cause is asserted, so no cause may be implied either.
    if ($report.Problem -match '(?i)internet|proxy' -or $report.NextAction -match '(?i)internet|proxy') {
        throw 'the unknown branch still mentions the network'
    }
}

Test-Case 'a tool that printed nothing telling still gets its last words shown' {
    $lines = @(Get-ToolErrorLines -Output "step one`nstep two`nstep three" -Limit 2)
    Assert-Equal 2 $lines.Count 'it falls back to the tail'
    Assert-Equal 'step three' $lines[1] 'the last thing it said'
}

Test-Case 'no output at all is not turned into a diagnosis' {
    $report = Get-InstallFailureReport -ExitCode 9 -Output ''
    Assert-Equal 'unknown' $report.Kind 'kind'
    Assert-Contains $report.Problem 'pip printed nothing that says'
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
