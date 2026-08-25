# 一键安装开机自启（管理员 PowerShell）
# 注册两个任务计划：
#   PAA-Collector  采集器（登录后延迟 30s 启动，崩溃自动重启 3 次）
#   PAA-Robot      桌面机器人（登录后启动）
# 用法: 管理员 PowerShell 执行  .\scripts\install_autostart.ps1
$ErrorActionPreference = "Stop"

# 脚本位于 <项目根>\scripts\，上翻一层即项目根
$ProjectRoot = Split-Path $PSScriptRoot -Parent
$CollectorDir = Join-Path $ProjectRoot "collector"
$DesktopDir = Join-Path $ProjectRoot "desktop"

# pythonw.exe（无控制台窗口）与 python.exe 同目录
$PythonDir = Split-Path (Get-Command python).Source
$PythonW = Join-Path $PythonDir "pythonw.exe"
if (-not (Test-Path $PythonW)) {
    throw "找不到 pythonw.exe（预期在 $PythonDir）"
}

# ── ① 采集器 ─────────────────────────────────────────────
Write-Host "==> 注册 PAA-Collector"
$Action1 = New-ScheduledTaskAction -Execute $PythonW -Argument "main.py" -WorkingDirectory $CollectorDir
$Trigger1 = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Trigger1.Delay = "PT30S"  # 登录后延迟 30 秒，避开开机高峰
$Settings1 = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero)  # 无限期运行
Register-ScheduledTask -TaskName "PAA-Collector" -Action $Action1 -Trigger $Trigger1 -Settings $Settings1 -Force | Out-Null

# ── ② 桌面机器人 ─────────────────────────────────────────
Write-Host "==> 注册 PAA-Robot"
$Action2 = New-ScheduledTaskAction -Execute $PythonW -Argument "main.py" -WorkingDirectory $DesktopDir
$Trigger2 = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Trigger2.Delay = "PT15S"
$Settings2 = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 1 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName "PAA-Robot" -Action $Action2 -Trigger $Trigger2 -Settings $Settings2 -Force | Out-Null

Write-Host ""
Write-Host "==> 完成。立即验证（不用重启）："
Write-Host "    Start-ScheduledTask -TaskName 'PAA-Collector'"
Write-Host "    Start-ScheduledTask -TaskName 'PAA-Robot'"
Write-Host "    采集器日志: $CollectorDir\logs\collector.log"
