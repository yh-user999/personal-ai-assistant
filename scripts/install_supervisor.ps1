# 安装机器人守护进程（管理员 PowerShell）：pythonw 常驻，零控制台，崩溃秒级拉起
# 用法: 管理员 PowerShell 执行  .\scripts\install_supervisor.ps1
# 迁移说明：
#   - 停用旧的 PAA-Robot 任务（机器人由守护进程拉起，避免双重启动抢单实例锁）
#   - 卸载旧的 PAA-Robot-Watchdog 任务（powershell 每分钟闪一次控制台窗口的元凶）
$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path $PSScriptRoot -Parent
$DesktopDir = Join-Path $ProjectRoot "desktop"
$SupervisorScript = Join-Path $PSScriptRoot "supervise_robot.py"

$PythonDir = Split-Path (Get-Command python).Source
$PythonW = Join-Path $PythonDir "pythonw.exe"
if (-not (Test-Path $PythonW)) {
    throw "找不到 pythonw.exe（预期在 $PythonDir）"
}

# ── 停用旧任务：机器人本体改由守护进程拉起 ────────────────
$oldRobot = Get-ScheduledTask -TaskName "PAA-Robot" -ErrorAction SilentlyContinue
if ($oldRobot) {
    Disable-ScheduledTask -TaskName "PAA-Robot" | Out-Null
    Write-Host "==> 已停用旧任务 PAA-Robot（机器人改由守护进程拉起）"
}

# ── 卸载旧的 powershell 看门狗（闪窗元凶）─────────────────
$oldDog = Get-ScheduledTask -TaskName "PAA-Robot-Watchdog" -ErrorAction SilentlyContinue
if ($oldDog) {
    Unregister-ScheduledTask -TaskName "PAA-Robot-Watchdog" -Confirm:$false
    Write-Host "==> 已卸载旧看门狗 PAA-Robot-Watchdog（powershell 闪窗来源）"
}

# ── 注册守护进程任务 ─────────────────────────────────────
Write-Host "==> 注册 PAA-Robot-Supervisor"
$Action = New-ScheduledTaskAction `
    -Execute $PythonW `
    -Argument "`"$SupervisorScript`"" `
    -WorkingDirectory $DesktopDir
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Trigger.Delay = "PT15S"
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName "PAA-Robot-Supervisor" -Action $Action -Trigger $Trigger -Settings $Settings -Force | Out-Null

$task = Get-ScheduledTask -TaskName "PAA-Robot-Supervisor" -ErrorAction SilentlyContinue
if ($null -eq $task) {
    Write-Host "❌ 注册失败：任务 PAA-Robot-Supervisor 不存在（检查是否以管理员运行）" -ForegroundColor Red
    exit 1
}
Start-ScheduledTask -TaskName "PAA-Robot-Supervisor"
Write-Host "==> 守护进程已启动。日志：$DesktopDir\logs\supervisor.log"
Write-Host "    卸载：Unregister-ScheduledTask -TaskName 'PAA-Robot-Supervisor' -Confirm:`$false"
