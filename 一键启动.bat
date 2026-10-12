@echo off
cd /d "%~dp0"
title Phone Remote Launcher

if not exist ".venv\Scripts\pythonw.exe" (
  echo [X] Missing venv. Please run setup.bat first.
  pause
  exit /b 1
)

echo Starting tray ...
start "" ".venv\Scripts\pythonw.exe" "lock_phone.py"

timeout /t 2 /nobreak >nul

echo Starting control panel ...
start "" ".venv\Scripts\pythonw.exe" "panel.py" --cmd panel

echo.
echo ============================================================
echo  Started. Look for the tray icon in the notification area
echo  (bottom-right, click the ^ arrow to see hidden icons).
echo.
echo    Left-click tray  = control panel + mapping overlay
echo    Right-click tray = menu
echo
echo  Mapping overlay: hold RIGHT mouse button to control the phone,
echo    Esc = close overlay,  V = hide overlay UI,  wheel = resize
echo ============================================================
echo.
timeout /t 4 /nobreak >nul
exit /b 0