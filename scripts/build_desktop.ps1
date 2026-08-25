# 打包桌面悬浮球为单个 exe（Windows PowerShell）
# 用法: .\scripts\build_desktop.ps1
$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$DesktopDir = Join-Path $ProjectRoot "desktop"

Push-Location $DesktopDir
& python -m pip install -r requirements.txt
& python -m pip install pyinstaller

& pyinstaller --noconfirm --onefile --windowed --name "PersonalAssistant" main.py

Write-Host "产物: $DesktopDir\dist\PersonalAssistant.exe"
Pop-Location
