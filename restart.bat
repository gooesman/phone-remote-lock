@echo off
cd /d "%~dp0"
title Phone Remote - Start All

if not exist ".venv\Scripts\pythonw.exe" (
  echo Please run setup.bat first.
  pause
  exit /b 1
)

echo [1/3] Stopping old tray / panel / overlay processes ...
rem 用 Python 自己来找并结束进程，不依赖 PowerShell（有些环境会被拦/报错）
".venv\Scripts\python.exe" -c "import psutil" >nul 2>&1
if errorlevel 1 (
  echo     (psutil not available - using taskkill)
  taskkill /F /IM pythonw.exe >nul 2>&1
) else (
  ".venv\Scripts\python.exe" -c "import psutil;[psutil.Process(p.pid).kill() for p in psutil.process_iter(['name','cmdline']) if p.info['name'] and 'python' in p.info['name'].lower() and p.info['cmdline'] and any(x in ' '.join(p.info['cmdline']) for x in ('lock_phone.py','panel.py','overlay.py','covert.py'))]" >nul 2>&1
)

echo [2/3] Waiting for port release ...
timeout /t 2 /nobreak >nul

echo [3/3] Starting tray + control panel ...
start "" ".venv\Scripts\pythonw.exe" "lock_phone.py"
timeout /t 3 /nobreak >nul
start "" ".venv\Scripts\pythonw.exe" "panel.py" --cmd panel

echo.
echo ============================================================
echo  READY - tray icon should be in the notification area now
echo  (bottom-right corner; click the ^ arrow if it is hidden).
echo.
echo    Left-click  tray = control panel + mapping overlay
echo    Right-click tray = full menu
echo.
echo  Mapping overlay: hold the RIGHT mouse button to control the
echo  phone.  Esc closes it, V hides its UI, wheel resizes it.
echo ============================================================
echo.
timeout /t 4 /nobreak >nul
exit /b 0