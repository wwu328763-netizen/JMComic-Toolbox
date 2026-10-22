@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python not found in PATH.
  echo Install Python 3.10+ and run: pip install jmcomic pillow pyyaml
  pause
  exit /b 1
)

echo Starting JMComic self-use QQ bot ...
echo.
python jmqq.py %*
if errorlevel 1 (
  echo.
  echo Bot exited with error, press any key to close.
  pause
)
