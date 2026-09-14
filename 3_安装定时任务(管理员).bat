@echo off
chcp 936 >nul
cd /d "%~dp0"
rem ===== 自动提权到管理员 =====
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo 正在请求管理员权限——请在弹出的 UAC 窗口点【是/允许】
    echo 授权成功后会自动打开一个新的【管理员】窗口继续执行，本窗口随后自动关闭。
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    timeout /t 3 /nobreak >nul
    exit /b
)
echo ========================================
echo 安装/更新 油学通每日签到 定时任务（最高权限）
echo ========================================
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_task.ps1"
echo.
pause
