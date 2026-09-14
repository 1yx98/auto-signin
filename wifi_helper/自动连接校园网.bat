@echo off
rem ============================================================
rem  XSYU_WLAN pre-connect  (20:50 daily, before the 20:55 sign-in)
rem
rem  IMPORTANT: keep EVERY line of this file pure ASCII.
rem  Any non-ASCII (Chinese) text in a .bat breaks cmd parsing when
rem  the task is launched by Task Scheduler -- the file gets read
rem  with a codepage that does not match, and the batch dies early
rem  (LastTaskResult 9009 / 2). Verified the hard way on 2026-09-13.
rem
rem  All the Chinese output you see in task_wifi.log comes from
rem  wifi_auto_login.py (printed as UTF-8), not from this file.
rem ============================================================
set PY=%~dp0..\runtime\python.exe
set LOG=%~dp0task_wifi.log

if not exist "%PY%" (
  echo [%date% %time%] ERROR: runtime\python.exe not found at %PY% >> "%LOG%"
  exit /b 2
)

set PYTHONIOENCODING=utf-8
"%PY%" "%~dp0wifi_auto_login.py" --auto >> "%LOG%" 2>&1
exit /b %errorlevel%
