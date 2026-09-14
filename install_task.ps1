# 重建 油学通每日签到 定时任务（最高权限）
$ErrorActionPreference = 'Stop'
$dir = $PSScriptRoot
if (-not $dir) { $dir = Split-Path -Parent $MyInvocation.MyCommand.Path }
$taskName = '油学通每日签到'
# 鄠邑校区 20:50 开始签到，20:55 触发（须晚于开放、早于结束）
$triggerTime = '20:55'
try {
    $act  = New-ScheduledTaskAction -Execute (Join-Path $dir 'run_signin_task.bat') -WorkingDirectory $dir
    $trg  = New-ScheduledTaskTrigger -Daily -At $triggerTime
    $prin = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest
    $set  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -WakeToRun -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $taskName -Action $act -Trigger $trg -Principal $prin -Settings $set -Force | Out-Null
    $t = Get-ScheduledTask -TaskName $taskName
    Write-Host ('[OK] RunLevel=' + $t.Principal.RunLevel + '  State=' + $t.State) -ForegroundColor Green
    Get-ScheduledTaskInfo -TaskName $taskName | Select-Object NextRunTime | Format-List
} catch {
    Write-Host ('[ERROR] ' + $_.Exception.Message) -ForegroundColor Red
}
Write-Host '完成，可关闭本窗口'