@echo off
cd /d "%~dp0"
title phone-remote-lock setup

set "PY="

rem 1) WorkBuddy bundled managed Python (preferred if present; path is not hardcoded to a user name)
set "MANAGED=%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if exist "%MANAGED%" set "PY=%MANAGED%"
if defined PY goto :havepy

rem 2) py launcher on PATH
where py >nul 2>nul
if %errorlevel%==0 set "PY=py -3"
if defined PY goto :havepy

rem 3) python on PATH
where python >nul 2>nul
if %errorlevel%==0 set "PY=python"
if defined PY goto :havepy

echo [ERROR] Python not found.
echo         Install Python 3.10+ from https://www.python.org/downloads/
echo         and tick "Add python.exe to PATH" during setup.
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
echo [2/2] Installing dependencies (pynput, pystray, Pillow, PySide6) ...
echo       PySide6 is around 150 MB, this may take a couple of minutes.
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :fail
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :fail

echo.
echo Setup complete.
echo   start.bat       - run in the system tray (hotkeys enabled)
echo   restart.bat     - kill old processes and restart the tray
echo   panel.bat       - open the control panel
echo   overlay.bat     - open the mouse mapping overlay
echo   remote.bat      - text menu remote
echo   lock.bat        - lock the phone once, with output
echo   lock_silent.vbs - lock silently (bind it to a shortcut hotkey)
echo.
echo Next: see README.md section 2 for the phone-side setup.
echo.
pause
exit /b 0

:fail
echo.
echo [ERROR] Setup failed. Read the messages above.
pause
exit /b 1
