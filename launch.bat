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
:: Probe 7860..7879. If any responds, open Chrome there and exit.
for /l %%P in (7860,1,7879) do (
    python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:%%P/api/system', timeout=1)" >nul 2>&1
    if not errorlevel 1 (
        echo Server is already running on port %%P.
        start chrome http://127.0.0.1:%%P
        exit /b 0
    )
)

:: -- Kill a previous DCS instance by PID (only if we own the port file) --
if exist ".dcs-port" (
    for /f "tokens=2 delims=:," %%p in ('findstr /c:"\"pid\"" ".dcs-port" 2^>nul') do (
        for /f "tokens=* delims= " %%q in ("%%p") do (
            taskkill /PID %%q /F >nul 2>&1
        )
    )
    del ".dcs-port" >nul 2>&1
)

:: -- Start the server --------------------------------------------------
:: The python server picks a free port from 7860..7879 and writes the
:: chosen port to .dcs-port. We poll for the file (up to 60s) so Chrome
:: opens at the right port even when Python startup takes 10-15s.
echo Starting server...
start "" /b "%~dp0open_browser.bat"
python "%~dp0app.py"

pause
