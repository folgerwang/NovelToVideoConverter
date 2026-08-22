@echo off
call "%~dp0env.bat"
title ComfyUI Flux.2
echo Slot B - images.  Flux.2 fp8, port 7860
call "%VENVS%\comfy\Scripts\activate.bat"
python "%COMFY%\main.py" --port 7860 --enable-cors-header "*" --fp8_e4m3fn-unet --lowvram
pause
