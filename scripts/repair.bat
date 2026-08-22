@echo off
call "%~dp0env.bat"
title Bookreel - repair deps
echo.
echo   [repair] Checks each venv and installs only what is missing.
echo            Safe to run any time - it never rebuilds a venv.
echo.

:audio
if not exist "%VENVS%\audio\Scripts\activate.bat" goto audio_missing
echo   --- venv-audio   TTS :9100 / align :9101 ---
call "%VENVS%\audio\Scripts\activate.bat"
call :ensure python-multipart
call :ensure fastapi
call :ensure uvicorn
call :ensure soundfile
call :ensure funasr
call :ensure modelscope
call :cosyvoice
call deactivate
goto tools
:audio_missing
echo   x venv-audio missing - run menu 1 Install first
echo.

:tools
if not exist "%VENVS%\tools\Scripts\activate.bat" goto tools_missing
echo.
echo   --- venv-tools   embeddings :8002 ---
call "%VENVS%\tools\Scripts\activate.bat"
call :ensure sentence-transformers
call :ensure fastapi
call :ensure uvicorn
call :ensure pillow
call :ensure requests
call :ensure ffmpeg-python
call deactivate
goto llm
:tools_missing
echo.
echo   x venv-tools missing - run menu 1 Install first

:llm
if not exist "%VENVS%\llm\Scripts\activate.bat" goto llm_missing
echo.
echo   --- venv-llm   vLLM :8000 ---
call "%VENVS%\llm\Scripts\activate.bat"
call :ensure vllm
call :ensure huggingface_hub
call deactivate
goto done
:llm_missing
echo.
echo   x venv-llm missing - run menu 1 Install first

:done
echo.
echo   [repair] done. Restart any service that was already running.
echo.
pause
exit /b 0

rem ----------------------------------------------------------
rem  :ensure ^<pip-name^>  - install only if pip does not know it
rem ----------------------------------------------------------
:ensure
python -m pip show %~1 >nul 2>&1
if not errorlevel 1 goto ensure_ok
echo     installing %~1 ...
python -m pip install -q -U %~1
if errorlevel 1 echo     x %~1 FAILED & goto :eof
echo     + %~1 installed
goto :eof
:ensure_ok
echo     ok %~1
goto :eof

rem ----------------------------------------------------------
rem  :cosyvoice - clone the repo if it is not there yet
rem ----------------------------------------------------------
:cosyvoice
if exist "%ROOT%\third_party\CosyVoice\cosyvoice" goto cosy_ok
echo     cloning CosyVoice ^(not on PyPI^) ...
if not exist "%ROOT%\third_party" mkdir "%ROOT%\third_party"
git clone --recursive https://github.com/FunAudioLLM/CosyVoice "%ROOT%\third_party\CosyVoice"
if errorlevel 1 echo     x CosyVoice clone FAILED - check git and network & goto :eof
python -m pip install -q -U -r "%ROOT%\third_party\CosyVoice\requirements.txt"
if errorlevel 1 echo     ! some CosyVoice deps failed - TTS may not start
echo     + CosyVoice cloned
goto :eof
:cosy_ok
echo     ok CosyVoice
goto :eof
