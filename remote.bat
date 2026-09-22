@echo off
cd /d "%~dp0"
title Phone Remote Control

if not exist ".venv\Scripts\python.exe" (
  echo Please run setup.bat first.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" "lock_phone.py" menu
exit /b 0
