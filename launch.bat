@echo off
title Drop Cat Go Studio
echo ============================================
echo   Drop Cat Go Studio
echo   AI Video Production
echo ============================================
echo.

:: ── Python check ─────────────────────────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found on PATH.
    echo Install Python 3.11+ from https://python.org and add it to PATH.
    pause
    exit /b 1
)

:: ── ffmpeg check ─────────────────────────────────────────────────────────────
ffmpeg -version >nul 2>&1
if errorlevel 1 (
    echo WARNING: ffmpeg not found on PATH.
    echo   Video generation, Ken Burns, and batch tools require ffmpeg.
    echo   Download from https://ffmpeg.org/download.html
    echo.
)

:: ── Install/update Python dependencies ───────────────────────────────────────
echo Checking Python dependencies...
pip install -q -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo WARNING: Some dependencies may not have installed correctly.
    echo   Try: pip install -r requirements.txt
    echo.
)

:: ── Auto-launch Forge if not already running ─────────────────────────────────
python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7861/sdapi/v1/samplers', timeout=2)" >nul 2>&1
if errorlevel 1 (
    echo Forge SD not detected on port 7861.
    if exist "C:\forge\webui-user.bat" (
        echo   Found Forge at C:\forge -- starting automatically...
        start "Forge SD" /min cmd /c "cd /d C:\forge && set COMMANDLINE_ARGS=--api --nowebui && webui.bat"
        echo   Forge starting -- SD image generation will be available in ~60s
        echo.
    ) else (
        echo   Forge not found at C:\forge -- SD image generation unavailable.
        echo   To enable: install Forge and launch with --api flag.
        echo.
    )
) else (
    echo Forge SD: running on port 7861
)

:: ── Port 7860 check -- handle existing instances ─────────────────────────────
python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7860/api/system', timeout=2)" >nul 2>&1
if not errorlevel 1 (
    echo Drop Cat Go Studio is already running.
    echo   Opening in Chrome...
    start chrome http://127.0.0.1:7860
    exit /b 0
)

:: Port in use but not responding -- find and kill the stale process
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":7860 " ^| findstr "LISTENING"') do (
    echo Stale process %%p is holding port 7860 -- terminating...
    taskkill /PID %%p /F >nul 2>&1
)

:: ── Launch DropCat Studio ─────────────────────────────────────────────────────
echo.
echo Starting Drop Cat Go Studio...
echo   URL: http://127.0.0.1:7860
echo   Press Ctrl+C to stop.
echo.

:: Open Chrome after a short delay to let the server start
start "" /b cmd /c "timeout /t 3 /nobreak >nul && start chrome http://127.0.0.1:7860"

python "%~dp0app.py"

pause
