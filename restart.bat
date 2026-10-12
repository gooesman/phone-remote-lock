@echo off
cd /d "%~dp0"
title Phone Remote - Start All

if not exist ".venv\Scripts\pythonw.exe" (
  echo Please run setup.bat first.
  pause
  exit /b 1
)

echo [1/3] Stopping old tray / panel / overlay processes ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -match 'lock_phone\.py|panel\.py|overlay\.py' } | ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch { } }"

echo [2/3] Waiting for port release ...
timeout /t 2 /nobreak >nul

echo [3/3] Starting tray + control panel ...
start "" ".venv\Scripts\pythonw.exe" "lock_phone.py"
timeout /t 2 /nobreak >nul
start "" ".venv\Scripts\pythonw.exe" "panel.py" --cmd panel

echo.
echo ============================================================
echo  READY.
echo.
echo   Tray icon lives in the notification area (bottom-right,
echo   may be hidden behind the ^ arrow).
echo     Left-click  tray = open control panel + mapping overlay
echo     Right-click tray = full menu
echo.
echo   In the panel, click the blue "Start" banner button.
echo   Then in the overlay window: HOLD RIGHT MOUSE BUTTON to
echo   control the phone.  Esc closes overlay, V hides its UI.
echo ============================================================
echo.
timeout /t 4 /nobreak >nul
exit /b 0