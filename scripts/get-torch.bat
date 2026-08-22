@echo off
rem Install / repair a CUDA-enabled torch in a venv.
rem   get-torch.bat "%VENVS%\comfy"
rem Verifies torch.cuda.is_available() and is a no-op when already working.
call "%~dp0env.bat"
if "%~1"=="" (
  echo   usage: get-torch.bat ^<path-to-venv^>
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0get-torch.ps1" -Venv "%~1" %TORCH_CUDA_ARG%
exit /b %errorlevel%
