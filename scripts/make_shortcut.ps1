# -*- coding: utf-8 -*-
<#
  在桌面（或指定位置）创建 WordCut 快捷方式，并套用 assets\wordcut.ico 图标。

  为什么需要：`start.bat` 是批处理文件，Windows 只能给它显示一个通用的齿轮图标；
  快捷方式可以把图标写进 .lnk，双击后依然是"单窗口启动"的体验。

  注意：控制台窗口本身的图标由 console host 决定，双击快捷方式不会改变它；
  这里保证的是**桌面/开始菜单/任务栏（固定后）**显示的是新图标。

  用法::

      powershell -ExecutionPolicy Bypass -File scripts\make_shortcut.ps1
      powershell -ExecutionPolicy Bypass -File scripts\make_shortcut.ps1 -Destination "D:\Tools"
#>
param(
    [string]$Destination = [Environment]::GetFolderPath('Desktop'),
    [string]$Name = 'WordCut 词剪'
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$icon = Join-Path $root 'assets\wordcut.ico'
$launcher = Join-Path $root 'start.bat'

if (-not (Test-Path $launcher)) { Write-Error "找不到启动脚本：$launcher"; exit 1 }
if (-not (Test-Path $icon)) {
    Write-Warning "找不到图标 $icon，先运行 python scripts\make_icons.py"
    exit 1
}
if (-not (Test-Path $Destination)) { New-Item -ItemType Directory -Path $Destination -Force | Out-Null }

$linkPath = Join-Path $Destination "$Name.lnk"
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($linkPath)
$shortcut.TargetPath = $launcher
$shortcut.WorkingDirectory = $root
$shortcut.IconLocation = "$icon,0"
$shortcut.Description = 'WordCut 词剪 — 基于词级对齐的文本驱动剪辑'
$shortcut.WindowStyle = 1
$shortcut.Save()

Write-Host "已创建快捷方式：$linkPath" -ForegroundColor Green
Write-Host "  目标：$launcher" -ForegroundColor Gray
Write-Host "  图标：$icon" -ForegroundColor Gray
