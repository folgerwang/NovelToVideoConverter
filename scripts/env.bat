@echo off
rem Shared paths and tunables for all Bookreel scripts.
set "SCRIPTS=%~dp0"
pushd "%SCRIPTS%.."
set "ROOT=%CD%"
popd
set "MODELS=%ROOT%\models"
set "VENVS=%ROOT%\venvs"
set "LOGS=%ROOT%\logs"
set "COMFY=%ROOT%\ComfyUI"
set "HAILUO_DIR=%ROOT%\hailuo"
set "HF_HOME=%MODELS%\hf"

rem --- model repos ---
set "M_MAIN_AWQ=Qwen/Qwen3.8-27B-AWQ"
set "M_SMALL=Qwen/Qwen3-8B-AWQ"
set "M_EMBED=Qwen/Qwen3-Embedding-0.6B"
set "M_FLUX=black-forest-labs/FLUX.2-dev"
set "M_TTS=FunAudioLLM/CosyVoice2-0.5B"

rem --- 24GB tunables: longer context = more KV cache ---
set "MAIN_CTX=32768"
set "MAIN_UTIL=0.92"
set "VIDEO_RES=720x1280"
exit /b 0
