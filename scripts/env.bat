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
set "LLAMA=%ROOT%\llama"
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
rem fits. To go higher, quantize the KV cache - see KV_FLAGS below.
set "MAIN_CTX=16384"
set "SMALL_CTX=32768"

rem Halves KV cache memory and roughly doubles the context you can hold.
rem Set to: --cache-type-k q8_0 --cache-type-v q8_0
set "KV_FLAGS="

set "VIDEO_RES=720x1280"
exit /b 0
