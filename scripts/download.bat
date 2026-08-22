@echo off
call "%~dp0env.bat"
title Bookreel - download models
call "%VENVS%\llm\Scripts\activate.bat"
echo.
echo   Cache dir: %HF_HOME%
echo   NOTE 24GB cards can only run quantized weights. Do NOT pull BF16 (56GB).
echo.
hf auth whoami >nul 2>&1
if errorlevel 1 (
  echo   ! Not logged in to Hugging Face - gated repos will fail.
  echo     Run menu option L first, then come back.
  echo.
  pause
)

echo   [1/5] %M_MAIN_AWQ%   ~16GB
hf download %M_MAIN_AWQ% --local-dir "%MODELS%\qwen3.8-27b-awq"
if errorlevel 1 (
  echo   ! official AWQ repo unavailable - community GGUF fallback:
  echo     hf download bartowski/Qwen3.8-27B-GGUF --include "*Q4_K_M*" --local-dir "%MODELS%\qwen3.8-27b-gguf"
)

echo   [2/5] %M_SMALL%   ~6GB
hf download %M_SMALL% --local-dir "%MODELS%\qwen3-8b-awq"
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
  echo       Comfy-Org/flux2-dev-ComfyUI       repackaged, drops straight into ComfyUI
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
