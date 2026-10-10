@echo off
cd /d "%~dp0"
title Install APK to phone
chcp 65001 >nul

if not exist ".venv\Scripts\python.exe" (
  echo Please run setup.bat first.
  pause
  exit /b 1
)

rem 把 APK 文件拖到这个 bat 上，就会装到手机里。
rem 不带参数直接双击的话，会引导你输入路径（也支持把文件拖进窗口）。
".venv\Scripts\python.exe" "lock_phone.py" install-apk %*

echo.
echo Press any key to close this window.
pause >nul
exit /b 0
