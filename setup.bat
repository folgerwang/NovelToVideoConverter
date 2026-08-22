@echo off
rem ==========================================================
rem  Bookreel - novel to video, local deployment
rem  Single RTX 4090 24GB: the heavy models cannot coexist,
rem  so slots A / B / C take turns on the card.
rem
rem    Slot A  text    Qwen3.8-27B AWQ 4bit   ~20GB   :8000
rem    Slot B  images  Flux.2 fp8 (ComfyUI)   ~18GB   :7860
rem    Slot C  video   Hailuo 720x1280        ~22GB   :9000
rem    Always on (CPU)  embeddings / TTS / ASR   0GB
rem
rem  Double click from Explorer, or from PowerShell:  .\setup.bat
rem  ASCII only + CRLF on purpose - cmd mis-parses UTF-8 batch files.
rem ==========================================================
call "%~dp0scripts\env.bat"
title Bookreel - local deployment

:menu
cls
echo.
echo   Bookreel - novel to video   [single RTX 4090 24GB]
for /f "tokens=1,2 delims=," %%a in ('nvidia-smi --query-gpu^=memory.used^,memory.total --format^=csv^,noheader^,nounits 2^>nul') do echo   VRAM %%a MiB used of %%b MiB
echo   --------------------------------------------------------
echo    1   Install                 venvs + ComfyUI
echo    L   Log in to Hugging Face  needed for gated repos
echo    2   Download model weights
echo    3   Start CPU services      embeddings / TTS / ASR
echo.
echo    A   Slot A - text     Qwen3.8-27B   script, shots, prompts
echo    B   Slot B - images   Flux.2        character and scene plates
echo    C   Slot C - video    Hailuo        10 second clips
echo    S   Slot A - small    Qwen3-8B      faster fallback
echo.
echo    H   Health check            F   Free the GPU
echo    W   Open web console        Q   Quit
echo   --------------------------------------------------------
echo.
set "CH="
set /p CH=  Choose: 
if /I "%CH%"=="1" call "%~dp0scripts\install.bat" & goto menu
if /I "%CH%"=="L" call "%~dp0scripts\hf-login.bat" & goto menu
if /I "%CH%"=="2" call "%~dp0scripts\download.bat" & goto menu
if /I "%CH%"=="3" goto services
if /I "%CH%"=="A" goto slot_a
if /I "%CH%"=="B" goto slot_b
if /I "%CH%"=="C" goto slot_c
if /I "%CH%"=="S" goto slot_s
if /I "%CH%"=="H" call "%~dp0scripts\health.bat" & goto menu
if /I "%CH%"=="F" call "%~dp0scripts\stop.bat" & echo   GPU freed. & pause & goto menu
if /I "%CH%"=="W" start "" "%ROOT%\Pipeline Runner.dc.html" & goto menu
if /I "%CH%"=="Q" exit /b 0
goto menu

:slot_a
call "%~dp0scripts\stop.bat"
start "vLLM Qwen3.8-27B" cmd /k "%~dp0scripts\run-llm.bat"
echo   Slot A starting. First load takes 3-6 minutes.
pause
goto menu

:slot_s
call "%~dp0scripts\stop.bat"
start "vLLM Qwen3-8B" cmd /k "%~dp0scripts\run-llm-small.bat"
pause
goto menu

:slot_b
call "%~dp0scripts\stop.bat"
start "ComfyUI Flux.2" cmd /k "%~dp0scripts\run-comfy.bat"
echo   Slot B starting. 1080x1920 at 28 steps is roughly 40-70s per image.
pause
goto menu

:slot_c
call "%~dp0scripts\stop.bat"
start "Hailuo video" cmd /k "%~dp0scripts\run-hailuo.bat"
pause
goto menu

:services
call :ensure_audio
call :ensure_tools
call "%~dp0scripts\run-light.bat"
pause
goto menu

rem ==========================================================
rem  Dependency guards. A venv built by an older install.bat
rem  will not have packages added later, so check before use
rem  and install only what is actually missing. Cheap when
rem  everything is already there - one python import test.
rem ==========================================================

:ensure_audio
if not exist "%VENVS%\audio\Scripts\activate.bat" goto :eof
call "%VENVS%\audio\Scripts\activate.bat"
python -c "import fastapi, uvicorn, soundfile, funasr" >nul 2>&1
if errorlevel 1 goto fix_audio
python -c "import python_multipart" >nul 2>&1
if not errorlevel 1 goto cosy_check
python -c "import multipart" >nul 2>&1
if not errorlevel 1 goto cosy_check
:fix_audio
echo   [deps] venv-audio is missing packages - installing ...
python -m pip install -q -U fastapi uvicorn soundfile python-multipart funasr modelscope
if errorlevel 1 echo   ! some venv-audio packages failed to install
:cosy_check
if exist "%ROOT%\third_party\CosyVoice\cosyvoice" goto audio_done
echo   [deps] CosyVoice is not cloned yet ^(it is not on PyPI^) - fetching ...
if not exist "%ROOT%\third_party" mkdir "%ROOT%\third_party"
git clone --recursive https://github.com/FunAudioLLM/CosyVoice "%ROOT%\third_party\CosyVoice"
if errorlevel 1 goto cosy_failed
python -m pip install -q -U -r "%ROOT%\third_party\CosyVoice\requirements.txt"
if errorlevel 1 echo   ! some CosyVoice deps failed - TTS may not start
goto audio_done
:cosy_failed
echo   ! CosyVoice clone failed - check git and network. TTS will stay offline.
:audio_done
call deactivate
goto :eof

:ensure_tools
if not exist "%VENVS%\tools\Scripts\activate.bat" goto :eof
call "%VENVS%\tools\Scripts\activate.bat"
python -c "import fastapi, uvicorn, sentence_transformers" >nul 2>&1
if not errorlevel 1 goto tools_done
echo   [deps] venv-tools is missing packages - installing ...
python -m pip install -q -U fastapi uvicorn sentence-transformers pillow requests ffmpeg-python
if errorlevel 1 echo   ! some venv-tools packages failed to install
:tools_done
call deactivate
goto :eof
