@echo off
rem Free the GPU: kill whichever heavy slot is running.
rem The CPU services on 8002 / 9100 / 9101 are left alone - they hold no VRAM.
taskkill /FI "WINDOWTITLE eq llama.cpp*" /T /F >nul 2>&1
taskkill /IM llama-server.exe            /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq ComfyUI*"   /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq Hailuo*"    /T /F >nul 2>&1
timeout /t 3 >nul
exit /b 0
