@echo off
REM Quick launcher for Drop Cat Go Studio
REM No local model install needed: the uncensored AI runs on Featherless cloud
REM (key in C:\JSON Credentials\featherless_api_key.txt) or a local KoboldCpp.

setlocal enabledelayedexpansion
title DropCat Studio V2

echo.
echo ============================================================
echo     DropCat Studio V2
echo     AI Video Production Suite
echo ============================================================
echo.

REM Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python 3.11+ required
    echo Install from https://python.org and add to PATH
    echo.
    pause
    exit /b 1
)

REM Check if server already running
python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7940/api/system', timeout=1)" >nul 2>&1
if not errorlevel 1 (
    echo Server already running. Opening Chrome...
    start chrome http://127.0.0.1:7940
    exit /b 0
)

REM Kill zombie processes on port 7940 (V2's own port -- never touches v1's 7860)
echo Cleaning up port 7940...
for /f "tokens=5" %%p in ('netstat -ano 2^>nul ^| findstr ":7940"') do (
    taskkill /PID %%p /T /F >nul 2>&1
)
timeout /t 1 >nul

REM Start server
echo Starting server...
start "" /b cmd /c "cd /d "%~dp0" && timeout /t 3 >nul && start chrome http://127.0.0.1:7940"
python "%~dp0app.py"

pause
