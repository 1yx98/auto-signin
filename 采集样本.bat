@echo off
chcp 936 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHON=%~dp0runtime\python.exe
title Collect signin UI samples
if not exist "%PYTHON%" (
  echo [ERROR] runtime\python.exe not found. Check the runtime folder.
  pause
  exit /b 1
)
echo ========================================
echo  Collect signin UI samples (read-only)
echo  This tool only grabs screenshots.
echo  It never clicks or controls WeChat.
echo ========================================
echo.
"%PYTHON%" collect_samples.py
echo.
echo Done. Samples are in logs\samples\
pause
