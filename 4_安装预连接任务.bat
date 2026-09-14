@echo off
chcp 936 >nul
cd /d "%~dp0"
echo ========================================
echo 安装/更新 校园网自动连接 定时任务（每天 20:50）
echo ========================================
echo.
echo 作用：20:50 先检查网络，没网就自动连上 XSYU_WLAN 并完成校园网认证，
echo       这样 20:55 的签到任务开始时网络已经就绪。
echo 权限：普通权限即可（不需要管理员）。
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_wifi_task.ps1"
echo.
pause
