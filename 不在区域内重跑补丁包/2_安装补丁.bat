@echo off
chcp 936 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHON=%~dp0..\runtime\python.exe
title 安装补丁 - 不在区域内重跑
if not exist "%PYTHON%" (
  echo [错误] 没找到 runtime\python.exe，请确认项目结构完整
  pause & exit /b 1
)
echo ============================================
echo  即将把补丁装到 signin.py 上
echo  （安装前会自动备份原始文件，可随时拆卸还原）
echo ============================================
echo.
"%PYTHON%" patch_engine.py check
echo.
set /p GO=确认安装吗？输入 y 回车继续：
if /i not "%GO%"=="y" (
  echo 已取消，未做任何改动。
  pause & exit /b 0
)
"%PYTHON%" patch_engine.py apply
echo.
echo ---- 装完跑一遍测试套件确认没破坏现状 ----
"%PYTHON%" ..\smoke_test.py
echo.
pause
