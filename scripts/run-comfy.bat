@echo off
call "%~dp0env.bat"
title ComfyUI Flux.2
call "%VENVS%\comfy\Scripts\activate.bat"
rem On Windows the PyPI torch wheel is CPU-only, so a venv built with a plain
rem "pip install torch" starts and then dies on the first CUDA call. Check
rem here and repair rather than failing halfway into startup.
python -c "import torch,sys; sys.exit(0 if torch.cuda.is_available() else 1)" >nul 2>&1
if not errorlevel 1 goto run
echo   [torch] this venv has a CPU-only torch - installing the CUDA build ...
call deactivate
call "%~dp0get-torch.bat" "%VENVS%\comfy"
if errorlevel 1 goto no_cuda
call "%VENVS%\comfy\Scripts\activate.bat"
:run
echo Slot B - images.  Flux.2 fp8, port 7860
python "%COMFY%\main.py" --port 7860 --enable-cors-header "*" --fp8_e4m3fn-unet --lowvram
pause
exit /b 0
:no_cuda
echo.
echo   x Slot B cannot start without a CUDA-enabled torch.
pause
exit /b 1
