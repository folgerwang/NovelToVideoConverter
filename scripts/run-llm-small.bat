@echo off
call "%~dp0env.bat"
title vLLM Qwen3-8B
echo Fallback slot - Qwen3-8B AWQ on port 8000 (faster, weaker).
call "%VENVS%\llm\Scripts\activate.bat"
vllm serve "%MODELS%\qwen3-8b-awq" --served-model-name qwen3.8-27b --port 8000 --quantization awq_marlin --max-model-len 32768 --gpu-memory-utilization 0.60 --enable-prefix-caching
pause
