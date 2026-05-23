@echo off
echo Starting DCS satellite services...
if not exist C:\DCS-satellite mkdir C:\DCS-satellite
copy /Y "%~dp0dcs_relay.py"  "C:\DCS-satellite\dcs_relay.py"  >nul
copy /Y "%~dp0dcs_backup.py" "C:\DCS-satellite\dcs_backup.py" >nul

REM Find Python
set PYTHON=
python --version >nul 2>&1 && set PYTHON=python && goto :run
for %%P in (
    "C:\pinokio\api\wan.git\app\.venv\Scripts\python.exe"
    "C:\Python312\python.exe"
    "C:\Python311\python.exe"
    "C:\Python310\python.exe"
    "C:\Users\%USERNAME%\AppData\Local\Programs\Python\Python312\python.exe"
    "C:\Users\%USERNAME%\AppData\Local\Programs\Python\Python311\python.exe"
    "Z:\Python310\python.exe"
) do (
    if exist %%P set PYTHON=%%P && goto :run
)
echo ERROR: Python not found.
pause & exit /b 1

:run
REM Main relay (port 9999)
start "DCS Relay"  /min %PYTHON% "C:\DCS-satellite\dcs_relay.py"

REM Backup panel (port 9998)
start "DCS Backup" /min %PYTHON% "C:\DCS-satellite\dcs_backup.py"

REM Enable WinRM for PowerShell remoting (fallback)
powershell -Command "Enable-PSRemoting -Force -SkipNetworkProfileCheck 2>$null; Set-Item WSMan:\localhost\Client\TrustedHosts -Value '*' -Force 2>$null" >nul 2>&1

for /f "tokens=2 delims=:" %%A in ('ipconfig ^| findstr /i "IPv4 Address"') do (
    set IP=%%A
    goto :show
)
:show
set IP=%IP: =%
echo.
echo  Relay:   http://%IP%:9999
echo  Backup:  http://%IP%:9998  ^<-- open this in browser if relay is down
echo.
echo  Startup complete. You can close this window.
timeout /t 5
