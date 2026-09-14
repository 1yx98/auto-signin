@echo off
title XSYU_WLAN Auto Login Test
cd /d "%~dp0"
echo ============================================================
echo   XSYU_WLAN Auto Connect and Auth Test
echo ============================================================
echo.
"%~dp0..\runtime\python.exe" "%~dp0wifi_auto_login.py"
echo.
pause
