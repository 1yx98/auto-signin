@echo off
chcp 936 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHON=%~dp0..\runtime\python.exe
title 拆卸补丁
if not exist "%PYTHON%" (
  echo [错误] 没找到 runtime\python.exe，请确认项目结构完整
  pause & exit /b 1
)
echo ============================================
echo  即将把 signin.py 还原成打补丁之前的版本
echo  （逐字节还原，并用 sha256 校验）
echo ============================================
echo.
"%PYTHON%" patch_engine.py check
echo.
set /p GO=确认拆卸吗？输入 y 回车继续：
if /i not "%GO%"=="y" (
  echo 已取消，未做任何改动。
  pause & exit /b 0
)
"%PYTHON%" patch_engine.py revert
echo.
"%PYTHON%" patch_engine.py check
echo.
pause
