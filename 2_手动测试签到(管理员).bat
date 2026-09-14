@echo off
chcp 936 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
rem ===== 自动提权到管理员（用于清除电脑管家等高权限弹窗对前台的劫持）=====
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo 正在请求管理员权限——请在弹出的 UAC 窗口点【是/允许】
    echo 授权成功后会自动打开一个新的【管理员】窗口继续执行，本窗口随后自动关闭。
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    timeout /t 3 /nobreak >nul
    exit /b
)
set PYTHON=%~dp0runtime\python.exe
title 油学通签到-手动测试(管理员)
if not exist "%PYTHON%" (
  echo [错误] 没找到 runtime\python.exe，请确认 runtime 文件夹完整
  pause
  exit /b 1
)
echo ========================================
echo 油学通自动签到 - 手动测试（管理员）
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
