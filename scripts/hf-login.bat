@echo off
setlocal EnableDelayedExpansion
call "%~dp0env.bat"
title Bookreel - Hugging Face login
call "%VENVS%\llm\Scripts\activate.bat"
echo.
echo   Hugging Face access
echo   --------------------------------------------------------
hf auth whoami 2>nul
if not errorlevel 1 (
  echo   Already logged in.
  goto gates
)
echo   Not logged in yet.
echo.
echo   1. Create a READ token:  https://huggingface.co/settings/tokens
echo   2. Paste it below (the characters will not be echoed by hf).
echo.
set "HFTOK="
set /p HFTOK=  Token: 
if "!HFTOK!"=="" (echo   No token entered. & pause & exit /b 1)
hf auth login --token !HFTOK! --add-to-git-credential
if errorlevel 1 (echo   x login failed & pause & exit /b 1)

:gates
echo.
echo   Gated repos - open each page ONCE in a browser and click Accept:
echo     https://huggingface.co/black-forest-labs/FLUX.2-dev
echo.
echo   Approval is usually instant. Without it the download returns
echo   "Access denied. This repository requires approval."
echo.
set "OPEN="
set /p OPEN=  Open the FLUX.2 page now? [y/N]: 
if /I "!OPEN!"=="y" start "" https://huggingface.co/black-forest-labs/FLUX.2-dev
pause
exit /b 0
