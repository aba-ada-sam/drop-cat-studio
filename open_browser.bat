@echo off
:: Poll .dcs-port until the server writes it (up to 60s), then open Chrome
:: pinned to the left third of the primary monitor so DCS doesn't cover other
:: apps the user has on screen (video players etc).
:: Called in background by launch.bat.
setlocal enabledelayedexpansion

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
:: Compute primary-screen dims via PowerShell (WMI / .NET) so we can size
:: the Chrome --app window to the left third. Falls back to 1280x720 if the
:: probe fails for any reason.
set "SCREEN_W=1920"
set "SCREEN_H=1080"
for /f "usebackq tokens=1,2 delims=," %%a in (`powershell -NoProfile -Command "Add-Type -AssemblyName System.Windows.Forms; $b=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds; \"$($b.Width),$($b.Height)\""`) do (
    set "SCREEN_W=%%a"
    set "SCREEN_H=%%b"
)
set /a WIN_W=!SCREEN_W! / 3
set /a WIN_H=!SCREEN_H!
:: Force minimum sane size in case PowerShell returned junk.
if !WIN_W! LSS 480 set WIN_W=640
if !WIN_H! LSS 480 set WIN_H=720

for /f "tokens=2 delims=:," %%p in ('findstr /c:"\"port\"" "%PORT_FILE%" 2^>nul') do (
    for /f "tokens=* delims= " %%q in ("%%p") do (
        start "" chrome --app=http://127.0.0.1:%%q/ --window-position=0,0 --window-size=!WIN_W!,!WIN_H!
        exit /b 0
    )
)
echo [DCS] Could not parse port from .dcs-port.
exit /b 1
