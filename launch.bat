@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul
title Drop Cat Go Studio

echo ============================================
echo   Drop Cat Go Studio  -  AI Video Production
echo ============================================

:: -- Python check -------------------------------------------------------
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found on PATH.
    echo Install Python 3.11+ from https://python.org
    pause
    exit /b 1
)

:: -- Register in Windows Startup (one-time, silent) ---------------------
:: After this the server starts automatically every time you log into Windows.
:: You only ever need to click the PWA desktop icon to open the app.
set "_STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\DropCatGoStudio.lnk"
if not exist "%_STARTUP%" (
    echo Registering Drop Cat Go Studio to start with Windows...
    powershell -NoProfile -Command ^
        "$ws=$([Runtime.InteropServices.Marshal]::GetActiveObject('WScript.Shell') 2>$null); if(-not $ws){$ws=New-Object -ComObject WScript.Shell}; $sc=$ws.CreateShortcut('%_STARTUP%'); $sc.TargetPath='%~dpnx0'; $sc.WorkingDirectory='%~dp0'; $sc.WindowStyle=7; $sc.Save()" 2>nul
    if not exist "%_STARTUP%" (
        :: Fallback if COM fails
        powershell -NoProfile -Command ^
            "$ws=New-Object -ComObject WScript.Shell; $sc=$ws.CreateShortcut('%_STARTUP%'); $sc.TargetPath='%~dpnx0'; $sc.WorkingDirectory='%~dp0'; $sc.WindowStyle=7; $sc.Save()"
    )
    if exist "%_STARTUP%" (
        echo Done. The server will now start automatically on login.
    ) else (
        echo [warn] Could not create startup entry -- you can run launch.bat manually.
    )
)

:: -- Auto-update from GitHub --------------------------------------------
set _GOT_UPDATE=0
git --version >nul 2>&1
if not errorlevel 1 (
    echo Checking for updates...
    for /f %%i in ('git -C "%~dp0" rev-parse HEAD 2^>nul') do set _SHA_BEFORE=%%i
    git -C "%~dp0" pull --ff-only origin master 2>&1
    for /f %%i in ('git -C "%~dp0" rev-parse HEAD 2^>nul') do set _SHA_AFTER=%%i
    if not "!_SHA_BEFORE!"=="!_SHA_AFTER!" (
        echo New version pulled -- will restart server if it is running.
        set _GOT_UPDATE=1
    ) else (
        echo Already up to date.
    )
) else (
    echo [update] Git not on PATH -- skipping update check.
)

:: -- Install dependencies (only if updated) -----------------------------
if "%_GOT_UPDATE%"=="1" (
    echo Updating dependencies...
    pip install -q -r "%~dp0requirements.txt" 2>nul
)

:: -- Check if server is already running ---------------------------------
set _RUNNING_PORT=0
for /l %%P in (7860,1,7879) do (
    if "!_RUNNING_PORT!"=="0" (
        python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:%%P/api/system', timeout=1)" >nul 2>&1
        if not errorlevel 1 set _RUNNING_PORT=%%P
    )
)

if not "!_RUNNING_PORT!"=="0" (
    if "%_GOT_UPDATE%"=="1" (
        echo Restarting server to apply updates...
        if exist "%~dp0.dcs-port" (
            for /f "tokens=2 delims=:," %%p in ('findstr /c:"\"pid\"" "%~dp0.dcs-port" 2^>nul') do (
                for /f "tokens=* delims= " %%q in ("%%p") do taskkill /PID %%q /F >nul 2>&1
            )
            del "%~dp0.dcs-port" >nul 2>&1
        )
        :: Fall through to start a fresh server below
    ) else (
        echo Server is already running on port !_RUNNING_PORT! ^(no changes^).
        start "" /b "%~dp0open_browser.bat"
        exit /b 0
    )
)

:: -- Kill any stale port file -------------------------------------------
if exist "%~dp0.dcs-port" (
    for /f "tokens=2 delims=:," %%p in ('findstr /c:"\"pid\"" "%~dp0.dcs-port" 2^>nul') do (
        for /f "tokens=* delims= " %%q in ("%%p") do taskkill /PID %%q /F >nul 2>&1
    )
    del "%~dp0.dcs-port" >nul 2>&1
)

:: -- Start the server ---------------------------------------------------
echo Starting server...
start "" /b "%~dp0open_browser.bat"
python "%~dp0app.py"

pause
