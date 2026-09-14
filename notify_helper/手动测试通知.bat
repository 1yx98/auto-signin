@echo off
chcp 936 >nul
title 签到通知 - 飞书连通性测试
cd /d "%~dp0"
"%~dp0..\runtime\python.exe" "%~dp0feishu_notify.py" --self-test
echo.
pause
