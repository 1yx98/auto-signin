@echo off
chcp 936 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHON=%~dp0..\runtime\python.exe
title 补丁引擎自检 - 僵尸窗口过滤
if not exist "%PYTHON%" (
  echo [错误] 没找到 runtime\python.exe
  pause & exit /b 1
)
echo ============================================
echo  负向测试：把护栏改坏，确认它真的会拦住
echo  （9/9 REAL 才算通过；出现"摆设!!"说明护栏失效）
echo ============================================
echo.
"%PYTHON%" test_patch_engine.py
echo.
pause
