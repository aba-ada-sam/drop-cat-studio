@echo off
setlocal EnableDelayedExpansion

REM ============================================================
REM  DCS 3060 Satellite -- Full automated setup
REM  Double-click this on the 3060 machine.
REM  Installs ACE-Step, Forge, Ollama, configures all three for
REM  network access, creates C:\DCS-satellite\start_all.bat
REM  and adds it to Windows startup.
REM ============================================================

REM -- Elevate to Administrator if not already --
NET SESSION >nul 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo Requesting administrator rights...
    powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo.
echo ============================================================
echo  DCS 3060 Satellite Setup
echo ============================================================
echo.

REM -- Step 1: Firewall rules (instant) --
echo [1/7] Opening firewall ports...
netsh advfirewall firewall add rule name="DCS ACE-Step" dir=in action=allow protocol=TCP localport=8019 >nul 2>&1
netsh advfirewall firewall add rule name="DCS Ollama"   dir=in action=allow protocol=TCP localport=11434 >nul 2>&1
netsh advfirewall firewall add rule name="DCS Forge"    dir=in action=allow protocol=TCP localport=7861 >nul 2>&1
echo     Done.

REM -- Step 2: Install Ollama silently --
echo [2/7] Installing Ollama...
powershell -Command "Invoke-WebRequest -Uri 'https://ollama.com/download/OllamaSetup.exe' -OutFile '%TEMP%\OllamaSetup.exe' -UseBasicParsing" >nul 2>&1
IF EXIST "%TEMP%\OllamaSetup.exe" (
    start /wait "" "%TEMP%\OllamaSetup.exe" /S
    echo     Ollama installed.
) ELSE (
    echo     WARNING: Could not download Ollama installer. Check internet connection.
)

REM -- Step 3: Configure Ollama for network access --
echo [3/7] Configuring Ollama for network access...
setx OLLAMA_HOST "0.0.0.0:11434" /M >nul 2>&1
echo     OLLAMA_HOST set to 0.0.0.0:11434
taskkill /F /IM ollama.exe >nul 2>&1
timeout /t 2 /nobreak >nul
start /min "" ollama serve
timeout /t 5 /nobreak >nul
echo     Pulling AI model (qwen3-vl:8b -- this may take several minutes)...
ollama pull qwen3-vl:8b
echo     Ollama ready.

REM -- Step 4: Trigger Pinokio installs --
echo [4/7] Triggering Pinokio installs for ACE-Step and Forge...
echo     (Pinokio windows will open -- let them run, do not close them)
start "" "pinokio://install?url=https://github.com/cocktailpeanut/ace-step.pinokio"
timeout /t 3 /nobreak >nul
start "" "pinokio://install?url=https://github.com/pinokiofactory/stable-diffusion-webui-forge"
echo     Install requests sent to Pinokio.
echo.
echo     Waiting for downloads to complete (can take 10-60 min)...
echo     This window will continue automatically when both are done.
echo.

REM -- Step 5: Poll for Pinokio installs to finish --
:WAIT_LOOP
timeout /t 30 /nobreak >nul

REM Search for ACE-Step api_server.py
set ACE_ROOT=
for /d %%D in (C:\pinokio\api\*) do (
    if exist "%%D\app\acestep\api_server.py" set ACE_ROOT=%%D\app
    if exist "%%D\acestep\api_server.py"     set ACE_ROOT=%%D
)

REM Search for Forge webui-user.bat
set FORGE_ROOT=
for /d %%D in (C:\pinokio\api\*) do (
    if exist "%%D\app\webui-user.bat" set FORGE_ROOT=%%D\app
    if exist "%%D\webui-user.bat"     set FORGE_ROOT=%%D
)

set READY=1
IF "!ACE_ROOT!"==""   set READY=0
IF "!FORGE_ROOT!"=="" set READY=0

IF !READY!==0 (
    set /a ELAPSED+=30
    echo     Still waiting... (!ELAPSED!s elapsed) ACE:!ACE_ROOT! Forge:!FORGE_ROOT!
    IF !ELAPSED! LSS 5400 goto WAIT_LOOP
    echo.
    echo     WARNING: Timeout waiting for installs. Continuing with what was found.
)

echo.
echo     Found ACE-Step at: !ACE_ROOT!
echo     Found Forge at:    !FORGE_ROOT!
echo.

REM -- Step 6: Configure ACE-Step for network access --
echo [5/7] Configuring ACE-Step...
mkdir C:\DCS-satellite >nul 2>&1

REM Find the Python interpreter in the ACE-Step install
set PYTHON=
for %%P in (
    "!ACE_ROOT!\.venv\Scripts\python.exe"
    "!ACE_ROOT!\env\Scripts\python.exe"
    "!ACE_ROOT!\venv\Scripts\python.exe"
) do (
    if exist %%P if "!PYTHON!"=="" set PYTHON=%%~P
)

IF "!PYTHON!"=="" (
    REM Try uv as fallback
    where uv >nul 2>&1
    IF !ERRORLEVEL!==0 (
        echo     Using uv runner for ACE-Step
        (
            echo @echo off
            echo cd /d "!ACE_ROOT!"
            echo uv run --no-sync python acestep\api_server.py --host 0.0.0.0 --port 8019
        ) > "C:\DCS-satellite\start_acestep.bat"
    ) ELSE (
        echo     WARNING: No Python found for ACE-Step. Startup script may need manual fixing.
        (
            echo @echo off
            echo cd /d "!ACE_ROOT!"
            echo python acestep\api_server.py --host 0.0.0.0 --port 8019
        ) > "C:\DCS-satellite\start_acestep.bat"
    )
) ELSE (
    (
        echo @echo off
        echo cd /d "!ACE_ROOT!"
        echo "!PYTHON!" acestep\api_server.py --host 0.0.0.0 --port 8019
    ) > "C:\DCS-satellite\start_acestep.bat"
)
echo     Created C:\DCS-satellite\start_acestep.bat

REM -- Step 7: Configure Forge for network access --
echo [6/7] Configuring Forge...
IF NOT "!FORGE_ROOT!"=="" (
    REM Read webui-user.bat and add --listen --api if missing
    powershell -Command ^
        "$f = '!FORGE_ROOT!\webui-user.bat'; " ^
        "$c = Get-Content $f -Raw; " ^
        "if ($c -notmatch '--listen') { $c = $c -replace 'set COMMANDLINE_ARGS=(.*)', 'set COMMANDLINE_ARGS=$1 --listen --api --port 7861'; Set-Content $f $c; Write-Host 'Forge configured for network access' } " ^
        "else { Write-Host 'Forge already has --listen flag' }"
    echo     Forge configured.
) ELSE (
    echo     WARNING: Forge not found. Configure manually after install completes.
)

REM -- Step 8: Create unified startup script --
echo [7/7] Creating startup scripts...
(
    echo @echo off
    echo echo Starting DCS satellite services...
    echo start "ACE-Step" /min "C:\DCS-satellite\start_acestep.bat"
    IF NOT "!FORGE_ROOT!"=="" echo start "Forge" /min "!FORGE_ROOT!\webui-user.bat"
    echo echo Done. Ollama starts automatically as a Windows service.
) > "C:\DCS-satellite\start_all.bat"

REM Add to Windows startup
powershell -Command ^
    "$s = New-Object -ComObject WScript.Shell; " ^
    "$l = $s.CreateShortcut('$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\DCS-satellite.lnk'); " ^
    "$l.TargetPath = 'C:\DCS-satellite\start_all.bat'; $l.Save()"

echo     C:\DCS-satellite\start_all.bat created and added to Windows startup.

REM -- Start services now --
echo.
echo Starting services...
start "ACE-Step" /min "C:\DCS-satellite\start_acestep.bat"
IF NOT "!FORGE_ROOT!"=="" start "Forge" /min "!FORGE_ROOT!\webui-user.bat"

REM -- Get IP address --
for /f "tokens=2 delims=:" %%A in ('ipconfig ^| findstr /i "IPv4 Address"') do (
    set RAW_IP=%%A
    set RAW_IP=!RAW_IP: =!
    goto :GOT_IP
)
:GOT_IP

echo.
echo ============================================================
echo  SETUP COMPLETE
echo ============================================================
echo.
echo  This machine's IP: !RAW_IP!
echo.
echo  On the 5080 machine, open DCS Settings and enter:
echo    ACE-Step Host : !RAW_IP!
echo    Ollama URL    : http://!RAW_IP!:11434
echo    Forge URL     : http://!RAW_IP!:7861
echo  Then click Save Settings.
echo.
echo  Services start automatically at login via Windows startup.
echo  To start manually: double-click C:\DCS-satellite\start_all.bat
echo.
echo  NOTE: Assign this machine a static IP in your router so
echo  this address does not change.
echo ============================================================
echo.
pause
