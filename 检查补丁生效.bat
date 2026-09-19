@echo off
chcp 936 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHON=%~dp0runtime\python.exe
title 补丁生效核查
if not exist "%PYTHON%" (
  echo [错误] 没找到 runtime\python.exe
  pause ^& exit /b 1
)
echo ============================================
echo  核查每个补丁在最近的运行里"有没有真的被触发过"
echo  （装了 != 生效；只有补丁后日志里出现过证据才算验证）
echo ============================================
echo.
"%PYTHON%" 检查补丁生效.py 20
echo.
pause
