@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul

:: -- Immediate visual feedback (fires before git pull and server start) -----
:: pythonw suppresses the console window; splash closes when .dcs-port appears.
where pythonw >nul 2>&1 && start "" pythonw "%~dp0pre_splash.py"

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

:: -- Remove startup folder entry if we accidentally created it before ----
set "_STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\DropCatGoStudio.lnk"
if exist "%_STARTUP%" (
    echo Removing auto-startup entry ^(not needed^)...
    del "%_STARTUP%" >nul 2>&1
)

:: -- Create / refresh the desktop shortcut pointing to manager.pyw ------
:: manager.pyw owns git pull, splash, server start, tray, and single-instance mutex.
:: The shortcut is also self-healed by manager.pyw on each run.
set "_DESKTOP_LNK=%USERPROFILE%\Desktop\Drop Cat Go Studio.lnk"
for /f "delims=" %%i in ('where pythonw 2^>nul') do set "_PYTHONW=%%i" & goto :have_pythonw
:have_pythonw
if not defined _PYTHONW (
    for /f "delims=" %%i in ('where python 2^>nul') do (
        if exist "%%~dpi\pythonw.exe" (set "_PYTHONW=%%~dpi\pythonw.exe" & goto :have_pythonw2)
    )
)
:have_pythonw2
if defined _PYTHONW (
    powershell -NoProfile -Command ^
        "$ws=New-Object -ComObject WScript.Shell; $sc=$ws.CreateShortcut('%_DESKTOP_LNK%'); $sc.TargetPath='%_PYTHONW%'; $sc.Arguments='\"\"\"^%~dp0manager.pyw\"\"\"'; $sc.WorkingDirectory='%~dp0'; $sc.IconLocation='%~dp0static\favicon.ico,0'; $sc.Description='Drop Cat Go Studio'; $sc.Save()" >nul 2>&1
)

:: -- Auto-update from GitHub --------------------------------------------
:: Strip trailing backslash from %~dp0 so git -C "path\" doesn't mis-parse the quote.
set "_REPO=%~dp0"
if "%_REPO:~-1%"=="\" set "_REPO=%_REPO:~0,-1%"

set _GOT_UPDATE=0
git --version >nul 2>&1
if not errorlevel 1 (
    echo Checking for updates...
    for /f %%i in ('git -C "%_REPO%" rev-parse HEAD 2^>nul') do set _SHA_BEFORE=%%i
    git -C "%_REPO%" pull --ff-only origin master >nul 2>&1
    for /f %%i in ('git -C "%_REPO%" rev-parse HEAD 2^>nul') do set _SHA_AFTER=%%i
    if not "!_SHA_BEFORE!"=="!_SHA_AFTER!" (
        echo New version pulled -- restarting server if running.
        set _GOT_UPDATE=1
    ) else (
        echo Already up to date.
    )
) else (
    echo [update] Git not on PATH -- skipping update check.
)

:: -- Install / update dependencies only when new code was pulled --------
if "%_GOT_UPDATE%"=="1" (
    echo Updating dependencies...
    pip install -q -r "%~dp0requirements.txt" 2>nul
)

:: -- Check if server is already running (read .dcs-port first, no blind scan) -
set _RUNNING_PORT=0
if exist "%~dp0.dcs-port" (
    for /f "tokens=2 delims=:," %%p in ('findstr /c:"\"port\"" "%~dp0.dcs-port" 2^>nul') do (
        for /f "tokens=* delims= " %%q in ("%%p") do (
            python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:%%q/api/system', timeout=2)" >nul 2>&1
            if not errorlevel 1 set _RUNNING_PORT=%%q
        )
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
    ) else (
        echo Server running on port !_RUNNING_PORT! -- opening app.
        start chrome --app=http://127.0.0.1:!_RUNNING_PORT!/
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
