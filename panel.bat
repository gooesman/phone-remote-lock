@echo off
cd /d "%~dp0"
title Phone Control Panel

if not exist ".venv\Scripts\pythonw.exe" (
  echo Please run setup.bat first.
  pause
  exit /b 1
)

if "%~1"=="" (
  start "" ".venv\Scripts\pythonw.exe" "panel.py" --cmd panel
) else (
  start "" ".venv\Scripts\pythonw.exe" "panel.py" --cmd %1
)
exit /b 0
