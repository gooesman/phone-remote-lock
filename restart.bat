@echo off
cd /d "%~dp0"
title Restart phone-remote-lock

if not exist ".venv\Scripts\pythonw.exe" (
  echo Please run setup.bat first.
  pause
  exit /b 1
)

echo [1/3] Stopping existing tray / panel / overlay processes ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -match 'lock_phone\.py|panel\.py|overlay\.py' } | ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; Write-Host ('  killed PID ' + $_.ProcessId) } catch { } }"

echo [2/3] Waiting ...
timeout /t 2 /nobreak >nul

echo [3/3] Starting tray ...
start "" ".venv\Scripts\pythonw.exe" "lock_phone.py"

echo.
echo Done. The tray icon should be in the notification area.
echo   Left click  = control panel + mouse mapping overlay
echo   Right click = full menu
echo.
timeout /t 3 /nobreak >nul
exit /b 0
