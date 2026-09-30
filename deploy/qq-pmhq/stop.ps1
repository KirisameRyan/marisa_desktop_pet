<#
.SYNOPSIS
    停止 QQ 协议端（PMHQ + LLBot 容器）。

.DESCRIPTION
    默认只停容器、保留数据卷 —— 登录态与 LLBot 配置都留下来，下次启动不用重新扫码。

    注意：宿主机上的 3001/3080 是 Docker 的端口代理，不是协议端进程本身。
    所以这里用 compose down，绝不要按端口找进程去杀（那会打坏 Docker 网络而容器还在跑）。

.PARAMETER Purge
    连同数据卷一起删除。这会**丢失 QQ 登录态**，下次启动需要重新扫码，请确认后再用。

.EXAMPLE
    pwsh -File stop.ps1
    pwsh -File stop.ps1 -Purge
#>
[CmdletBinding()]
param(
    [switch]$Purge
)

$ErrorActionPreference = 'Continue'
$deployDir = $PSScriptRoot

Write-Host '============ 停止 QQ 协议端（PMHQ + LLBot）============' -ForegroundColor Yellow

Push-Location $deployDir
try {
    if ($Purge) {
        Write-Host '[清理] 停止并删除容器与数据卷（登录态会丢失）...' -ForegroundColor Red
        & docker compose down -v 2>&1 | Out-Host
        $dataDir = Join-Path $deployDir 'data\llbot'
        if (Test-Path -LiteralPath $dataDir) {
            Remove-Item -LiteralPath $dataDir -Recurse -Force -ErrorAction SilentlyContinue
            Write-Host '  已删除 data\llbot' -ForegroundColor DarkYellow
        }
    } else {
        & docker compose down 2>&1 | Out-Host
        Write-Host '  容器已停止；登录态与 data\llbot 保留。' -ForegroundColor DarkYellow
    }
} finally {
    Pop-Location
}

Write-Host '================================================' -ForegroundColor Yellow
