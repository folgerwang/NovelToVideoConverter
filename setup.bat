@echo off
rem ==========================================================
rem  Bookreel - novel to video, local deployment
rem  Single RTX 4090 24GB: the heavy models cannot coexist,
rem  so slots A / B / C take turns on the card.
rem
rem    Slot A  text    Qwen3.8-27B Q4_K_M     ~16GB   :8000  (llama.cpp)
rem    Slot B  images  Flux.2 fp8 (ComfyUI)   ~18GB   :7860
rem    Slot C  video   Hailuo 720x1280        ~22GB   :9000
rem    Always on (CPU)  embeddings / TTS / align   0GB
rem
rem  Double click from Explorer, or from PowerShell:  .\setup.bat
rem  ASCII only + CRLF on purpose - cmd mis-parses UTF-8 batch files.
rem
rem  Everything lives in this one file. It re-invokes itself with a
rem  command word to open a slot in its own window, e.g.
rem      setup.bat llm       setup.bat comfy      setup.bat health
rem  Only two helpers stay outside, because they parse JSON and unzip:
rem      scripts\get-llama.ps1   scripts\get-torch.ps1
rem ==========================================================

rem Resolve these at top level: %~dp0 and %~f0 are not dependable once
rem you are inside a "call :label" - %0 becomes the label name there.
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "SELF=%~f0"
call :config
if not "%~1"=="" goto dispatch
goto menu

rem ==========================================================
rem  Configuration - edit here
rem ==========================================================
:config
set "SCRIPTS=%ROOT%\scripts"
set "MODELS=%ROOT%\models"
set "VENVS=%ROOT%\venvs"
set "LOGS=%ROOT%\logs"
set "COMFY=%ROOT%\ComfyUI"
set "HAILUO_DIR=%ROOT%\hailuo"
set "LLAMA=%ROOT%\llama"
set "SERVICES=%ROOT%\services"
set "HF_HOME=%MODELS%\hf"

rem --- slot A runtime -------------------------------------------------
rem vLLM is Linux-only: PyPI publishes manylinux wheels and nothing else,
rem so pip falls back to the sdist, the CUDA extensions never build, and
rem it dies with "No module named 'vllm._C_stable_libtorch'". llama.cpp
rem ships prebuilt Windows CUDA binaries and serves the same
rem OpenAI-compatible API, so the web console does not change.
rem 12.4 and 13.3 are the CUDA builds currently published.
set "LLAMA_CUDA=12.4"

rem --- model repos ----------------------------------------------------
set "M_MAIN_GGUF=unsloth/Qwen3.8-27B-GGUF"
set "F_MAIN_GGUF=Qwen3.8-27B-UD-Q4_K_M.gguf"
set "M_SMALL_GGUF=unsloth/Qwen3-8B-GGUF"
set "F_SMALL_GGUF=Qwen3-8B-Q4_K_M.gguf"
set "M_EMBED=Qwen/Qwen3-Embedding-0.6B"
set "M_FLUX=black-forest-labs/FLUX.2-dev"
set "M_TTS=FunAudioLLM/CosyVoice2-0.5B"

rem --- 24GB tunables: longer context = more KV cache ------------------
rem Q4_K_M weights are 16.4GB, leaving about 7GB. At 63 layers that is
rem roughly 260KB of KV per token, so 16K context costs about 4GB and
rem fits. To go higher, set KV_FLAGS below.
set "MAIN_CTX=16384"
set "SMALL_CTX=32768"

rem Halves KV cache memory. Set to: --cache-type-k q8_0 --cache-type-v q8_0
set "KV_FLAGS="

rem Leave empty to auto-probe cu130 / cu129 / cu128 / cu126 against the
rem installed driver. Pin one with: -Cuda cu128
set "TORCH_CUDA_ARG="

set "VIDEO_RES=720x1280"
exit /b 0

rem ==========================================================
rem  Command-word dispatch (used by the windows we spawn)
rem ==========================================================
:dispatch
if /I "%~1"=="install"  goto do_install
if /I "%~1"=="hflogin"  goto do_hflogin
if /I "%~1"=="download" goto do_download
if /I "%~1"=="services" goto do_services
if /I "%~1"=="llm"      goto do_llm
if /I "%~1"=="small"    goto do_small
if /I "%~1"=="comfy"    goto do_comfy
if /I "%~1"=="hailuo"   goto do_hailuo
if /I "%~1"=="embed"    goto do_embed
if /I "%~1"=="tts"      goto do_tts
if /I "%~1"=="asr"      goto do_asr
if /I "%~1"=="health"   goto do_health
if /I "%~1"=="stop"     goto do_stop
if /I "%~1"=="getllama" goto do_getllama
if /I "%~1"=="gettorch" goto do_gettorch_cmd
echo   unknown command: %~1
echo   try: install hflogin download services llm small comfy hailuo
echo        embed tts asr health stop getllama gettorch
exit /b 1

rem ==========================================================
rem  Menu
rem ==========================================================
:menu
cls
echo.
echo   Bookreel - novel to video   [single RTX 4090 24GB]
for /f "tokens=1,2 delims=," %%a in ('nvidia-smi --query-gpu^=memory.used^,memory.total --format^=csv^,noheader^,nounits 2^>nul') do echo   VRAM %%a MiB used of %%b MiB
echo   --------------------------------------------------------
echo    1   Install                 venvs + llama.cpp + ComfyUI
echo    L   Log in to Hugging Face  needed for gated repos
echo    2   Download model weights
echo    3   Start CPU services      embeddings / TTS / align
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
if /I "%CH%"=="1" call :do_install & goto menu
if /I "%CH%"=="L" call :do_hflogin & goto menu
if /I "%CH%"=="2" call :do_download & goto menu
if /I "%CH%"=="3" call :do_services & pause & goto menu
if /I "%CH%"=="A" goto slot_a
if /I "%CH%"=="B" goto slot_b
if /I "%CH%"=="C" goto slot_c
if /I "%CH%"=="S" goto slot_s
if /I "%CH%"=="H" call :do_health & goto menu
if /I "%CH%"=="F" call :do_stop & echo   GPU freed. & pause & goto menu
if /I "%CH%"=="W" start "" "%ROOT%\Pipeline Runner.dc.html" & goto menu
if /I "%CH%"=="Q" exit /b 0
goto menu

:slot_a
call :do_stop
start "llama.cpp Qwen3.8-27B" cmd /k ""%SELF%" llm"
echo   Slot A starting. First load reads 16GB off disk - give it a minute.
pause
goto menu

:slot_s
call :do_stop
start "llama.cpp Qwen3-8B" cmd /k ""%SELF%" small"
pause
goto menu

:slot_b
call :do_stop
start "ComfyUI Flux.2" cmd /k ""%SELF%" comfy"
echo   Slot B starting. 1080x1920 at 28 steps is roughly 40-70s per image.
pause
goto menu

:slot_c
call :do_stop
start "Hailuo video" cmd /k ""%SELF%" hailuo"
pause
goto menu

rem ==========================================================
rem  1 - Install
rem ==========================================================
:do_install
title Bookreel - install
echo.
echo   [check] python / git / gpu
where python >nul 2>&1 || (echo   x python not found & pause & exit /b 1)
where git    >nul 2>&1 || (echo   x git not found & pause & exit /b 1)
python -c "import sys;print('   python',sys.version.split()[0])"
python -c "import sys;sys.exit(0 if sys.version_info[:2]<=(3,12) else 1)"
if errorlevel 1 (
  echo   ! Python 3.13+ detected. Torch and ComfyUI wheels lag a release or two
  echo     behind; 3.11 or 3.12 is the safe choice. Continuing anyway - if a
  echo     package fails to build, install 3.12 and delete the venvs folder.
  echo.
)
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
if errorlevel 1 (echo   x no NVIDIA GPU detected & pause & exit /b 1)
if not exist "%MODELS%" mkdir "%MODELS%"
if not exist "%LOGS%"   mkdir "%LOGS%"
if not exist "%VENVS%"  mkdir "%VENVS%"

echo.
echo   [1/4] llama.cpp CUDA binaries + Hugging Face CLI
if not exist "%VENVS%\llm" python -m venv "%VENVS%\llm"
call "%VENVS%\llm\Scripts\activate.bat"
python -m pip install -U pip
python -m pip install -U "huggingface_hub[cli]"
call deactivate
call :do_getllama
if errorlevel 1 echo   ! llama.cpp download failed - slot A will not start

echo.
echo   [2/4] venv-audio  (CosyVoice2 / FunASR - CPU is fine)
if not exist "%VENVS%\audio" python -m venv "%VENVS%\audio"
call "%VENVS%\audio\Scripts\activate.bat"
python -m pip install -U pip
rem The CPU build is all these services need - TTS and ASR both run with
rem --device cpu so the GPU stays free for slots A / B / C. On Windows the
rem plain PyPI wheel IS the CPU build, which is what we want here.
python -m pip install -U torch torchaudio
python -m pip install -U funasr modelscope fastapi uvicorn soundfile python-multipart
call :cosyvoice
call deactivate

echo.
echo   [3/4] venv-tools  (ffmpeg helpers / CPU embeddings)
if not exist "%VENVS%\tools" python -m venv "%VENVS%\tools"
call "%VENVS%\tools\Scripts\activate.bat"
python -m pip install -U pip
python -m pip install -U ffmpeg-python pillow requests sentence-transformers fastapi uvicorn
call deactivate

echo.
echo   [4/4] ComfyUI
if not exist "%COMFY%" git clone https://github.com/comfyanonymous/ComfyUI "%COMFY%"
if not exist "%VENVS%\comfy" python -m venv "%VENVS%\comfy"
call "%VENVS%\comfy\Scripts\activate.bat"
python -m pip install -U pip
python -m pip install -r "%COMFY%\requirements.txt"
call deactivate
call :do_gettorch "%VENVS%\comfy"
if errorlevel 1 echo   ! slot B will not start until torch has CUDA

echo.
echo   Install done. Next: menu L (Hugging Face login), then 2 (download).
pause
exit /b 0

rem ==========================================================
rem  L - Hugging Face login
rem ==========================================================
:do_hflogin
title Bookreel - Hugging Face login
call "%VENVS%\llm\Scripts\activate.bat"
echo.
echo   Hugging Face access
echo   --------------------------------------------------------
hf auth whoami 2>nul
if not errorlevel 1 goto hf_gates
echo   Not logged in yet.
echo.
echo   1. Create a READ token:  https://huggingface.co/settings/tokens
echo   2. Paste it below.
echo.
set "HFTOK="
set /p HFTOK=  Token:
if "%HFTOK%"=="" goto hf_notoken
hf auth login --token %HFTOK% --add-to-git-credential
set "HFTOK="
if errorlevel 1 goto hf_failed
:hf_gates
echo.
echo   Gated repos - open each page ONCE in a browser and click Accept:
echo     https://huggingface.co/black-forest-labs/FLUX.2-dev
echo.
echo   Approval is usually instant. Without it the download returns
echo   "Access denied. This repository requires approval."
echo.
set "OPEN="
set /p OPEN=  Open the FLUX.2 page now? [y/N]:
if /I "%OPEN%"=="y" start "" https://huggingface.co/black-forest-labs/FLUX.2-dev
call deactivate
pause
exit /b 0
:hf_notoken
echo   No token entered.
call deactivate
pause
exit /b 1
:hf_failed
echo   x login failed
call deactivate
pause
exit /b 1

rem ==========================================================
rem  2 - Download weights
rem ==========================================================
:do_download
title Bookreel - download models
call "%VENVS%\llm\Scripts\activate.bat"
echo.
echo   Cache dir: %HF_HOME%
echo   NOTE 24GB cards can only run 4-bit weights. Do NOT pull BF16 or FP8.
echo.
hf auth whoami >nul 2>&1
if errorlevel 1 (
  echo   ! Not logged in to Hugging Face - gated repos will fail.
  echo     Run menu option L first, then come back.
  echo.
  pause
)

echo   [1/5] %M_MAIN_GGUF% / %F_MAIN_GGUF%   ~16GB
hf download %M_MAIN_GGUF% --include "%F_MAIN_GGUF%" --local-dir "%MODELS%\qwen3.8-27b-gguf"
if errorlevel 1 (
  echo   x download failed. Other quants in the same repo:
  echo       Qwen3.8-27B-UD-Q4_K_S.gguf   15.4GB  more KV cache headroom
  echo       Qwen3.8-27B-UD-Q5_K_M.gguf   19.8GB  better quality, tight on 24GB
  echo     Change F_MAIN_GGUF in the :config section of setup.bat to switch.
)

echo   [2/5] %M_SMALL_GGUF% / %F_SMALL_GGUF%   ~5GB
hf download %M_SMALL_GGUF% --include "%F_SMALL_GGUF%" --local-dir "%MODELS%\qwen3-8b-gguf"
if errorlevel 1 echo   ! skipped

echo   [3/5] %M_EMBED%
hf download %M_EMBED% --local-dir "%MODELS%\qwen3-embedding"
if errorlevel 1 echo   ! skipped

echo   [4/5] %M_FLUX%   ~24GB   (GATED)
hf download %M_FLUX% --local-dir "%MODELS%\flux2"
if errorlevel 1 (
  echo   x Access denied - this repo requires approval.
  echo     Open https://huggingface.co/black-forest-labs/FLUX.2-dev and click Accept,
  echo     then run this step again. Alternative ungated checkpoints:
  echo       Comfy-Org/flux2-dev-ComfyUI       repackaged, drops into ComfyUI
  echo       Kijai/flux2-fp8                   fp8 single file, smallest download
)

echo   [5/5] %M_TTS%
hf download %M_TTS% --local-dir "%MODELS%\cosyvoice2"
if errorlevel 1 echo   ! skipped
call deactivate
echo.
echo   FunASR pulls its weights on first call - nothing to predownload.
echo   Put your Hailuo weights in: %HAILUO_DIR%
echo   Copy FLUX.2 files into %COMFY%\models\{diffusion_models,text_encoders,vae}
echo   On a 4090 use the fp8_e4m3fn unet - fp16 will OOM.
pause
exit /b 0

rem ==========================================================
rem  3 - CPU services
rem ==========================================================
:do_services
call :ensure_audio
call :ensure_tools
echo.
echo   CPU-only services (no VRAM): embeddings 8002, TTS 9100, align 9101
echo   Anything already listening is left alone - close its window first
echo   if you want to restart it.
echo.
call :svc 8002 "Embed CPU"      embed  embed_server.py
call :svc 9100 "CosyVoice2 TTS" tts    tts_server.py
call :svc 9101 "FunASR align"   asr    asr_server.py
echo.
exit /b 0

rem  :svc <port> "<window title>" <command word> <script filename>
:svc
if not exist "%SERVICES%\%~4" goto svc_missing
call :busy %~1
if not errorlevel 1 goto svc_running
start %2 cmd /k ""%SELF%" %~3"
echo   +  %~3   starting on %~1
goto :eof
:svc_running
echo   .  %~3   already listening on %~1 - left alone
goto :eof
:svc_missing
echo   !  services\%~4 missing - skipped
goto :eof

:do_embed
title Embed CPU
call "%VENVS%\tools\Scripts\activate.bat"
python "%SERVICES%\embed_server.py" --port 8002
exit /b 0

:do_tts
title CosyVoice2 TTS
call "%VENVS%\audio\Scripts\activate.bat"
python "%SERVICES%\tts_server.py" --model "%MODELS%\cosyvoice2" --port 9100 --device cpu
exit /b 0

:do_asr
title FunASR align
call "%VENVS%\audio\Scripts\activate.bat"
python "%SERVICES%\asr_server.py" --port 9101 --device cpu
exit /b 0

rem ==========================================================
rem  A / S - text slots (llama.cpp)
rem ==========================================================
:do_llm
title llama.cpp Qwen3.8-27B
set "GGUF=%MODELS%\qwen3.8-27b-gguf\%F_MAIN_GGUF%"
if not exist "%LLAMA%\llama-server.exe" call :do_getllama
if not exist "%LLAMA%\llama-server.exe" goto llm_noserver
if not exist "%GGUF%" goto llm_nomodel
echo Slot A - text.  %F_MAIN_GGUF%, ctx %MAIN_CTX%, port 8000
echo 262K native context does not fit in 24GB of KV cache - feed by chapter.
"%LLAMA%\llama-server.exe" -m "%GGUF%" --host 127.0.0.1 --port 8000 --alias qwen3.8-27b -ngl 99 -c %MAIN_CTX% --parallel 1 --jinja --no-warmup %KV_FLAGS%
pause
exit /b 0

:do_small
title llama.cpp Qwen3-8B
set "GGUF=%MODELS%\qwen3-8b-gguf\%F_SMALL_GGUF%"
if not exist "%LLAMA%\llama-server.exe" call :do_getllama
if not exist "%LLAMA%\llama-server.exe" goto llm_noserver
if not exist "%GGUF%" goto llm_nomodel
echo Fallback slot - Qwen3-8B on port 8000 (faster, weaker).
echo Same alias as the 27B so the web console needs no change.
"%LLAMA%\llama-server.exe" -m "%GGUF%" --host 127.0.0.1 --port 8000 --alias qwen3.8-27b -ngl 99 -c %SMALL_CTX% --parallel 2 --jinja --no-warmup %KV_FLAGS%
pause
exit /b 0

:llm_noserver
echo   x %LLAMA%\llama-server.exe not found. Run menu 1 (Install).
pause
exit /b 1
:llm_nomodel
echo   x %GGUF% not found. Run menu 2 (Download model weights).
pause
exit /b 1

rem ==========================================================
rem  B - images (ComfyUI)
rem ==========================================================
:do_comfy
title ComfyUI Flux.2
call "%VENVS%\comfy\Scripts\activate.bat"
rem Two ways this venv can be wrong, both fatal at startup:
rem   - the PyPI torch wheel is CPU-only on Windows, so the first CUDA call
rem     raises "Torch not compiled with CUDA enabled"
rem   - torchaudio can be missing, and ComfyUI imports it from
rem     comfy\ldm\lightricks\vae\audio_vae.py
rem Check both here; the import fails the test if either is wrong.
python -c "import torch, torchaudio, sys; sys.exit(0 if torch.cuda.is_available() else 1)" >nul 2>&1
if not errorlevel 1 goto comfy_run
echo   [torch] this venv needs a CUDA torch + torchaudio - fixing ...
call deactivate
call :do_gettorch "%VENVS%\comfy"
if errorlevel 1 goto comfy_nocuda
call "%VENVS%\comfy\Scripts\activate.bat"
:comfy_run
echo Slot B - images.  Flux.2 fp8, port 7860
python "%COMFY%\main.py" --port 7860 --enable-cors-header "*" --fp8_e4m3fn-unet --lowvram
pause
exit /b 0
:comfy_nocuda
echo.
echo   x Slot B cannot start without a CUDA-enabled torch.
pause
exit /b 1

rem ==========================================================
rem  C - video (Hailuo) - bring your own deployment
rem ==========================================================
:do_hailuo
title Hailuo video
echo Slot C - video.  %VIDEO_RES%, port 9000
echo 24GB cannot do 1080x1920 x 10s - render at %VIDEO_RES% and upscale later.
if not exist "%HAILUO_DIR%\server.py" goto hailuo_missing
rem venv-llm only holds the Hugging Face CLI now - point this at whatever
rem environment your Hailuo deployment actually needs.
call "%VENVS%\llm\Scripts\activate.bat"
python "%HAILUO_DIR%\server.py" --port 9000 --cors --resolution %VIDEO_RES% --offload --vae-tiling
pause
exit /b 0
:hailuo_missing
echo   x %HAILUO_DIR%\server.py not found.
echo   Edit the :do_hailuo section of setup.bat to match your own deployment.
pause
exit /b 1

rem ==========================================================
rem  H - health   F - stop
rem ==========================================================
:do_health
echo.
echo   [health]
curl -s -o nul -w "  :8000 main LLM   HTTP %%{http_code}\n" http://127.0.0.1:8000/v1/models
curl -s -o nul -w "  :8002 embeddings HTTP %%{http_code}\n" http://127.0.0.1:8002/health
curl -s -o nul -w "  :7860 ComfyUI    HTTP %%{http_code}\n" http://127.0.0.1:7860/system_stats
curl -s -o nul -w "  :9000 Hailuo     HTTP %%{http_code}\n" http://127.0.0.1:9000/health
curl -s -o nul -w "  :9100 CosyVoice  HTTP %%{http_code}\n" http://127.0.0.1:9100/health
curl -s -o nul -w "  :9101 align      HTTP %%{http_code}\n" http://127.0.0.1:9101/health
echo.
nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu --format=csv
pause
exit /b 0

:do_stop
rem Free the GPU. The CPU services hold no VRAM and are left running.
taskkill /FI "WINDOWTITLE eq llama.cpp*" /T /F >nul 2>&1
taskkill /IM llama-server.exe            /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq ComfyUI*"   /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq Hailuo*"    /T /F >nul 2>&1
timeout /t 3 >nul
exit /b 0

rem ==========================================================
rem  Helpers that shell out to PowerShell
rem ==========================================================
:do_getllama
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPTS%\get-llama.ps1" -Dest "%LLAMA%" -Cuda "%LLAMA_CUDA%"
exit /b %errorlevel%

:do_gettorch_cmd
call :do_gettorch "%~2"
exit /b %errorlevel%

rem  :do_gettorch [venv]  - defaults to venv-comfy
:do_gettorch
set "TVENV=%~1"
if "%TVENV%"=="" set "TVENV=%VENVS%\comfy"
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPTS%\get-torch.ps1" -Venv "%TVENV%" %TORCH_CUDA_ARG%
exit /b %errorlevel%

rem ==========================================================
rem  Dependency guards. A venv built by an older run will not have
rem  packages added later, so check before use and install only what
rem  is missing. One python import test when everything is fine.
rem ==========================================================
:ensure_audio
if not exist "%VENVS%\audio\Scripts\activate.bat" goto :eof
call "%VENVS%\audio\Scripts\activate.bat"
python -c "import fastapi, uvicorn, soundfile, funasr" >nul 2>&1
if errorlevel 1 goto fix_audio
python -c "import python_multipart" >nul 2>&1
if not errorlevel 1 goto ea_cosy
python -c "import multipart" >nul 2>&1
if not errorlevel 1 goto ea_cosy
:fix_audio
echo   [deps] venv-audio is missing packages - installing ...
python -m pip install -q -U fastapi uvicorn soundfile python-multipart funasr modelscope
if errorlevel 1 echo   ! some venv-audio packages failed to install
:ea_cosy
call :cosyvoice
call deactivate
goto :eof

rem  CosyVoice2 is not on PyPI - clone it, then install a curated
rem  dependency list. Its own requirements.txt pins grpcio 1.57 and
rem  torch 2.3.1, which have no cp313 wheel and would clobber torch.
:cosyvoice
if not exist "%ROOT%\third_party\CosyVoice\cosyvoice" goto cosy_clone
python -c "import hyperpyyaml, whisper, onnxruntime, diffusers, conformer, wetext" >nul 2>&1
if not errorlevel 1 goto :eof
goto cosy_deps
:cosy_clone
echo   [deps] CosyVoice is not cloned yet ^(it is not on PyPI^) - fetching ...
if not exist "%ROOT%\third_party" mkdir "%ROOT%\third_party"
git clone --recursive https://github.com/FunAudioLLM/CosyVoice "%ROOT%\third_party\CosyVoice"
if errorlevel 1 goto cosy_failed
:cosy_deps
echo   [deps] installing CosyVoice inference requirements ...
set "PIP_CONSTRAINT=%SCRIPTS%\pip-constraints.txt"
python -m pip install -U -r "%SCRIPTS%\cosyvoice-req.txt"
if errorlevel 1 echo   ! some CosyVoice deps failed - TTS may not start
set "PIP_CONSTRAINT="
goto :eof
:cosy_failed
echo   ! CosyVoice clone failed - check git and network. TTS will stay offline.
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

rem  :busy <port> - errorlevel 0 when something is LISTENING on it
:busy
netstat -ano | findstr /c:"127.0.0.1:%~1" | findstr /c:"LISTENING" >nul 2>&1
goto :eof
