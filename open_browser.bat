@echo off
:: Poll .dcs-port until the server writes it (up to 60s), then open Chrome.
:: Called in background by launch.bat.
setlocal

set "ROOT=%~dp0"
set "PORT_FILE=%ROOT%.dcs-port"
set /a TRIES=0

:wait_loop
if exist "%PORT_FILE%" goto :read_port
timeout /t 1 >nul
set /a TRIES+=1
if %TRIES% LSS 60 goto :wait_loop
echo [DCS] Timed out waiting for server port file.
exit /b 1

:read_port
for /f "tokens=2 delims=:," %%p in ('findstr /c:"\"port\"" "%PORT_FILE%" 2^>nul') do (
    for /f "tokens=* delims= " %%q in ("%%p") do (
        start chrome --app=http://127.0.0.1:%%q/
        exit /b 0
    )
)
echo [DCS] Could not parse port from .dcs-port.
exit /b 1
