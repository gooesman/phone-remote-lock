@echo off
rem ============================================================
rem  scrcpy 启动器：电脑上看手机画面 + 用电脑鼠标键盘直接操作手机
rem  用法： 双击本文件即可（自动找已连接的手机，USB 优先）
rem         想指定设备：scrcpy_run.bat 192.168.1.23:42007
rem ============================================================
cd /d "%~dp0"
title scrcpy - phone mirror

set "SCRCPY_DIR=%USERPROFILE%\WorkBuddy\xiaomi\.workbuddy\tools\scrcpy\scrcpy-win64-v5.0.1"
if not exist "%SCRCPY_DIR%\scrcpy.exe" (
  echo [X] 没找到 scrcpy：%SCRCPY_DIR%
  echo     请先下载 scrcpy-win64 压缩包解压到该目录。
  pause
  exit /b 1
)

rem 用我们自己的 adb（和本工具共用同一套设备和配对）
set "ADB=D:\adb\platform-tools\adb.exe"
if not exist "%ADB%" set "ADB=%SCRCPY_DIR%\adb.exe"

set "DEV=%~1"

if "%DEV%"=="" (
  "%SCRCPY_DIR%\scrcpy.exe" ^
    --no-audio ^
    --stay-awake ^
    --max-size=1024 ^
    --window-title="手机画面 scrcpy"
) else (
  "%SCRCPY_DIR%\scrcpy.exe" -s "%DEV%" ^
    --no-audio ^
    --stay-awake ^
    --max-size=1024 ^
    --window-title="手机画面 scrcpy"
)

echo.
echo scrcpy 已退出（按任意键关闭本窗口）
pause >nul
