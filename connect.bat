@echo off
cd /d "%~dp0"
title Connect phone (wireless adb)
chcp 65001 >nul

if not exist ".venv\Scripts\python.exe" (
  echo Please run setup.bat first.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" "lock_phone.py" connect
echo.
echo Press any key to close this window.
pause >nul
exit /b 0
