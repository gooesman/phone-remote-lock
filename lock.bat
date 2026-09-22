@echo off
cd /d "%~dp0"
title Lock phone

if not exist ".venv\Scripts\python.exe" (
  echo Please run setup.bat first.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" "lock_phone.py" lock
timeout /t 2 >nul
exit /b 0
