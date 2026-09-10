# -*- coding: utf-8 -*-
<#
启动一体化：NapCat -> MaiBot -> 桌宠(由 MaiBot 守护拉起)
幂等：已在运行的组件自动跳过，可反复执行。
#>
$ErrorActionPreference = "Continue"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$NapCatDir = Join-Path $Root "NapCat.Shell.Windows.Node\napcat"
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

# ---------- Step 1: NapCat ----------
$napcatUp = Test-PortOpen 3001
$napcatProc = Get-Process -Name "NapCatWinBootMain" -ErrorAction SilentlyContinue
if ($napcatUp -or $napcatProc) {
    Write-Host "[跳过] NapCat 已在运行 (3001/进程)" -ForegroundColor DarkYellow
} else {
    Write-Host "[启动] 拉起 NapCat (QQ 登录壳)..." -ForegroundColor Cyan
    Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "launcher-user.bat" `
        -WorkingDirectory $NapCatDir -WindowStyle Hidden
    if (-not (Wait-PortOpen 3001 90 "NapCat/QQ")) {
        Write-Host "  [警告] NapCat 90 秒内未就绪，继续尝试 MaiBot（可稍后手动重启 NapCat）" -ForegroundColor Red
    }
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
Write-Host "  NapCat WebUI : http://127.0.0.1:6099"
Write-Host "  MaiBot WebUI : http://127.0.0.1:8001"
Write-Host "  桌宠网关     : ws://127.0.0.1:8400"
Write-Host "（桌宠窗口若未出现，检查任务栏/托盘区域；日志见 $LogDir\maibot_err.log）"
