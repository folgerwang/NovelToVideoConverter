@echo off
call "%~dp0env.bat"
echo Starting CPU-only services (no VRAM): embeddings 8002, TTS 9100, ASR 9101
if exist "%ROOT%\services\embed_server.py" (
  start "Embed CPU" cmd /k ""%VENVS%\tools\Scripts\activate.bat" && python "%ROOT%\services\embed_server.py" --port 8002"
) else (echo   ! services\embed_server.py missing - skipped)
if exist "%ROOT%\services\tts_server.py" (
  start "CosyVoice2 TTS" cmd /k ""%VENVS%\audio\Scripts\activate.bat" && python "%ROOT%\services\tts_server.py" --model "%MODELS%\cosyvoice2" --port 9100 --device cpu"
) else (echo   ! services\tts_server.py missing - skipped)
if exist "%ROOT%\services\asr_server.py" (
  start "FunASR align" cmd /k ""%VENVS%\audio\Scripts\activate.bat" && python "%ROOT%\services\asr_server.py" --port 9101 --device cpu"
) else (echo   ! services\asr_server.py missing - skipped)
exit /b 0
