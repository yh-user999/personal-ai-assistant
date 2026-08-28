# 桌面机器人看门狗：检测进程消失则自动拉起（任务计划每分钟触发）
# 安装（管理员 PowerShell）:  .\scripts\install_watchdog.ps1
# 卸载: Unregister-ScheduledTask -TaskName "PAA-Robot-Watchdog" -Confirm:$false
$ErrorActionPreference = "SilentlyContinue"

$DesktopDir = Split-Path $PSScriptRoot -Parent | Join-Path -ChildPath "desktop"

# pythonw.exe 与安装自启脚本同源：优先固定路径，否则从 PATH 的 python 推导
$PythonW = "D:\python\pythonw.exe"
if (-not (Test-Path $PythonW)) {
    $py = (Get-Command python -ErrorAction SilentlyContinue).Source
    if ($py) { $PythonW = Join-Path (Split-Path $py -Parent) "pythonw.exe" }
}
if (-not (Test-Path $PythonW)) { exit 1 }

# 判活：存在命令行引用 desktop\main.py 的 pythonw 进程（机器人 + 面板同进程）
$alive = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -like "*desktop*main.py*" }

if (-not $alive) {
    Start-Process -FilePath $PythonW -ArgumentList "main.py" -WorkingDirectory $DesktopDir
}
