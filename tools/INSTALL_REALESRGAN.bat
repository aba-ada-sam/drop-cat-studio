@echo off
REM Install Real-ESRGAN dependencies for DCS AI upscaling.
REM Run this once on the 5080 machine. Requires Python on PATH (use the same
REM Python that runs DCS -- e.g. the WanGP venv or the system Python).
REM
REM DCS auto-falls-back to Lanczos if these packages are absent, so this
REM install is optional but strongly recommended for low-res model outputs.

echo Installing Real-ESRGAN dependencies...

pip install basicsr realesrgan

REM basicsr requires older versions of some packages -- suppress the noise.
REM The install is successful if "Successfully installed" lines appear above.

echo.
echo Done. DCS will now use Real-ESRGAN when upscale_method=ai is requested.
echo Model weights (RealESRGAN_x2plus.pth and x4plus.pth) are downloaded
echo automatically on first use to %%USERPROFILE%%\.realesrgan\weights\.
echo.
pause
