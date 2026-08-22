@echo off
rem Free the GPU: kill whichever heavy slot is running.
taskkill /FI "WINDOWTITLE eq vLLM*"    /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq ComfyUI*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq Hailuo*"  /T /F >nul 2>&1
timeout /t 3 >nul
exit /b 0
