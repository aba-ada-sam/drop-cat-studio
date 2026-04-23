@echo off
:: Drop Cat Go Studio — Electron launcher
:: Starts the Python server in the background, then opens the Electron window.

setlocal
cd /d "%~dp0"

:: Start the server if not already running
start /b "" pythonw manager.pyw --no-browser 2>nul
if errorlevel 1 (
  start /b "" python app.py 2>nul
)

:: Small pause to let the server begin writing .dcs-port
timeout /t 1 /nobreak >nul

:: Launch Electron (it will poll .dcs-port and the server itself)
npx electron .
