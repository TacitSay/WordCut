# -*- coding: utf-8 -*-
<#
WordCut 一键启动脚本（由 start.bat 调用，也可直接右键「使用 PowerShell 运行」）。

做什么：
  1. 检查 Python / Node 环境与前端依赖；
  2. 后端（uvicorn :8000）与前端（vite :5173）各开一个独立窗口，日志留在窗口里；
  3. 轮询等两边真正可用（不是"进程起来了"就算）；
  4. 自动打开浏览器到 http://localhost:5173。

设计取舍：
  - 端口已在监听时**不重复启动**，直接复用（方便你手动开过服务后再次双击）；
  - 用 `/api/health` 判定后端就绪，而不是固定 sleep，机器慢也不会白等；
  - 前端用 `--strictPort` 锁死 5173，避免 vite 自动跳到 5174 导致打开错地址；
  - 服务窗口保持打开（`cmd /k`），出错时你能直接看到报错。

参数：
  -NoBrowser   只启动服务，不打开浏览器
  -Restart     先结束本项目已在跑的服务再启动
  -NoPause     结束时不等待按键（供脚本/计划任务调用）
#>
param(
    [switch]$NoBrowser,
    [switch]$Restart,
    [switch]$NoPause
)

$ErrorActionPreference = 'Stop'

$Root        = Split-Path -Parent $MyInvocation.MyCommand.Path
$BackendPort  = 8000
$FrontendPort = 5173
$FrontendUrl  = "http://localhost:$FrontendPort"
$HealthUrl    = "http://127.0.0.1:$BackendPort/api/health"

function Write-Step($text) { Write-Host "  $text" -ForegroundColor Gray }
function Write-Ok($text)   { Write-Host "  [OK] $text" -ForegroundColor Green }
function Write-Warn2($text){ Write-Host "  [!]  $text" -ForegroundColor Yellow }
function Write-Err2($text) { Write-Host "  [X]  $text" -ForegroundColor Red }

function Test-PortInUse([int]$port) {
    # 注意：vite 只监听 IPv6 的 ::1，后端监听 127.0.0.1。只测 IPv4 会把
    # "前端已在跑"误判成"没在跑"，于是重复启动一个必然失败的 vite。
    # Get-NetTCPConnection 按端口查监听，与协议族无关，最稳。
    try {
        $listening = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction Stop
        if ($listening) { return $true }
    } catch {
        # 老系统没有该 cmdlet 时退回 TCP 探测（两种协议族都试）
    }

    foreach ($address in @('127.0.0.1', '::1')) {
        try {
            $client = New-Object System.Net.Sockets.TcpClient
            $iar = $client.BeginConnect($address, $port, $null, $null)
            if ($iar.AsyncWaitHandle.WaitOne(400)) {
                $client.EndConnect($iar)
                $client.Close()
                return $true
            }
            $client.Close()
        } catch {
            # 该地址族连不上，继续试下一个
        }
    }
    return $false
}

function Wait-Url([string]$url, [int]$timeoutSeconds) {
    $deadline = (Get-Date).AddSeconds($timeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 3
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500) { return $true }
        } catch {
            Start-Sleep -Milliseconds 700
        }
    }
    return $false
}

function Stop-ProjectProcesses {
    # 按端口 + 命令行双重定位：
    #   - 后端命令行只有 "python -m uvicorn"，不含项目路径，只能靠端口认；
    #   - vite 的 node 进程命令行里有项目路径，靠它兜底。
    # 两条都限定 python / node，避免误杀浏览器等占用端口的程序。
    $ids = New-Object 'System.Collections.Generic.HashSet[int]'

    foreach ($port in @($BackendPort, $FrontendPort)) {
        $connections = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
        foreach ($conn in $connections) {
            $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
            if ($proc -and ($proc.ProcessName -eq 'python' -or $proc.ProcessName -eq 'node')) {
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

    foreach ($processId in $ids) {
        try { Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue } catch { }
    }
    if ($ids.Count -gt 0) { Start-Sleep -Seconds 2 }
}

Write-Host ""
Write-Host "  ==========================================" -ForegroundColor Cyan
Write-Host "     WordCut 词剪 - 一键启动" -ForegroundColor Cyan
Write-Host "  ==========================================" -ForegroundColor Cyan
Write-Host ""

# ----------------------------------------------------------------------
# 0. 可选：重启
# ----------------------------------------------------------------------
if ($Restart) {
    Write-Step "正在结束本项目已在运行的进程…"
    Stop-ProjectProcesses
}

# ----------------------------------------------------------------------
# 1. 环境检查
# ----------------------------------------------------------------------
Write-Step "检查运行环境…"

$PythonExe = $null
$candidates = @(
    'E:\Python\Python310\python.exe',
    (Join-Path $Root '.venv\Scripts\python.exe'),
    'python'
)
foreach ($candidate in $candidates) {
    if ($candidate -eq 'python') {
        $found = Get-Command python -ErrorAction SilentlyContinue
        if ($found) { $PythonExe = $found.Source; break }
    } elseif (Test-Path $candidate) {
        $PythonExe = $candidate; break
    }
}
if (-not $PythonExe) {
    Write-Err2 "找不到 Python。请安装 Python 3.10 或修改本脚本里的路径。"
    exit 1
}
Write-Ok "Python: $PythonExe"

$NpmCmd = Get-Command npm -ErrorAction SilentlyContinue
if (-not $NpmCmd) {
    Write-Err2 "找不到 npm。请安装 Node.js 20+ 并加入 PATH。"
    exit 1
}
Write-Ok "Node  : $((Get-Command node -ErrorAction SilentlyContinue).Source)"

$FrontendDir = Join-Path $Root 'frontend'
if (-not (Test-Path (Join-Path $FrontendDir 'index.html'))) {
    Write-Err2 "找不到前端目录：$FrontendDir"
    exit 1
}

$NodeModules = Join-Path $FrontendDir 'node_modules'
$NeedInstall = -not (Test-Path (Join-Path $NodeModules 'vite'))
if ($NeedInstall) {
    Write-Warn2 "前端依赖未安装（缺 node_modules/vite）"
}

# ----------------------------------------------------------------------
# 2. 启动后端
# ----------------------------------------------------------------------
Write-Host ""
if (Test-PortInUse $BackendPort) {
    Write-Warn2 "端口 $BackendPort 已被占用，跳过启动后端（复用现有服务）"
} else {
    Write-Step "启动后端（uvicorn :$BackendPort，新窗口）…"
    $backendCommand = "title WordCut 后端 :$BackendPort & cd /d `"$Root\src`" & `"$PythonExe`" -m uvicorn api:app --host 127.0.0.1 --port $BackendPort"
    Start-Process -FilePath "$env:ComSpec" -ArgumentList '/k', $backendCommand -WorkingDirectory (Join-Path $Root 'src') | Out-Null
}

# ----------------------------------------------------------------------
# 3. 启动前端
# ----------------------------------------------------------------------
if (Test-PortInUse $FrontendPort) {
    Write-Warn2 "端口 $FrontendPort 已被占用，跳过启动前端（复用现有服务）"
} else {
    if ($NeedInstall) {
        Write-Step "首次运行，先安装前端依赖（npm install，可能需要几分钟）…"
        $installCommand = "title WordCut 依赖安装 & cd /d `"$FrontendDir`" & npm install"
        Start-Process -FilePath "$env:ComSpec" -ArgumentList '/c', $installCommand -WorkingDirectory $FrontendDir -Wait | Out-Null
        Write-Ok "前端依赖安装完成"
    }
    Write-Step "启动前端（vite :$FrontendPort，新窗口）…"
    # --strictPort：端口被占时直接失败，而不是悄悄换到 5174
    $frontendCommand = "title WordCut 前端 :$FrontendPort & cd /d `"$FrontendDir`" & npm run dev -- --port $FrontendPort --strictPort"
    Start-Process -FilePath "$env:ComSpec" -ArgumentList '/k', $frontendCommand -WorkingDirectory $FrontendDir | Out-Null
}

# ----------------------------------------------------------------------
# 4. 等待就绪
# ----------------------------------------------------------------------
Write-Host ""
Write-Step "等待后端就绪（$HealthUrl）…"
if (Wait-Url $HealthUrl 90) {
    try {
        $health = Invoke-RestMethod -Uri $HealthUrl -UseBasicParsing -TimeoutSec 5
        Write-Ok "后端就绪：v$($health.version)，项目 $($health.project_count) 个，采样率 $($health.sample_rate) Hz"
        if ($health.cuda_devices -gt 0) {
            $device = if ($health.whisper_device) { $health.whisper_device } else { '未加载（首次转写时确定）' }
            Write-Step "GPU：检测到 $($health.cuda_devices) 个 CUDA 设备，转写设备 = $device"
        }
    } catch {
        Write-Ok "后端就绪"
    }
} else {
    Write-Err2 "后端 90 秒内没有就绪，请查看「WordCut 后端」窗口里的报错。"
}

Write-Step "等待前端就绪（$FrontendUrl）…"
if (Wait-Url $FrontendUrl 120) {
    Write-Ok "前端就绪：$FrontendUrl"
    if (-not $NoBrowser) {
        Start-Process $FrontendUrl
        Write-Ok "已在浏览器打开"
    }
} else {
    Write-Err2 "前端 120 秒内没有就绪，请查看「WordCut 前端」窗口（首次可能要编译一会儿）。"
}

Write-Host ""
Write-Host "  窗口说明：" -ForegroundColor Cyan
Write-Host "    · WordCut 后端   -> 不要关，关了就没法转写/导出" -ForegroundColor Gray
Write-Host "    · WordCut 前端   -> 不要关，关了页面会白屏" -ForegroundColor Gray
Write-Host "    · 停止服务：双击 stop.bat，或直接关掉上面两个窗口" -ForegroundColor Gray
Write-Host ""
if (-not $NoPause) {
    Write-Host "  按任意键关闭本窗口（服务继续在后台窗口运行）…" -ForegroundColor DarkGray
    try { $null = $Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown') } catch { }
}
