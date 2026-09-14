# 重建 校园网自动连接 定时任务（每天 20:50 预连接，保证 20:55 签到开始前网络已就绪）
#
# 说明：
#   - 只做"连上 XSYU_WLAN + 完成校园网认证"，不碰签到流程（签到任务仍然是 20:55 那个）。
#   - 权限用 Limited 就够：netsh wlan connect、HTTP 请求、启动浏览器都不需要管理员。
#     因此本脚本可以直接双击运行，不用提权。
#   - 20:50 跑的时候如果已经有外网，脚本会在 1 秒内直接返回，不会做多余动作。

$ErrorActionPreference = 'Stop'
$dir = $PSScriptRoot
if (-not $dir) { $dir = Split-Path -Parent $MyInvocation.MyCommand.Path }

$taskName    = '校园网自动连接'
$triggerTime = '20:50'
$batPath     = Join-Path $dir 'wifi_helper\自动连接校园网.bat'

try {
    if (-not (Test-Path -LiteralPath $batPath)) {
        throw "找不到 $batPath，请确认 wifi_helper 文件夹完整"
    }
    $act  = New-ScheduledTaskAction -Execute $batPath -WorkingDirectory (Split-Path -Parent $batPath)
    $trg  = New-ScheduledTaskTrigger -Daily -At $triggerTime
    $prin = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
    $set  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
             -StartWhenAvailable -WakeToRun `
             -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -MultipleInstances IgnoreNew

    Register-ScheduledTask -TaskName $taskName -Action $act -Trigger $trg -Principal $prin `
        -Settings $set -Force | Out-Null

    $t = Get-ScheduledTask -TaskName $taskName
    Write-Host ('[OK] 任务名=' + $t.TaskName + '  RunLevel=' + $t.Principal.RunLevel + '  State=' + $t.State) -ForegroundColor Green
    Write-Host ('[OK] 动作=' + $batPath)
    Get-ScheduledTaskInfo -TaskName $taskName | Select-Object LastRunTime, NextRunTime | Format-List
    Write-Host '每天 20:50 触发；20:55 的签到任务照旧，两者互不干扰。' -ForegroundColor Cyan
} catch {
    Write-Host ('[ERROR] ' + $_.Exception.Message) -ForegroundColor Red
    Write-Host '如果提示拒绝访问，请右键"以管理员身份运行"本脚本对应的 bat。' -ForegroundColor Yellow
}
Write-Host '完成，可关闭本窗口'
