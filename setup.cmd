@echo off
rem ---------------------------------------------------------------------------
rem  dictate_v1 setup, for double-clicking.
rem
rem  This exists so the install does not begin with "open PowerShell and type a
rem  command with a flag in it". It asks Windows for administrator rights (the
rem  build tools cannot install without them), runs setup.ps1, and then waits so
rem  the window does not vanish before you have read what it said.
rem
rem  Everything it does is in setup.ps1. This file only starts it.
rem ---------------------------------------------------------------------------

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Windows will now ask for permission to install software.
    echo Click Yes on the prompt that appears.
    echo.
    if "%~1"=="" (
        powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    ) else (
        powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%*' -Verb RunAs"
    )
    exit /b 0
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
set DICTATE_SETUP_EXIT=%errorlevel%

echo.
echo Press any key to close this window.
pause >nul
exit /b %DICTATE_SETUP_EXIT%
