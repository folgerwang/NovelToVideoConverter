@echo off
rem Download / update the prebuilt llama.cpp CUDA binaries.
rem Idempotent: it stamps the build tag and skips when already current.
call "%~dp0env.bat"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0get-llama.ps1" -Dest "%LLAMA%" -Cuda "%LLAMA_CUDA%"
exit /b %errorlevel%
