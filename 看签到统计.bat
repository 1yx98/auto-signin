@echo off
chcp 936 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
title 签到出勤报表

if not exist "runtime\python.exe" (
  echo [ERROR] 找不到 runtime\python.exe，请先按 README 第六节准备运行时。
  pause
  exit /b 2
)

runtime\python.exe report.py %*
echo.
echo ------------------------------------------------------------
echo 想改统计范围：双击本文件看最近 30 天；命令行可加天数参数，
echo 例如  report.py 7  = 最近 7 天，report.py 999 = 全部历史。
echo ------------------------------------------------------------
pause
