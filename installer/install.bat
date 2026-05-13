@echo off
title Drop Cat Go Studio -- Installer
echo.
echo  ============================================================
echo   Drop Cat Go Studio -- Automated Installer
echo   This window will run for 30-60 minutes.
echo   Do not close it until it says DONE.
echo  ============================================================
echo.

:: Check for admin rights
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo   Requesting administrator rights...
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

:: Run the PowerShell installer from the same folder as this .bat
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -File "%~dp0install.ps1"
pause
