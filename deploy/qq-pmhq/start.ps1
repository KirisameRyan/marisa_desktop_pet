<#
.SYNOPSIS
    启动 QQ 协议端（PMHQ + LLBot 容器）。

.DESCRIPTION
    幂等：已经在跑就跳过，可反复执行。

    做四件事：
      1. 确认 Docker 守护进程可用（没起就尝试拉起 Docker Desktop 并等待）；
      2. 缺 LLBot 配置就从适配器配置推导生成（见 prepare-config.ps1）；
      3. docker compose up -d（镜像缺失或加了 -Rebuild 时构建）；
      4. 等端口就绪并报告登录状态。

    适配器侧无需干预：MaiBot-Napcat-Adapter 连的是 127.0.0.1:3001，
    本脚本只负责让 LLBot 在这个端口上听。

.PARAMETER Rebuild
    强制重新构建 pmhq 镜像（改了 Dockerfile.pmhq 或 bypass/ 之后用）。

.PARAMETER NoWait
    启动后不等待端口就绪，立即返回。

.EXAMPLE
    pwsh -File start.ps1
    pwsh -File start.ps1 -Rebuild
#>
[CmdletBinding()]
param(
    [switch]$Rebuild,
    [switch]$NoWait
)

$ErrorActionPreference = 'Continue'
$deployDir = $PSScriptRoot
$composeFile = Join-Path $deployDir 'docker-compose.yml'

function Test-PortOpen {
    param([int]$Port, [string]$HostName = '127.0.0.1')
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $task = $client.ConnectAsync($HostName, $Port)
        if ($task.Wait(500)) { $client.Close(); return $true }
        $client.Close()
    } catch {}
    return $false
}

function Wait-PortOpen {
    param([int]$Port, [int]$Seconds, [string]$Label)
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-PortOpen -Port $Port) {
            Write-Host ("  [OK] {0} 已就绪 (:{1})" -f $Label, $Port) -ForegroundColor Green
            return $true
        }
        Start-Sleep -Seconds 2
    }
    return $false
}

Write-Host '============ QQ 协议端（PMHQ + LLBot）============' -ForegroundColor Yellow

if (-not (Test-Path -LiteralPath $composeFile)) {
    Write-Host "[错误] 找不到 $composeFile" -ForegroundColor Red
    exit 1
}

# ---------- 1. Docker 守护进程 ----------
$dockerVersion = & docker info --format '{{.ServerVersion}}' 2>$null
if ($LASTEXITCODE -ne 0 -or -not $dockerVersion) {
    Write-Host '[启动] Docker 守护进程未就绪，尝试拉起 Docker Desktop...' -ForegroundColor Cyan
    $dockerExe = 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
    if (Test-Path -LiteralPath $dockerExe) {
        $running = Get-Process -Name 'Docker Desktop' -ErrorAction SilentlyContinue
        if (-not $running) { Start-Process -FilePath $dockerExe }
        $deadline = (Get-Date).AddSeconds(240)
        while ((Get-Date) -lt $deadline) {
            $dockerVersion = & docker info --format '{{.ServerVersion}}' 2>$null
            if ($LASTEXITCODE -eq 0 -and $dockerVersion) { break }
            Start-Sleep -Seconds 8
        }
    }
    if (-not $dockerVersion) {
        Write-Host '[错误] 240 秒内 Docker 守护进程仍未就绪，请先手动打开 Docker Desktop' -ForegroundColor Red
        exit 1
    }
}
Write-Host ("  Docker {0}" -f $dockerVersion) -ForegroundColor DarkGray

# ---------- 2. LLBot 配置 ----------
$llbotConfig = Join-Path $deployDir 'config\llbot-default_config.json'
if (-not (Test-Path -LiteralPath $llbotConfig)) {
    Write-Host '[准备] 生成 LLBot 配置（对齐适配器的端口与 token）...' -ForegroundColor Cyan
    & "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass `
        -File (Join-Path $deployDir 'prepare-config.ps1') | Out-Host
} else {
    Write-Host '  LLBot 配置已存在，跳过生成（要重新对齐请先删掉 config\ 或跑 prepare-config.ps1 -Force）' -ForegroundColor DarkGray
}

# ---------- 3. 启动容器 ----------
Push-Location $deployDir
try {
    if ($Rebuild) {
        Write-Host '[构建] 重建 pmhq 镜像...' -ForegroundColor Cyan
        & docker compose build pmhq 2>&1 | Out-Host
    }
    Write-Host '[启动] docker compose up -d ...' -ForegroundColor Cyan
    & docker compose up -d 2>&1 | Out-Host
} finally {
    Pop-Location
}

# ---------- 4. 等端口 + 报告 ----------
if (-not $NoWait) {
    Write-Host '[等待] OneBot 11 正向 WS (3001)...' -ForegroundColor Cyan
    # 首次部署还没扫码时 3001 不会就绪，所以这里只警告不中断
    if (-not (Wait-PortOpen -Port 3001 -Seconds 180 -Label 'OneBot WS')) {
        Write-Host '  [警告] 180 秒内未就绪。若是首次部署，需要先登录 QQ：' -ForegroundColor Red
        Write-Host '         浏览器打开 http://127.0.0.1:3080 扫码，或看 docker compose logs -f llbot' -ForegroundColor Red
    }
    Wait-PortOpen -Port 3080 -Seconds 30 -Label 'LLBot WebUI' | Out-Null
}

Push-Location $deployDir
try { & docker compose ps --format '{{.Name}} | {{.Status}} | {{.Ports}}' 2>&1 | Out-Host } finally { Pop-Location }

# 登录状态：LLBot 登录成功后会写出 config_<QQ>.json
$dataDir = Join-Path $deployDir 'data\llbot'
$loggedIn = $false
if (Test-Path -LiteralPath $dataDir) {
    $cfg = Get-ChildItem -LiteralPath $dataDir -Filter 'config_*.json' -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -notmatch 'default' } | Select-Object -First 1
    if ($cfg) {
        $qq = [regex]::Match($cfg.Name, 'config_(\d+)\.json').Groups[1].Value
        Write-Host ("  [已登录] 账号 {0}" -f $qq) -ForegroundColor Green
        $loggedIn = $true
    }
}
if (-not $loggedIn) {
    Write-Host '  [未登录] 需要扫码：浏览器打开 http://127.0.0.1:3080' -ForegroundColor Yellow
}

Write-Host '================================================' -ForegroundColor Yellow
Write-Host '  OneBot 11 WS : ws://127.0.0.1:3001   （适配器连这里）'
Write-Host '  LLBot WebUI  : http://127.0.0.1:3080 （扫码登录 / 看日志）'
