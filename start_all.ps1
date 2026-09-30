# -*- coding: utf-8 -*-
<#
启动一体化：QQ协议端(PMHQ+LLBot，Docker) -> MaiBot -> 桌宠(由 MaiBot 守护拉起)
幂等：已在运行的组件自动跳过，可反复执行。
#>
$ErrorActionPreference = "Continue"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$MaiBotDir = $Root
$PetExe = Join-Path $Root "dist\魔理沙桌宠\魔理沙桌宠.exe"
$LogDir = Join-Path $Root "data"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

function Test-PortOpen([int]$port, [string]$hostName = "127.0.0.1") {
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $task = $client.ConnectAsync($hostName, $port)
        if ($task.Wait(500)) { $client.Close(); return $true }
        $client.Close()
    } catch {}
    return $false
}

function Wait-PortOpen([int]$port, [int]$seconds, [string]$label) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-PortOpen $port) {
            Write-Host "  [OK] $label 已就绪 (: $port)" -ForegroundColor Green
            return $true
        }
        Start-Sleep -Seconds 2
    }
    return $false
}

Write-Host "================= 魔理沙一体化启动 =================" -ForegroundColor Yellow

# ---------- Step 1: QQ 协议端（PMHQ + LLBot，跑在 Docker 里） ----------
# 2026-09-30 起 QQ 侧由 NapCat 换成 PMHQ + LLBot，部署文件在 deploy/qq-pmhq/。
# 适配器没变（仍是 plugins/MaiBot-Napcat-Adapter，OneBot 11 正向 WS 客户端），
# 它连的还是 127.0.0.1:3001 —— 这一步换的只是"谁在 3001 上听"。
# 具体逻辑（Docker 守护进程、配置生成、等待端口、登录状态）都由子脚本负责，这里不重复实现。
$QqDeployDir = Join-Path $Root "deploy\qq-pmhq"
$QqStartScript = Join-Path $QqDeployDir "start.ps1"
$qqUp = Test-PortOpen 3001
if ($qqUp) {
    Write-Host "[跳过] QQ 协议端已在运行 (3001)" -ForegroundColor DarkYellow
} elseif (Test-Path -LiteralPath $QqStartScript) {
    & "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass `
        -File $QqStartScript | Out-Host
} else {
    Write-Host "[警告] 未找到 $QqStartScript，QQ 侧不会启动" -ForegroundColor Red
}

# 模型密钥只有 config/model_config.toml 一份（WebUI「模型配置」页维护），不再需要启动时同步。

# ---------- Step 2: MaiBot ----------
$maibotUp = Test-PortOpen 8001
$maibotProc = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match 'python' -and $_.CommandLine -match [regex]::Escape($MaiBotDir) }
if ($maibotUp -or $maibotProc) {
    Write-Host "[跳过] MaiBot 已在运行 (8001/进程)" -ForegroundColor DarkYellow
} else {
    Write-Host "[启动] 拉起 MaiBot 大脑..." -ForegroundColor Cyan
    $env:PYTHONUTF8 = "1"
    $env:PYTHONIOENCODING = "utf-8"
    Start-Process -FilePath "$MaiBotDir\.venv\Scripts\python.exe" -ArgumentList "bot.py" `
        -WorkingDirectory $MaiBotDir -WindowStyle Hidden `
        -RedirectStandardOutput "$LogDir\maibot_run.log" `
        -RedirectStandardError "$LogDir\maibot_err.log"
    if (-not (Wait-PortOpen 8001 75 "MaiBot WebUI")) {
        Write-Host "  [警告] MaiBot 75 秒内未就绪，请查看 $LogDir\maibot_err.log" -ForegroundColor Red
    }
}

# ---------- Step 3: 桌宠 ----------
Write-Host "[等待] MaiBot 守护拉起桌宠..." -ForegroundColor Cyan
$petUp = Wait-PortOpen 8400 45 "桌宠连接"
if (-not $petUp) {
    $petProc = Get-Process -Name "魔理沙桌宠" -ErrorAction SilentlyContinue
    if ($petProc) {
        Write-Host "[跳过] 桌宠进程已在运行" -ForegroundColor DarkYellow
    } elseif (Test-Path -LiteralPath $PetExe) {
        Write-Host "[启动] 直接补拉起桌宠 EXE..." -ForegroundColor Cyan
        Start-Process -FilePath $PetExe -WorkingDirectory (Split-Path -Parent $PetExe) -WindowStyle Hidden
        Wait-PortOpen 8400 40 "桌宠连接" | Out-Null
    }
}

Write-Host "================= 启动检查完成 =================" -ForegroundColor Yellow
Write-Host "  LLBot WebUI  : http://127.0.0.1:3080"
Write-Host "  MaiBot WebUI : http://127.0.0.1:8001"
Write-Host "  桌宠网关     : ws://127.0.0.1:8400"
Write-Host "（桌宠窗口若未出现，检查任务栏/托盘区域；日志见 $LogDir\maibot_err.log）"
