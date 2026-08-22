@echo off
call "%~dp0env.bat"
title llama.cpp Qwen3-8B
set "GGUF=%MODELS%\qwen3-8b-gguf\%F_SMALL_GGUF%"
if not exist "%LLAMA%\llama-server.exe" call "%~dp0get-llama.bat"
if not exist "%LLAMA%\llama-server.exe" goto no_server
if not exist "%GGUF%" goto no_model
echo Fallback slot - Qwen3-8B on port 8000 (faster, weaker).
echo Served under the same alias so the web console needs no change.
"%LLAMA%\llama-server.exe" -m "%GGUF%" --host 127.0.0.1 --port 8000 --alias qwen3.8-27b -ngl 99 -c %SMALL_CTX% --parallel 2 --jinja --no-warmup %KV_FLAGS%
pause
exit /b 0
:no_server
echo   x %LLAMA%\llama-server.exe not found. Run menu 1 (Install).
pause
exit /b 1
:no_model
echo   x %GGUF% not found. Run menu 2 (Download model weights).
pause
exit /b 1
