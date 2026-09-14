@echo off
chcp 936 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHON=%~dp0runtime\python.exe
title 油学通签到-直接运行
if not exist "%PYTHON%" (
  echo [错误] 没找到 runtime\python.exe，请确认 runtime 文件夹完整
  pause
  exit /b 1
)
echo ========================================
echo 油学通自动签到脚本启动
echo 时间: %date% %time%
echo ========================================
"%PYTHON%" signin.py
echo.
echo 脚本执行完毕，退出码: %errorlevel%
pause
