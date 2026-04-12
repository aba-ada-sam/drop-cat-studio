@echo off
:: Stop Drop Cat Go Studio server (kills process listening on port 7860)
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":7860 " ^| findstr "LISTENING" 2^>nul') do (
    echo Stopping server (PID %%p)...
    taskkill /PID %%p /F >nul 2>&1
    echo Done.
    goto :done
)
echo Server is not running on port 7860.
:done
pause
