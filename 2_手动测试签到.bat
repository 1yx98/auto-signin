@echo off
chcp 936 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHON=%~dp0runtime\python.exe
title 油学通签到-手动测试
if not exist "%PYTHON%" (
  echo [错误] 没找到 runtime\python.exe，请确认 runtime 文件夹完整未被移动/删除
  pause
  exit /b 1
)
echo ========================================
echo 油学通自动签到 - 手动测试
echo 时间: %date% %time%
echo ========================================
echo.
"%PYTHON%" signin.py
set RC=%errorlevel%
echo.
echo ========================================
echo 执行完毕，退出码: %RC%
echo 0=签到成功   3=不在签到时段(正常)   1/2=失败
echo ========================================
pause
