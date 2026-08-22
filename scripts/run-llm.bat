@echo off
call "%~dp0env.bat"
title vLLM Qwen3.8-27B
echo Slot A - text.  Qwen3.8-27B AWQ, ctx %MAIN_CTX%, port 8000
echo 262K native context does not fit in 24GB of KV cache - feed by chapter instead.
call "%VENVS%\llm\Scripts\activate.bat"
vllm serve "%MODELS%\qwen3.8-27b-awq" --served-model-name qwen3.8-27b --port 8000 --quantization awq_marlin --max-model-len %MAIN_CTX% --gpu-memory-utilization %MAIN_UTIL% --max-num-seqs 4 --enable-prefix-caching --trust-remote-code
pause
