# 安装采集器为开机自启任务（Windows，管理员 PowerShell）
# 用法: 管理员 PowerShell 执行  .\scripts\install_collector.ps1
$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$CollectorDir = Join-Path $ProjectRoot "collector"
$PythonExe = (Get-Command python).Source

Write-Host "==> 安装依赖"
Push-Location $CollectorDir
& python -m pip install -r requirements.txt
Pop-Location

Write-Host "==> 注册开机自启任务"
$Action = New-ScheduledTaskAction -Execute $PythonExe -Argument "main.py" -WorkingDirectory $CollectorDir
$Trigger = New-ScheduledTaskTrigger -AtStartup
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "PersonalAssistantCollector" -Action $Action -Trigger $Trigger -Settings $Settings -Force

Write-Host "==> 立即启动一次"
Start-ScheduledTask -TaskName "PersonalAssistantCollector"
Write-Host "完成。检查: 服务器 /api/events 是否收到事件"
