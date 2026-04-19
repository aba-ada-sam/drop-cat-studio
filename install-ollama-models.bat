@echo off
setlocal enabledelayedexpansion
title Ollama Model Installer - Drop Cat Go Studio

echo ============================================
echo   Ollama Model Installer
echo   Drop Cat Go Studio
echo ============================================
echo.

REM Check if Ollama is installed
ollama --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Ollama is not installed or not on PATH
    echo.
    echo Install from: https://ollama.ai
    echo Then add Ollama to your PATH and try again.
    echo.
    pause
    exit /b 1
)

echo [OK] Ollama found on PATH
echo.

REM Try to list existing models
echo Current installed models:
ollama list 2>nul
echo.

REM Install required models
echo Installing required models...
echo.

echo [1/3] Installing dolphin3:8b (fast model)...
ollama pull dolphin3:8b
if errorlevel 1 (
    echo ERROR: Failed to install dolphin3:8b
    echo Check your internet connection and try again.
    pause
    exit /b 1
)
echo [OK] dolphin3:8b installed
echo.

echo [2/3] Installing impish-bloodmoon:12b (balanced model)...
ollama pull impish-bloodmoon:12b
if errorlevel 1 (
    echo ERROR: Failed to install impish-bloodmoon:12b
    echo Check your internet connection and try again.
    pause
    exit /b 1
)
echo [OK] impish-bloodmoon:12b installed
echo.

echo [3/3] Installing heretic-gemma4:31b (power model)...
ollama pull heretic-gemma4:31b
if errorlevel 1 (
    echo ERROR: Failed to install heretic-gemma4:31b
    echo Check your internet connection and try again.
    pause
    exit /b 1
)
echo [OK] heretic-gemma4:31b installed
echo.

REM Verify all models are installed
echo ============================================
echo   Final model list:
echo ============================================
ollama list
echo.

echo [OK] All models installed successfully!
echo.
echo You can now run Drop Cat Go Studio with: launch.bat
echo.
pause
exit /b 0
