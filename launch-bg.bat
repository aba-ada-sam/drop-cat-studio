@echo off
:: Drop Cat Go Studio — Background launcher
:: Invoked by launch-silent.vbs inside a hidden cmd window (style=0).
:: Uses regular python.exe — the hidden console means no visible window.
:: Do NOT run this directly; use launch.bat for debugging.

cd /d "%~dp0"
if not exist logs mkdir logs

echo [%TIME%] launch-bg.bat started >> logs\server.log

:: ── Kill stale process on port 7860 ──────────────────────────────────────────
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":7860 " ^| findstr "LISTENING" 2^>nul') do (
    echo [%TIME%] Killing stale PID %%p >> logs\server.log
    taskkill /PID %%p /F >nul 2>&1
)

:: ── Install/update deps silently ─────────────────────────────────────────────
pip install -q -r requirements.txt >> logs\server.log 2>&1


:: ── Run the server (blocks until server exits) ────────────────────────────────
:: python.exe inherits the hidden console — no new window appears.
:: stdout/stderr go to server.log; important logs also go to logs/dropcat.log.
echo [%TIME%] Starting python app.py >> logs\server.log
python "%~dp0app.py" >> "%~dp0logs\server.log" 2>&1
echo [%TIME%] Server exited >> logs\server.log
