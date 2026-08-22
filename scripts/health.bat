@echo off
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
