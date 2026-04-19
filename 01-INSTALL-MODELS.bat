@echo off
REM This script is the first thing you run after installing Ollama.
REM It downloads the three AI models that Drop Cat Go Studio needs.
REM This is a one-time setup (takes 30 min - 2 hours depending on internet speed).

setlocal enabledelayedexpansion
title [1/2] Install AI Models - Drop Cat Go Studio

echo.
echo ============================================================
echo     Drop Cat Go Studio - Model Installation
echo     Step 1 of 2: Install Required AI Models
echo ============================================================
echo.
echo This downloads 3 AI models (~25 GB total) that power all
echo creative features in Drop Cat Go Studio.
echo.
echo Requirements:
echo  - Ollama must be installed and running (https://ollama.ai)
echo  - ~25 GB free disk space
echo  - 30 minutes to 2 hours (depending on internet speed)
echo.
echo Press any key to continue...
echo.

pause

call install-ollama-models.bat

if errorlevel 1 (
    echo.
    echo ============================================================
    echo  Installation failed. Check the errors above.
    echo  Common fixes:
    echo  - Restart your computer after installing Ollama
    echo  - Check your internet connection
    echo  - Run as Administrator
    echo ============================================================
    echo.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  Models installed successfully!
echo.
echo  Next step: Run launch.bat to start the app
echo  Or: Double-click 02-START-APP.bat
echo ============================================================
echo.
pause
exit /b 0
