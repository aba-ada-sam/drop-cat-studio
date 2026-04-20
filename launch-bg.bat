@echo off
:: Drop Cat Go Studio — Background launcher
:: Invoked by launch-silent.vbs inside a hidden cmd window (style=0).
:: Uses regular python.exe — the hidden console means no visible window.
:: Do NOT run this directly; use launch.bat for debugging.
::
:: Port flexibility: the python server picks the first free port from
:: 7860..7879 and writes it to .dcs-port. We no longer kill arbitrary
:: processes on port 7860 — that used to stomp on unrelated apps.
:: The server can coexist with Forge (7861), WanGP (7899), and anything
:: else that happened to grab 7860 before us.

cd /d "%~dp0"
if not exist logs mkdir logs

echo [%TIME%] launch-bg.bat started >> logs\server.log

:: ── Kill the previous DCS instance (if any) by PID from .dcs-port ────────────
:: This only touches OUR own previous python process — never someone else's.
if exist ".dcs-port" (
    for /f "tokens=2 delims=:," %%p in ('findstr /c:"\"pid\"" ".dcs-port" 2^>nul') do (
        for /f "tokens=* delims= " %%q in ("%%p") do (
            echo [%TIME%] Killing prior DCS PID %%q >> logs\server.log
            taskkill /PID %%q /F >nul 2>&1
        )
    )
    del ".dcs-port" >nul 2>&1
)

:: ── Run the server (blocks until server exits) ────────────────────────────────
:: (deps are installed on first run via launch.bat or install.bat — skip here
::  so the silent launcher doesn't add a 15-20s pip-check delay every start)
:: python.exe inherits the hidden console — no new window appears.
:: stdout/stderr go to server.log; important logs also go to logs/dropcat.log.
echo [%TIME%] Starting python app.py >> logs\server.log
python "%~dp0app.py" >> "%~dp0logs\server.log" 2>&1
echo [%TIME%] Server exited >> logs\server.log
