@echo off
call "%~dp0env.bat"
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
echo   [1/4] venv-llm  (vLLM - pulls its own matching torch)
if not exist "%VENVS%\llm" python -m venv "%VENVS%\llm"
call "%VENVS%\llm\Scripts\activate.bat"
python -m pip install -U pip
python -m pip install -U "vllm>=0.11" "huggingface_hub[cli]"
call deactivate

echo.
echo   [2/4] venv-audio  (CosyVoice2 / FunASR - CPU is fine)
if not exist "%VENVS%\audio" python -m venv "%VENVS%\audio"
call "%VENVS%\audio\Scripts\activate.bat"
python -m pip install -U pip
rem Default PyPI already ships CUDA-enabled torch wheels. Pinning an old
rem cu124 index breaks on new Python versions (no cp313/cp314 builds there).
python -m pip install -U torch torchaudio
if errorlevel 1 (
  echo   ! torch from PyPI failed - trying the cu128 index
  python -m pip install -U torch torchaudio --index-url https://download.pytorch.org/whl/cu128
)
if errorlevel 1 (
  echo   ! still failing - trying the CPU-only build (fine for TTS and ASR)
  python -m pip install -U torch torchaudio --index-url https://download.pytorch.org/whl/cpu
)
python -m pip install -U funasr modelscope fastapi uvicorn soundfile
rem CosyVoice2 is not on PyPI - it has to be cloned, and it needs its
rem Matcha-TTS submodule plus a few extras that are not in its own list.
if not exist "%ROOT%\third_party" mkdir "%ROOT%\third_party"
if not exist "%ROOT%\third_party\CosyVoice" (
  git clone --recursive https://github.com/FunAudioLLM/CosyVoice "%ROOT%\third_party\CosyVoice"
) else (
  pushd "%ROOT%\third_party\CosyVoice" && git submodule update --init --recursive & popd
)
if exist "%ROOT%\third_party\CosyVoice\cosyvoice" (
  rem Curated list on purpose - the repo requirements.txt pins grpcio 1.57
  rem and torch 2.3.1, which have no cp313 wheel and would clobber torch.
  set "PIP_CONSTRAINT=%~dp0pip-constraints.txt"
  python -m pip install -U -r "%~dp0cosyvoice-req.txt"
  if errorlevel 1 echo   ! some CosyVoice deps failed - TTS may not start
  set "PIP_CONSTRAINT="
) else (
  echo   ! CosyVoice clone missing - check git and your network
)
python -m pip install -U python-multipart
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
python -m pip install -U torch torchvision
if errorlevel 1 python -m pip install -U torch torchvision --index-url https://download.pytorch.org/whl/cu128
python -m pip install -r "%COMFY%\requirements.txt"
python -c "import torch;print('   torch',torch.__version__,'cuda',torch.cuda.is_available())"
call deactivate

echo.
echo   Install done. Next: menu L (Hugging Face login), then 2 (download).
pause
exit /b 0
