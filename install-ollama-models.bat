@echo off
setlocal enabledelayedexpansion
title Ollama Model Installer - Drop Cat Go Studio

echo ============================================
echo   Ollama Model Installer
echo   Drop Cat Go Studio
echo   Models tuned for RTX 4070 (12GB VRAM)
echo ============================================
echo.

ollama --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Ollama is not installed or not on PATH
    echo Install from: https://ollama.ai
    pause
    exit /b 1
)

echo [OK] Ollama found on PATH
echo.
echo Current installed models:
ollama list
echo.

echo Installing required models...
echo.

echo [1/2] Installing gemma3:4b (fast + vision model, ~3 GB)...
ollama pull gemma3:4b
if errorlevel 1 (
    echo ERROR: Failed to install gemma3:4b
    pause
    exit /b 1
)
echo [OK] gemma3:4b installed
echo.

echo [2/2] Installing qwen2.5:14b (power model, ~8.7 GB)...
ollama pull qwen2.5:14b
if errorlevel 1 (
    echo ERROR: Failed to install qwen2.5:14b
    pause
    exit /b 1
)
echo [OK] qwen2.5:14b installed
echo.

echo ============================================
echo   Final model list:
echo ============================================
ollama list
echo.
echo [OK] All models installed! Run launch.bat to start the app.
echo.
pause
exit /b 0
