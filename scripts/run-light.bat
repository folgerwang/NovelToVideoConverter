@echo off
call "%~dp0env.bat"
echo.
echo   CPU-only services (no VRAM): embeddings 8002, TTS 9100, align 9101
echo   Anything already listening is left alone - close that window first
echo   if you want to restart it.
echo.

:embed
call :busy 8002
if not errorlevel 1 goto embed_skip
if not exist "%ROOT%\services\embed_server.py" goto embed_missing
start "Embed CPU" cmd /k ""%VENVS%\tools\Scripts\activate.bat" && python "%ROOT%\services\embed_server.py" --port 8002"
echo   +  embeddings   starting on 8002
goto tts
:embed_skip
echo   .  embeddings   already listening on 8002 - left alone
goto tts
:embed_missing
echo   !  services\embed_server.py missing - skipped
goto tts

:tts
call :busy 9100
if not errorlevel 1 goto tts_skip
if not exist "%ROOT%\services\tts_server.py" goto tts_missing
start "CosyVoice2 TTS" cmd /k ""%VENVS%\audio\Scripts\activate.bat" && python "%ROOT%\services\tts_server.py" --model "%MODELS%\cosyvoice2" --port 9100 --device cpu"
echo   +  TTS          starting on 9100
goto asr
:tts_skip
echo   .  TTS          already listening on 9100 - left alone
goto asr
:tts_missing
echo   !  services\tts_server.py missing - skipped
goto asr

:asr
call :busy 9101
if not errorlevel 1 goto asr_skip
if not exist "%ROOT%\services\asr_server.py" goto asr_missing
start "FunASR align" cmd /k ""%VENVS%\audio\Scripts\activate.bat" && python "%ROOT%\services\asr_server.py" --port 9101 --device cpu"
echo   +  align        starting on 9101
goto done
:asr_skip
echo   .  align        already listening on 9101 - left alone
goto done
:asr_missing
echo   !  services\asr_server.py missing - skipped
goto done

:done
echo.
exit /b 0

rem ----------------------------------------------------------
rem  :busy ^<port^>  - errorlevel 0 when something is LISTENING
rem  on that port, 1 when the port is free.
rem ----------------------------------------------------------
:busy
netstat -ano | findstr /c:"127.0.0.1:%~1" | findstr /c:"LISTENING" >nul 2>&1
goto :eof
