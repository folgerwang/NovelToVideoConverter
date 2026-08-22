@echo off
call "%~dp0env.bat"
title Hailuo video
echo Slot C - video.  %VIDEO_RES%, port 9000
echo 24GB cannot do 1080x1920 x 10s - render at %VIDEO_RES% and upscale later.
if not exist "%HAILUO_DIR%\server.py" (
  echo   x %HAILUO_DIR%\server.py not found.
  echo   Edit this file to match your own Hailuo deployment command.
  pause
  exit /b 1
)
rem venv-llm now only holds the Hugging Face CLI - point this at whatever
rem environment your Hailuo deployment actually needs.
call "%VENVS%\llm\Scripts\activate.bat"
python "%HAILUO_DIR%\server.py" --port 9000 --cors --resolution %VIDEO_RES% --offload --vae-tiling
pause
