@echo off
chcp 936 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHON=%~dp0..\runtime\python.exe
title 列出所有补丁
"%PYTHON%" patch_engine.py list
echo.
pause
