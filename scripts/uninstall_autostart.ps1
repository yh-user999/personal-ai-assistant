# 卸载开机自启（管理员 PowerShell）
# 用法: 管理员 PowerShell 执行  .\scripts\uninstall_autostart.ps1
$ErrorActionPreference = "SilentlyContinue"

foreach ($name in @("PAA-Collector", "PAA-Robot")) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false
    Write-Host "已卸载: $name"
}
Write-Host "完成。数据与代码不受影响。"
