# -*- coding: utf-8 -*-
<#
WordCut 一键启动（单窗口版）

一个窗口内同时启动后端与前端，并把两边的日志实时汇总到这里：
  [后端] ...   [前端] ...
报错也直接打在这里，不需要再去翻别的窗口。

实现方式：两个服务都用 `Start-Process -NoNewWindow -RedirectStandardOutput` 起在
同一控制台（不弹新窗口），输出写进 .runtime/*.log，再由本脚本 tail 这两个文件、
加前缀打印。这样既只有一个窗口，又能看到真实日志。

参数：
  -NoBrowser   只启动服务，不打开浏览器
  -Restart     先结束本项目已在跑的服务再启动
  -Quiet       只显示错误与关键提示（不 tail 完整日志）
#>
param(
    [switch]$NoBrowser,
    [switch]$Restart,
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'

$Root         = Split-Path -Parent $MyInvocation.MyCommand.Path
$BackendPort  = 8000
$FrontendPort = 5173
$FrontendUrl  = "http://localhost:$FrontendPort"
$HealthUrl    = "http://127.0.0.1:$BackendPort/api/health"
$RuntimeDir   = Join-Path $Root '.runtime'
$BackendLog   = Join-Path $RuntimeDir 'backend.log'
$FrontendLog  = Join-Path $RuntimeDir 'frontend.log'

function Write-Ok($text)   { Write-Host "  [OK] $text" -ForegroundColor Green }
function Write-Warn2($text){ Write-Host "  [!]  $text" -ForegroundColor Yellow }
function Write-Err2($text) { Write-Host "  [X]  $text" -ForegroundColor Red }
function Write-Info($text) { Write-Host "  $text" -ForegroundColor Gray }

function Test-PortInUse([int]$port) {
    # 注意：vite 只监听 IPv6 的 ::1，后端监听 127.0.0.1，两种都要认
    try {
        $listening = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction Stop
        if ($listening) { return $true }
    } catch { }
    foreach ($address in @('127.0.0.1', '::1')) {
        try {
            $client = New-Object System.Net.Sockets.TcpClient
            $iar = $client.BeginConnect($address, $port, $null, $null)
            if ($iar.AsyncWaitHandle.WaitOne(400)) { $client.EndConnect($iar); $client.Close(); return $true }
            $client.Close()
        } catch { }
    }
    return $false
}

function Wait-Url([string]$url, [int]$timeoutSeconds) {
    $deadline = (Get-Date).AddSeconds($timeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 3
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500) { return $true }
        } catch { Start-Sleep -Milliseconds 500 }
    }
    return $false
}

function Stop-ProjectProcesses {
    $ids = New-Object 'System.Collections.Generic.HashSet[int]'
    foreach ($port in @($BackendPort, $FrontendPort)) {
        foreach ($conn in Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
            $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
            if ($proc -and ($proc.ProcessName -eq 'python' -or $proc.ProcessName -eq 'node')) {
                [void]$ids.Add([int]$proc.Id)
            }
        }
    }
    foreach ($proc in Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='node.exe'") {
        if ($proc.CommandLine -and $proc.CommandLine -like "*$Root*") {
            [void]$ids.Add([int]$proc.ProcessId)
        }
    }
    foreach ($processId in $ids) {
        try { Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue } catch { }
    }
    if ($ids.Count -gt 0) { Start-Sleep -Seconds 2 }
}

# ----------------------------------------------------------------------
# 日志 tail：把文件新增内容按行取出（服务在写，我们在读）
# ----------------------------------------------------------------------
function New-Tailer([string]$path) {
    if (-not (Test-Path $path)) { New-Item -ItemType File -Path $path -Force | Out-Null }
    $stream = [System.IO.File]::Open(
        $path,
        [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::Read,
        [System.IO.FileShare]::ReadWrite
    )
    return [pscustomobject]@{ Stream = $stream; Offset = 0; Partial = '' }
}

<#
  用 .NET 的 Process 启动后台命令（输出重定向到日志文件）。

  为什么不用 Start-Process -NoNewWindow -RedirectStandardOutput：PowerShell 5.1
  构造子进程环境变量字典时，若环境里同时有 NO_PROXY 与 no_proxy（Clash 之类
  工具会设置），会直接抛"已添加项。字典中的关键字…"。换成 .NET 的
  ProcessStartInfo 让 CreateProcess 继承环境，就不经过那个字典。
#>
function Start-BackgroundProcess([string]$commandLine, [string]$workdir) {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = "$env:ComSpec"
    $psi.Arguments = '/s /c "' + $commandLine + '"'
    $psi.WorkingDirectory = $workdir
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    return [System.Diagnostics.Process]::Start($psi)
}

function Read-NewLines($tailer) {
    $stream = $tailer.Stream
    $length = $stream.Length
    if ($length -le $tailer.Offset) { return @() }
    try {
        $null = $stream.Seek($tailer.Offset, [System.IO.SeekOrigin]::Begin)
        $count = [int]($length - $tailer.Offset)
        $buffer = New-Object byte[] $count
        $read = $stream.Read($buffer, 0, $count)
        if ($read -le 0) { return @() }
        $tailer.Offset += $read
        $text = $tailer.Partial + [System.Text.Encoding]::UTF8.GetString($buffer, 0, $read)
        $lines = $text -split "`r?`n"
        $tailer.Partial = $lines[$lines.Count - 1]
        if ($lines.Count -le 1) { return @() }
        return $lines[0..($lines.Count - 2)] | Where-Object { $_.Trim() -ne '' }
    } catch {
        return @()
    }
}

function Write-Prefixed([string]$prefix, [string]$colour, $lines) {
    foreach ($line in $lines) {
        Write-Host "  $prefix " -ForegroundColor $colour -NoNewline
        $isError = $line -match 'error|失败|Error|Traceback|EADDRINUSE|Exception'
        if ($isError) {
            Write-Host $line -ForegroundColor Red
        } else {
            Write-Host $line -ForegroundColor DarkGray
        }
    }
}

# ----------------------------------------------------------------------
# 0. 重启
# ----------------------------------------------------------------------
if ($Restart) {
    Write-Info '正在结束本项目已在运行的进程…'
    Stop-ProjectProcesses
}

Write-Host ''
Write-Host '  ==========================================' -ForegroundColor Cyan
Write-Host '     WordCut 词剪 - 一键启动（单窗口）' -ForegroundColor Cyan
Write-Host '  ==========================================' -ForegroundColor Cyan
Write-Host ''

if (-not (Test-Path $RuntimeDir)) { New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null }

# ----------------------------------------------------------------------
# 1. 环境检查
# ----------------------------------------------------------------------
$PythonExe = $null
foreach ($candidate in @('E:\Python\Python310\python.exe', (Join-Path $Root '.venv\Scripts\python.exe'))) {
    if (Test-Path $candidate) { $PythonExe = $candidate; break }
}
if (-not $PythonExe) {
    $found = Get-Command python -ErrorAction SilentlyContinue
    if ($found) { $PythonExe = $found.Source }
}
if (-not $PythonExe) { Write-Err2 '找不到 Python，请安装 Python 3.10 或修改脚本里的路径。'; exit 1 }
Write-Ok "Python: $PythonExe"

if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    Write-Err2 '找不到 npm，请安装 Node.js 20+ 并加入 PATH。'
    exit 1
}
$FrontendDir = Join-Path $Root 'frontend'
if (-not (Test-Path (Join-Path $FrontendDir 'index.html'))) { Write-Err2 "找不到前端目录：$FrontendDir"; exit 1 }

if (-not (Test-Path (Join-Path $FrontendDir 'node_modules\vite'))) {
    Write-Warn2 '前端依赖未安装，先执行 npm install（可能需要几分钟）…'
    & cmd.exe /c "npm install" | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Err2 'npm install 失败，请手动在 frontend 目录执行。'; exit 1 }
    Write-Ok '前端依赖安装完成'
}

# ----------------------------------------------------------------------
# 2. 启动（同一控制台，不弹新窗口）
# ----------------------------------------------------------------------
$started = @()

# 先清空日志（tailer 只读增量，清空必须在启动之前）
foreach ($logPath in @($BackendLog, $FrontendLog, (Join-Path $RuntimeDir 'backend.err.log'), (Join-Path $RuntimeDir 'frontend.err.log'))) {
    try { [System.IO.File]::WriteAllText($logPath, '') } catch { }
}

if (Test-PortInUse $BackendPort) {
    Write-Warn2 "端口 $BackendPort 已被占用，复用现有后端"
} else {
    Write-Info "启动后端 :$BackendPort"
    $backendCmd = "`"$PythonExe`" -m uvicorn api:app --host 127.0.0.1 --port $BackendPort >> `"$BackendLog`" 2>&1"
    $started += (Start-BackgroundProcess $backendCmd (Join-Path $Root 'src'))
}

if (Test-PortInUse $FrontendPort) {
    Write-Warn2 "端口 $FrontendPort 已被占用，复用现有前端"
} else {
    Write-Info "启动前端 :$FrontendPort"
    $frontendCmd = "npm run dev -- --port $FrontendPort --strictPort >> `"$FrontendLog`" 2>&1"
    $started += (Start-BackgroundProcess $frontendCmd $FrontendDir)
}

$backendTail     = New-Tailer $BackendLog
$frontendTail    = New-Tailer $FrontendLog
$backendErrTail  = New-Tailer (Join-Path $RuntimeDir 'backend.err.log')
$frontendErrTail = New-Tailer (Join-Path $RuntimeDir 'frontend.err.log')

Write-Host ''
Write-Host '  ------------------------------------------' -ForegroundColor DarkGray

# ----------------------------------------------------------------------
# 3. 汇总日志 + 等待就绪 + 开浏览器
# ----------------------------------------------------------------------
$browserOpened = $false
$backendReady = $false
$frontendReady = $false
$lastReadyCheck = Get-Date

try {
    while ($true) {
        if (-not $Quiet) {
            Write-Prefixed '[后端]' 'Cyan' (Read-NewLines $backendTail)
            Write-Prefixed '[后端]' 'Cyan' (Read-NewLines $backendErrTail)
            Write-Prefixed '[前端]' 'Blue' (Read-NewLines $frontendTail)
            Write-Prefixed '[前端]' 'Blue' (Read-NewLines $frontendErrTail)
        } else {
            $null = Read-NewLines $backendTail
            $null = Read-NewLines $frontendTail
        }

        $now = Get-Date
        if (($now - $lastReadyCheck).TotalSeconds -ge 2) {
            $lastReadyCheck = $now
            if (-not $backendReady) {
                $backendReady = Wait-Url $HealthUrl 1
                if ($backendReady) {
                    try {
                        $health = Invoke-RestMethod -Uri $HealthUrl -UseBasicParsing -TimeoutSec 5
                        Write-Ok "后端就绪 v$($health.version) · 项目 $($health.project_count) 个 · CUDA $($health.cuda_devices)"
                    } catch { Write-Ok '后端就绪' }
                }
            }
            if (-not $frontendReady) {
                $frontendReady = Wait-Url $FrontendUrl 1
                if ($frontendReady) {
                    Write-Ok "前端就绪 $FrontendUrl"
                    if (-not $NoBrowser -and -not $browserOpened) {
                        Start-Process $FrontendUrl | Out-Null
                        $browserOpened = $true
                        Write-Ok '已在浏览器打开'
                    }
                    Write-Host ''
                    Write-Host '  ------------------------------------------' -ForegroundColor DarkGray
                    Write-Host '  服务日志实时显示在下方；按 Ctrl+C 停止前后端并退出' -ForegroundColor DarkGray
                    Write-Host '  ------------------------------------------' -ForegroundColor DarkGray
                    Write-Host ''
                }
            }
        }

        if ($started.Count -gt 0) {
            $alive = @($started | Where-Object { -not $_.HasExited })
            if ($alive.Count -eq 0) {
                Write-Host ''
                Write-Err2 '前后端进程都已退出，可能启动失败。请检查上方日志。'
                break
            }
        }

        Start-Sleep -Milliseconds 400
    }
} finally {
    Write-Host ''
    Write-Info '正在停止服务…'
    foreach ($proc in $started) {
        try { if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue } } catch { }
    }
    Stop-ProjectProcesses
    foreach ($tailer in @($backendTail, $frontendTail, $backendErrTail, $frontendErrTail)) {
        try { $tailer.Stream.Close() } catch { }
    }
    Write-Ok '已停止。'
}
