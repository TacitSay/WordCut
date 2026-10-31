# -*- coding: utf-8 -*-
<#
停止 WordCut 的前后端服务。

定位方式（两条都试，取并集）：
  1. 监听 8000 / 5173 端口、且进程是 python / node 的（后端命令行里没有项目
     路径，只能靠端口认）；
  2. 命令行里含本项目路径的进程（vite 的 node 进程属于这种）。
两个来源都限定 python / node，避免误杀浏览器等占用端口的程序。
#>

$ErrorActionPreference = 'SilentlyContinue'

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Ports = @(8000, 5173)

function Get-TargetProcesses {
    $ids = New-Object 'System.Collections.Generic.HashSet[int]'

    foreach ($port in $Ports) {
        $connections = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
        foreach ($conn in $connections) {
            $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
            if ($null -eq $proc) { continue }
            if ($proc.ProcessName -eq 'python' -or $proc.ProcessName -eq 'node') {
                [void]$ids.Add([int]$proc.Id)
            }
        }
    }

    $pattern = "*$Root*"
    foreach ($proc in Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='node.exe'") {
        if ($proc.CommandLine -and $proc.CommandLine -like $pattern) {
            [void]$ids.Add([int]$proc.ProcessId)
        }
    }

    return $ids
}

Write-Host ""
Write-Host "  ==========================================" -ForegroundColor Cyan
Write-Host "     WordCut 词剪 - 停止服务" -ForegroundColor Cyan
Write-Host "  ==========================================" -ForegroundColor Cyan
Write-Host ""

$targets = Get-TargetProcesses
if ($targets.Count -eq 0) {
    Write-Host "  没有发现本项目在运行的服务。" -ForegroundColor DarkGray
} else {
    foreach ($processId in $targets) {
        $proc = Get-Process -Id $processId -ErrorAction SilentlyContinue
        if ($null -eq $proc) { continue }
        $label = if ($proc.ProcessName -like 'python*') { '后端 (python/uvicorn)' } else { '前端 (node/vite)' }
        try {
            Stop-Process -Id $processId -Force -ErrorAction Stop
            Write-Host "  已结束 $label  PID=$processId" -ForegroundColor Green
        } catch {
            Write-Host "  结束失败 PID=$processId：$($_.Exception.Message)" -ForegroundColor Yellow
        }
    }
    Write-Host ""
    Write-Host "  共结束 $($targets.Count) 个进程。" -ForegroundColor Green
}

Write-Host ""
