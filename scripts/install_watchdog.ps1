# 注册桌面机器人看门狗任务计划（管理员 PowerShell，每分钟判活拉起）
# 用法: 管理员 PowerShell 执行  .\scripts\install_watchdog.ps1
$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path $PSScriptRoot -Parent
$WatchdogScript = Join-Path $PSScriptRoot "watchdog_robot.ps1"

$Action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$WatchdogScript`""

# 每 1 分钟触发一次。注意：不写 -RepetitionDuration——
# [TimeSpan]::MaxValue 会生成越界 XML（P99999999DT23H59M59S，0x80041318），
# 省略该参数时新版任务计划默认无限期重复。
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 1)

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 2) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "PAA-Robot-Watchdog" -Action $Action -Trigger $Trigger -Settings $Settings -Force | Out-Null

# 注册后回读校验——Register 成功与否以任务真实存在为准，不轻信返回值
$task = Get-ScheduledTask -TaskName "PAA-Robot-Watchdog" -ErrorAction SilentlyContinue
if ($null -eq $task) {
    Write-Host "❌ 注册失败：任务 PAA-Robot-Watchdog 不存在（检查是否以管理员运行）" -ForegroundColor Red
    exit 1
}
Start-ScheduledTask -TaskName "PAA-Robot-Watchdog"  # 立即跑一轮，马上把消失的机器人拉起
Start-Sleep -Seconds 3
$info = Get-ScheduledTaskInfo -TaskName "PAA-Robot-Watchdog"
Write-Host "==> 看门狗已注册并启动（上次运行结果码: $($info.LastTaskResult)）" -ForegroundColor Green
Write-Host "    机器人进程消失后 ≤1 分钟自动拉起"
Write-Host "    卸载: Unregister-ScheduledTask -TaskName 'PAA-Robot-Watchdog' -Confirm:`$false"
