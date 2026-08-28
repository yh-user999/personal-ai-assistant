# 注册桌面机器人看门狗任务计划（管理员 PowerShell，每分钟判活拉起）
# 用法: 管理员 PowerShell 执行  .\scripts\install_watchdog.ps1
$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path $PSScriptRoot -Parent
$WatchdogScript = Join-Path $PSScriptRoot "watchdog_robot.ps1"

$Action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$WatchdogScript`""
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 1) -RepetitionDuration ([TimeSpan]::MaxValue)
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 2) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "PAA-Robot-Watchdog" -Action $Action -Trigger $Trigger -Settings $Settings -Force | Out-Null

Write-Host "==> 看门狗已注册（PAA-Robot-Watchdog，每分钟判活）"
Write-Host "    立即生效，机器人进程消失后 ≤1 分钟自动拉起"
Write-Host "    卸载: Unregister-ScheduledTask -TaskName 'PAA-Robot-Watchdog' -Confirm:`$false"
