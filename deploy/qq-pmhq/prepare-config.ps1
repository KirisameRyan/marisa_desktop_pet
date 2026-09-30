<#
.SYNOPSIS
    生成 LLBot 的默认配置，把它的 OneBot 11 正向 WS 对齐到 MarisaBOT 适配器。

.DESCRIPTION
    MarisaBOT 的 plugins/MaiBot-Napcat-Adapter 是 OneBot 11 正向 WebSocket 的**客户端**，
    它按 [napcat_server] 里的 host/port/token 去连协议端。换上 LLBot 之后协议端变了，
    但"连 127.0.0.1:3001 并带同一个 token"这件事没变——所以本脚本把适配器的这份配置
    读出来，写进 LLBot 的 default_config.json，保证两边天然一致。

    生成物（都不入库，见同目录 .gitignore）：
      config/llbot-default_config.json  —— 挂进容器覆盖 /app/llbot/default_config.json
      .env                              —— 供 docker compose 读 AUTO_LOGIN_QQ

.PARAMETER AdapterConfig
    适配器配置文件路径，默认取仓库里的 plugins/MaiBot-Napcat-Adapter/config.toml。

.PARAMETER QQ
    要自动登录的 QQ 号。不传则尝试从 NapCat 的 onebot11_<QQ>.json 文件名里推断。

.PARAMETER Force
    生成物已存在时是否覆盖。默认不覆盖，避免把已经登录后产生的配置冲掉。

.EXAMPLE
    pwsh -File prepare-config.ps1
    pwsh -File prepare-config.ps1 -QQ 123456789 -Force
#>
[CmdletBinding()]
param(
    [string]$AdapterConfig = '',
    [string]$QQ = '',
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

# 脚本在 deploy/qq-pmhq/ 下，仓库根目录是上两级
$deployDir = $PSScriptRoot
$repoRoot = (Resolve-Path (Join-Path $deployDir '..\..')).Path

if ([string]::IsNullOrWhiteSpace($AdapterConfig)) {
    $AdapterConfig = Join-Path $repoRoot 'plugins\MaiBot-Napcat-Adapter\config.toml'
}
if (-not (Test-Path -LiteralPath $AdapterConfig)) {
    throw "找不到适配器配置：$AdapterConfig"
}

# ---------- 1. 从适配器配置里取出连接参数 ----------
$adapterText = Get-Content -LiteralPath $AdapterConfig -Raw -Encoding UTF8
$sectionMatch = [regex]::Match($adapterText, '(?ms)^\[napcat_server\]\s*(.*?)(?=^\[|\z)')
if (-not $sectionMatch.Success) {
    throw "适配器配置里没有 [napcat_server] 段：$AdapterConfig"
}
$section = $sectionMatch.Groups[1].Value

$adapterHost = [regex]::Match($section, '(?m)^\s*host\s*=\s*"([^"]*)"').Groups[1].Value
$adapterPort = [regex]::Match($section, '(?m)^\s*port\s*=\s*(\d+)').Groups[1].Value
$adapterToken = [regex]::Match($section, '(?m)^\s*token\s*=\s*"([^"]*)"').Groups[1].Value

if ([string]::IsNullOrWhiteSpace($adapterPort)) { $adapterPort = '3001' }
if ([string]::IsNullOrWhiteSpace($adapterHost)) { $adapterHost = '127.0.0.1' }

# ---------- 2. 推断 QQ 号（用于快速登录） ----------
if ([string]::IsNullOrWhiteSpace($QQ)) {
    $napcatConfigDir = Join-Path $repoRoot 'NapCat.Shell.Windows.Node\napcat\config'
    if (Test-Path -LiteralPath $napcatConfigDir) {
        $onebot = Get-ChildItem -LiteralPath $napcatConfigDir -Filter 'onebot11_*.json' -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($onebot) {
            $m = [regex]::Match($onebot.Name, '^onebot11_(\d+)\.json$')
            if ($m.Success) { $QQ = $m.Groups[1].Value }
        }
    }
}

# ---------- 3. 组装 LLBot 默认配置 ----------
# 结构与官方 default_config.json 保持一致，只改必要项：
#   - ob11.connect[0]：启用正向 WS，监听 0.0.0.0（容器内必须监听全接口，
#     否则 Docker 的端口发布到不了它），端口/token 与适配器一致；
#   - musicSignUrl 置空：官方默认值指向第三方签名服务，本部署刻意不调用外部服务；
#   - 其余协议（milky / satori）保持关闭。
$llbotConfig = [ordered]@{
    webui = [ordered]@{
        enable = $true
        host   = '0.0.0.0'
        port   = 3080
    }
    milky  = [ordered]@{
        enable            = $false
        reportSelfMessage = $false
        http              = [ordered]@{ host = '0.0.0.0'; port = 3010; prefix = ''; accessToken = '' }
        webhook           = [ordered]@{ urls = @(); accessToken = '' }
    }
    satori = [ordered]@{ enable = $false; host = '0.0.0.0'; port = 5600; token = '' }
    ob11   = [ordered]@{
        enable  = $true
        connect = @(
            [ordered]@{
                type                = 'ws'
                enable              = $true
                host                = '0.0.0.0'
                port                = [int]$adapterPort
                heartInterval       = 60000
                token               = $adapterToken
                reportSelfMessage   = $false
                reportOfflineMessage = $false
                messageFormat       = 'array'
                debug               = $false
            }
            [ordered]@{ type = 'ws-reverse'; enable = $false; url = ''; heartInterval = 60000; token = ''; reportSelfMessage = $false; reportOfflineMessage = $false; messageFormat = 'array'; debug = $false }
            [ordered]@{ type = 'http'; enable = $false; host = '127.0.0.1'; port = 3000; token = ''; reportSelfMessage = $false; reportOfflineMessage = $false; messageFormat = 'array'; debug = $false }
            [ordered]@{ type = 'http-post'; enable = $false; url = ''; enableHeart = $false; heartInterval = 60000; token = ''; reportSelfMessage = $false; reportOfflineMessage = $false; messageFormat = 'array'; debug = $false }
        )
    }
    enableLocalFile2Url    = $false
    log                    = $true
    logLevel               = 'info'
    autoDeleteFile         = $false
    autoDeleteFileSecond   = 60
    # 官方默认是 https://llob.linyuchen.net/sign/music（第三方签名服务）。置空以避免任何外发请求。
    musicSignUrl           = ''
    msgCacheExpire         = 120
    ffmpeg                 = '/usr/bin/ffmpeg'
}

$configDir = Join-Path $deployDir 'config'
$configPath = Join-Path $configDir 'llbot-default_config.json'
$envPath = Join-Path $deployDir '.env'

if ((Test-Path -LiteralPath $configPath) -and -not $Force) {
    Write-Host "[跳过] $configPath 已存在（加 -Force 覆盖）" -ForegroundColor DarkYellow
} else {
    New-Item -ItemType Directory -Path $configDir -Force | Out-Null
    # 关键：不要写 BOM，否则 LLBot 侧 JSON.parse 可能失败
    $json = $llbotConfig | ConvertTo-Json -Depth 6
    [System.IO.File]::WriteAllText($configPath, $json, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "[生成] $configPath" -ForegroundColor Green
}

if ((Test-Path -LiteralPath $envPath) -and -not $Force) {
    Write-Host "[跳过] $envPath 已存在（加 -Force 覆盖）" -ForegroundColor DarkYellow
} else {
    $envText = "# 由 prepare-config.ps1 生成；AUTO_LOGIN_QQ 留空则首次需要扫码登录`nAUTO_LOGIN_QQ=$QQ`n"
    [System.IO.File]::WriteAllText($envPath, $envText, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "[生成] $envPath" -ForegroundColor Green
}

# ---------- 4. 汇总（绝不打印 token 明文） ----------
Write-Host ''
Write-Host '对齐结果：' -ForegroundColor Cyan
Write-Host ("  适配器连接    : {0}:{1}（token {2} 字符）" -f $adapterHost, $adapterPort, $adapterToken.Length)
Write-Host ("  LLBot 监听    : 0.0.0.0:{0}（token 同上，两端一致）" -f $adapterPort)
Write-Host ("  LLBot WebUI   : http://localhost:3080")
Write-Host ("  AUTO_LOGIN_QQ : {0}" -f $(if ([string]::IsNullOrWhiteSpace($QQ)) { '（未设置，首次需扫码）' } else { $QQ }))
Write-Host ''
Write-Host '下一步：docker compose up -d --build' -ForegroundColor Cyan
