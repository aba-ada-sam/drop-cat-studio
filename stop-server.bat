@echo off
:: Stop Drop Cat Go Studio server. Reads .dcs-port for the PID the
:: running server wrote at startup, and only kills that PID. If the
:: port file is missing we fall back to scanning 7860..7879 for a
:: python.exe we can match by port.

setlocal

if exist ".dcs-port" (
    for /f "tokens=2 delims=:," %%p in ('findstr /c:"\"pid\"" ".dcs-port" 2^>nul') do (
        for /f "tokens=* delims= " %%q in ("%%p") do (
            echo Stopping Drop Cat Go Studio (PID %%q)...
            taskkill /PID %%q /F >nul 2>&1
            del ".dcs-port" >nul 2>&1
            echo Done.
            goto :done
        )
    )
)

:: No .dcs-port — scan the likely range and kill whatever's listening.
:: This is a fallback only; normally the .dcs-port path hits.
set KILLED=0
for /l %%P in (7860,1,7879) do (
    for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%%P " ^| findstr "LISTENING" 2^>nul') do (
        echo Stopping process on port %%P (PID %%p)...
        taskkill /PID %%p /F >nul 2>&1
        set KILLED=1
    )
)
if "%KILLED%"=="0" echo Server is not running in range 7860..7879.

:done
pause
