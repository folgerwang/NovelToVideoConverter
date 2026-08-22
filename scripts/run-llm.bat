@echo off
call "%~dp0env.bat"
title llama.cpp Qwen3.8-27B
set "GGUF=%MODELS%\qwen3.8-27b-gguf\%F_MAIN_GGUF%"
if not exist "%LLAMA%\llama-server.exe" call "%~dp0get-llama.bat"
if not exist "%LLAMA%\llama-server.exe" goto no_server
if not exist "%GGUF%" goto no_model
echo Slot A - text.  %F_MAIN_GGUF%, ctx %MAIN_CTX%, port 8000
echo 262K native context does not fit in 24GB of KV cache - feed by chapter instead.
"%LLAMA%\llama-server.exe" -m "%GGUF%" --host 127.0.0.1 --port 8000 --alias qwen3.8-27b -ngl 99 -c %MAIN_CTX% --parallel 1 --jinja --no-warmup %KV_FLAGS%
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
