@echo off
chcp 936 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHON=%~dp0..\runtime\python.exe
title 查看补丁状态
if not exist "%PYTHON%" (
  echo [错误] 没找到 runtime\python.exe，请确认项目结构完整
  pause & exit /b 1
)
"%PYTHON%" patch_engine.py check
echo.
pause
