# -*- coding: utf-8 -*-
<#
停止一体化：安全终止 MaiBot（含插件宿主） -> 桌宠 -> QQ协议端(PMHQ+LLBot 容器)

判定为什么这么写（都是实测踩出来的）：
- MaiBot 的命令行是 <根目录>\.venv\Scripts\python.exe bot.py，不包含可稳定匹配的工程名，
  所以按路径字符串匹配会永远落空；这里改用「端口持有者」为主
  （8000/8001 是主脑、8400 是插件宿主）、命令行匹配为辅。
- **「杀一遍就完事」不成立（实测）**：杀掉插件宿主后 8400 会冒出新的 PID、
  杀掉主脑后它的宿主子进程树仍在——存在守护/自愈。所以顺序必须是
  「先杀主脑断源头 → 再杀宿主 → 再桌宠」，并**循环清扫若干轮**直到端口真正释放。
- QQ 协议端（PMHQ + LLBot）跑在 Docker 容器里，用 compose down 统一停；
  宿主机上的 3001/3080 是 Docker 的端口代理，按端口找进程去杀会打坏 Docker 网络。
- 普通权限跑本脚本杀不掉管理员进程（会只报「未在运行」的假象）——请通过
  「停止一体化.bat」运行（自动提权）；脚本开头也会自检并明确提示。
#>
$ErrorActionPreference = "Continue"

Write-Host "================= 正在停止魔理沙一体化工程 =================" -ForegroundColor Yellow

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

# 权限自检：MaiBot 以管理员运行时，普通权限的 Stop-Process 会被拒绝，
# 旧版这里静默失败 → 只报「未在运行」但实际没杀掉，表现就是「关不掉」。
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "⚠ 当前不是管理员权限，将无法终止以管理员运行的 MaiBot！" -ForegroundColor Red
    Write-Host "  请改用「停止一体化.bat」（会自动提权），或右键本脚本以管理员身份运行。" -ForegroundColor Yellow
}

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
        # 不再静默：失败要显式报出来（旧版吞掉异常导致「未在运行」的误报）
        Write-Host "  ✘ 无法终止 $Label (PID: $TargetPid)：$($_.Exception.Message)" -ForegroundColor Red
        return $false
    }
}

function Get-PidsByCommandLine {
    param([string]$Pattern)
    $result = @()
    $procs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match 'python' -and $_.CommandLine -match $Pattern }
    foreach ($p in $procs) { $result += [int]$p.ProcessId }
    return $result
}

# ---------- 循环清扫：主脑(断源头) -> 插件宿主 -> 桌宠 ----------
# 第 1 轮杀掉主脑之后，它的自愈/守护不可能再拉新；后续轮次只负责清残余。
$maxRounds = 5
$cleaned = $false
for ($round = 1; $round -le $maxRounds; $round++) {
    Write-Host "[第 $round 轮清扫]" -ForegroundColor Cyan
    $killedThisRound = 0

    # 1) MaiBot 主脑：优先杀（断掉一切自愈源头）
    $maibotPids = @()
    foreach ($port in 8000, 8001) { $maibotPids += Get-PortOwnerPid -Port $port }
    $maibotPids += Get-PidsByCommandLine -Pattern 'bot\.py'
    foreach ($targetPid in ($maibotPids | Sort-Object -Unique)) {
        if (Stop-OneProcess -TargetPid $targetPid -Label "MaiBot 主脑") { $killedThisRound++ }
    }

    # 2) 插件宿主 runner_main（持 8400，或命令行含 plugin_runtime）
    $runnerPids = @()
    $runnerPids += Get-PortOwnerPid -Port 8400
    $runnerPids += Get-PidsByCommandLine -Pattern 'plugin_runtime'
    foreach ($targetPid in ($runnerPids | Sort-Object -Unique)) {
        if (Stop-OneProcess -TargetPid $targetPid -Label "插件宿主") { $killedThisRound++ }
    }

    # 3) 桌宠（EXE + run_pet.py）
    $petExeProcs = Get-Process -Name "魔理沙桌宠" -ErrorAction SilentlyContinue
    foreach ($p in $petExeProcs) {
        Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
        Write-Host "  已终止 桌宠 (PID: $($p.Id))" -ForegroundColor DarkYellow
        $killedThisRound++
    }
    $petPyPids = Get-PidsByCommandLine -Pattern 'run_pet\.py'
    foreach ($targetPid in ($petPyPids | Sort-Object -Unique)) {
        if (Stop-OneProcess -TargetPid $targetPid -Label "桌宠") { $killedThisRound++ }
    }

    # 4) 清「父进程已死」的 Python 残留子进程（不持端口也匹配不到命令行的那些）
    #    主脑/宿主被杀后，它们的子进程会变成孤儿继续挂着——按父进程链收掉。
    $killedIds = @()
    foreach ($targetPid in ($maibotPids + $runnerPids + $petPyPids)) {
        if ($targetPid) { $killedIds += [int]$targetPid }
    }
    for ($depth = 0; $depth -lt 2; $depth++) {
        $orphans = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match 'python' -and $killedIds -contains [int]$_.ParentProcessId }
        if (-not $orphans) { break }
        foreach ($p in $orphans) {
            if (Stop-OneProcess -TargetPid ([int]$p.ProcessId) -Label "残留子进程") {
                $killedThisRound++
                $killedIds += [int]$p.ProcessId
            }
        }
    }

    # 本轮收尾检查：端口全释放且没有新匹配 → 干净
    Start-Sleep -Milliseconds 1200
    $busyPorts = @()
    foreach ($port in 8000, 8001, 8400) {
        if (Get-PortOwnerPid -Port $port) { $busyPorts += "$port" }
    }
    if ($busyPorts.Count -eq 0) {
        $cleaned = $true
        break
    }
    Write-Host "  端口仍被占用：$($busyPorts -join '，')，继续下一轮..." -ForegroundColor DarkYellow
}

if (-not $cleaned) {
    Write-Host "⚠ 多轮清扫后仍有端口未释放，可能存在脚本之外的守护进程。" -ForegroundColor Red
}

# ---------- QQ 协议端（PMHQ + LLBot 容器） ----------
$dockerOk = $false
try {
    & docker info *> $null
    $dockerOk = ($LASTEXITCODE -eq 0)
} catch { $dockerOk = $false }

if ($dockerOk) {
    Write-Host "[停止 QQ 协议端 (PMHQ + LLBot 容器)]" -ForegroundColor Cyan
    $QqStopScript = Join-Path $Root "deploy\qq-pmhq\stop.ps1"
    if (Test-Path -LiteralPath $QqStopScript) {
        & "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass `
            -File $QqStopScript | Out-Host
    } else {
        Write-Host "  未找到 $QqStopScript，跳过 QQ 协议端。" -ForegroundColor DarkGray
    }
} else {
    Write-Host "[跳过 QQ 协议端] Docker Desktop 未运行（协议端本就不在跑）。" -ForegroundColor DarkGray
}

# ---------- 最终验证 ----------
Write-Host "================= 全部组件已停止 =================" -ForegroundColor Green
$stillListening = @()
foreach ($port in 8000, 8001, 8400) {
    $owner = Get-PortOwnerPid -Port $port
    if ($owner) { $stillListening += "$port(PID $owner)" }
}
if ($stillListening.Count -gt 0) {
    Write-Host "⚠ 以下端口仍被占用：$($stillListening -join '，') —— 可能还有 MaiBot 进程存活！" -ForegroundColor Red
    Write-Host "  请再运行一次（管理员），或用任务管理器结束对应 PID。" -ForegroundColor Yellow
} else {
    Write-Host "  验证通过：端口 8000/8001/8400 均已释放。" -ForegroundColor Green
}
