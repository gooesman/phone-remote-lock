@echo off
cd /d "%~dp0"
title phone-remote-lock setup

set "PY="
set "MANAGED=C:\Users\78374\.workbuddy\binaries\python\versions\3.13.12\python.exe"

if exist "%MANAGED%" set "PY=%MANAGED%"
if defined PY goto :havepy

where py >nul 2>nul
if %errorlevel%==0 set "PY=py -3"
if defined PY goto :havepy

where python >nul 2>nul
if %errorlevel%==0 set "PY=python"
if defined PY goto :havepy

echo [ERROR] Python not found. Install Python 3.10+ first.
pause
exit /b 1

:havepy
echo Using Python: %PY%
echo.

if exist ".venv\Scripts\python.exe" goto :deps
echo [1/2] Creating virtual environment .venv ...
%PY% -m venv .venv
if errorlevel 1 goto :fail

:deps
echo [2/2] Installing dependencies (pynput, pystray, Pillow) ...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :fail
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :fail

echo.
echo Setup complete.
echo   start.bat      - run in system tray (hotkeys enabled)
echo   lock_now.bat   - lock the phone once, with output
echo   lock_silent.vbs- lock silently (bind it to a shortcut hotkey)
echo.
pause
exit /b 0

:fail
echo.
echo [ERROR] Setup failed. Read the messages above.
pause
exit /b 1
