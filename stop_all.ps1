# -*- coding: utf-8 -*-
<#
停止一体化：安全终止 MaiBot（含插件宿主） -> 桌宠 -> NapCat(QQ壳)

判定为什么这么写（一体化合并后必须如此）：
- MaiBot 的命令行是 <根目录>\.venv\Scripts\python.exe bot.py，不包含可稳定匹配的工程名，
  所以按路径字符串匹配会永远落空；这里改用「端口持有者」为主、命令行 `bot.py` 为辅。
- NapCat 是注入进 QQ.exe 里运行的，NapCatWinBootMain.exe 只是拉起 QQ 后就退出的引导器。
- 插件宿主 runner_main 每 10 秒守护一次桌宠，必须先停它，否则刚杀掉的桌宠会被立刻重新拉起。
#>
$ErrorActionPreference = "Continue"

Write-Host "================= 正在停止魔理沙一体化工程 =================" -ForegroundColor Yellow

function Get-PortOwnerPid {
    param([int]$Port)
    try {
        $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
            Select-Object -First 1
        if ($conn) { return [int]$conn.OwningProcess }
    } catch { }
    return 0
}

function Stop-OneProcess {
    param([int]$TargetPid, [string]$Label)
    if (-not $TargetPid -or $TargetPid -le 0) { return $false }
    try {
        $proc = Get-Process -Id $TargetPid -ErrorAction Stop
        $procName = $proc.ProcessName
        Stop-Process -Id $TargetPid -Force -ErrorAction Stop
        Write-Host "  已终止 $Label (PID: $TargetPid, $procName)" -ForegroundColor DarkYellow
        return $true
    } catch {
        return $false
    }
}

# 1. 停止 MaiBot 主脑 + 插件宿主
#    顺序很关键：先停插件宿主，否则它的守护循环会把第二步杀掉的桌宠重新拉起来。
Write-Host "[1/3] 正在停止 MaiBot 主脑..." -ForegroundColor Cyan
$maibotAnyStopped = $false

# 1a 插件宿主 runner_main（命令行匹配，或持有桌宠网关端口 8400）
$runnerPids = @()
$runnerProcs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match 'plugin_runtime' }
foreach ($p in $runnerProcs) { $runnerPids += [int]$p.ProcessId }
$runnerPids += Get-PortOwnerPid -Port 8400
foreach ($targetPid in ($runnerPids | Sort-Object -Unique)) {
    if (Stop-OneProcess -TargetPid $targetPid -Label "插件宿主") { $maibotAnyStopped = $true }
}

# 1b 主脑本身（优先认 WebUI 端口 8000/8001 的持有者，再用 bot.py 兜底）
$maibotPids = @()
foreach ($port in 8000, 8001) { $maibotPids += Get-PortOwnerPid -Port $port }
$botProcs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match 'python' -and $_.CommandLine -match 'bot\.py' }
foreach ($p in $botProcs) { $maibotPids += [int]$p.ProcessId }
foreach ($targetPid in ($maibotPids | Sort-Object -Unique)) {
    if (Stop-OneProcess -TargetPid $targetPid -Label "MaiBot") { $maibotAnyStopped = $true }
}

if (-not $maibotAnyStopped) {
    Write-Host "  MaiBot 未在运行。" -ForegroundColor DarkGray
}

# 2. 停止桌宠进程（PyQt6 开发版脚本 / 打包版 exe）
Write-Host "[2/3] 正在停止魔理沙桌宠..." -ForegroundColor Cyan
$petStopped = $false

$petExeProcs = Get-Process -Name "魔理沙桌宠" -ErrorAction SilentlyContinue
if ($petExeProcs) {
    $petExeProcs | Stop-Process -Force -ErrorAction SilentlyContinue
    Write-Host "  已终止桌宠 EXE 进程。" -ForegroundColor DarkYellow
    $petStopped = $true
}

$petPyProcs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match 'python' -and $_.CommandLine -match 'run_pet\.py' }
foreach ($p in $petPyProcs) {
    if (Stop-OneProcess -TargetPid $p.ProcessId -Label "桌宠") { $petStopped = $true }
}

if (-not $petStopped) {
    Write-Host "  桌宠未在运行。" -ForegroundColor DarkGray
}

# 3. 停止 NapCat (QQ登录壳)
#    NapCat 注入在 QQ.exe 内运行，所以先掐它占用的端口持有进程；
#    若端口仍被监听，再回退到官方做法 taskkill /f /im QQ.exe
#    （见 NapCat.Shell.Windows.Node\napcat\KillQQ.bat；注意这会连 QQ 客户端一起关掉）。
Write-Host "[3/3] 正在停止 NapCat (QQ壳)..." -ForegroundColor Cyan
$napcatStopped = $false

$napcatPids = @()
foreach ($port in 3001, 4001, 6099) { $napcatPids += Get-PortOwnerPid -Port $port }
foreach ($targetPid in ($napcatPids | Sort-Object -Unique)) {
    if (Stop-OneProcess -TargetPid $targetPid -Label "NapCat") { $napcatStopped = $true }
}

if ($napcatStopped) { Start-Sleep -Milliseconds 800 }

$napcatStillListening = $false
foreach ($port in 3001, 4001, 6099) {
    if ((Get-PortOwnerPid -Port $port) -ne 0) { $napcatStillListening = $true }
}

if ($napcatStillListening) {
    $qqProcs = Get-Process -Name "QQ" -ErrorAction SilentlyContinue
    if ($qqProcs) {
        $qqProcs | Stop-Process -Force -ErrorAction SilentlyContinue
        Write-Host "  已终止 NapCat (QQ.exe，含 QQ 客户端)。" -ForegroundColor DarkYellow
        $napcatStopped = $true
    }
}

if (-not $napcatStopped) {
    Write-Host "  NapCat 未在运行。" -ForegroundColor DarkGray
}

Write-Host "================= 全部组件已停止 =================" -ForegroundColor Green
