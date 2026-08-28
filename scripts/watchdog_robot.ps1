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

# 判活：机器人以 --robot 参数启动（desktop/main.py 入口自动附加），
# 与采集器（同为 pythonw main.py）唯一可区分的特征
$alive = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -like "*--robot*" }

if (-not $alive) {
    # 必须带 --robot 拉起：否则拉起的实例命令行里没有判活标记，
    # 下个周期看门狗看不到它 → 每分钟多拉一个机器人（多实例堆积 bug）
    Start-Process -FilePath $PythonW -ArgumentList "main.py --robot" -WorkingDirectory $DesktopDir
}
