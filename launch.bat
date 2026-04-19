@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul
title Drop Cat Go Studio

echo ============================================
echo   Drop Cat Go Studio
echo   AI Video Production
echo ============================================

:: -- Python check -------------------------------------------------------
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found on PATH.
    echo Install Python 3.11+ from https://python.org
    pause
    exit /b 1
)

:: -- Install dependencies -----------------------------------------------
echo Checking dependencies...
pip install -q -r "%~dp0requirements.txt" 2>nul

:: -- Check if server is already running --------------------------------
python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7860/api/system', timeout=1)" >nul 2>&1
if not errorlevel 1 (
    echo Server is already running.
    start chrome http://127.0.0.1:7860
    exit /b 0
)

:: -- Kill any zombie processes on port 7860 ----------------------------
for /f "tokens=5" %%p in ('netstat -ano 2^>nul ^| findstr ":7860"') do (
    taskkill /PID %%p /T /F >nul 2>&1
)
timeout /t 2 >nul

:: -- Start the server --------------------------------------------------
echo Starting server...
start "" /b cmd /c "cd /d "%~dp0" && timeout /t 4 >nul && start chrome http://127.0.0.1:7860"
python "%~dp0app.py"

pause
