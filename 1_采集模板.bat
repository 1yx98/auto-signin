@echo off
chcp 936 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHON=%~dp0runtime\python.exe
title 采集界面模板
if not exist "%PYTHON%" (
  echo [错误] 没找到 runtime\python.exe，请确认 runtime 文件夹完整未被移动/删除
  pause
  exit /b 1
)
"%PYTHON%" capture_templates.py
echo.
echo ========================================
echo 采集流程结束（若上面报错请截图）
echo ========================================
pause
