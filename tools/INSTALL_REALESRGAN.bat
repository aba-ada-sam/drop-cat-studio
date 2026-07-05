@echo off
REM Build the dedicated AI-upscale venv for DCS (venv-upscale in the app root).
REM The AI upscaler runs as a subprocess in this venv, so the Python that runs
REM DCS itself never needs the CUDA torch stack.
REM
REM Versions mirror the Forge venv (torch 2.11.0+cu128) -- proven on the
REM RTX 5080. DCS auto-falls-back to Lanczos if this venv is absent, so this
REM install is optional but strongly recommended for low-res model outputs.

setlocal
set "ROOT=%~dp0.."
set "VENV=%ROOT%\venv-upscale"
set "VPY=%VENV%\Scripts\python.exe"

echo Creating venv at %VENV% ...
python -m venv "%VENV%" || goto :fail

echo Upgrading pip ...
"%VPY%" -m pip install -q -U pip wheel setuptools || goto :fail

echo Installing CUDA torch (cu128, ~3 GB download) ...
"%VPY%" -m pip install torch==2.11.0+cu128 torchvision==0.26.0+cu128 --index-url https://download.pytorch.org/whl/cu128 || goto :fail

echo Installing Real-ESRGAN ...
"%VPY%" -m pip install basicsr realesrgan || goto :fail

echo Patching basicsr for modern torchvision ...
"%VPY%" -c "from pathlib import Path; import basicsr, os; p = Path(os.path.dirname(basicsr.__file__)) / 'data' / 'degradations.py'; s = p.read_text(encoding='utf-8'); p.write_text(s.replace('torchvision.transforms.functional_tensor', 'torchvision.transforms.functional'), encoding='utf-8'); print('patched', p)" || goto :fail

echo Verifying ...
"%VPY%" -c "from realesrgan import RealESRGANer; import torch; print('OK torch', torch.__version__, 'cuda:', torch.cuda.is_available())" || goto :fail

echo.
echo Done. DCS will now use Real-ESRGAN (GPU) when AI upscale is requested.
echo Model weights (RealESRGAN_x2plus.pth / x4plus.pth) download automatically
echo on first use; drop copies into %ROOT%\models\ to pin them locally.
echo.
pause
exit /b 0

:fail
echo.
echo INSTALL FAILED -- see errors above.
pause
exit /b 1
