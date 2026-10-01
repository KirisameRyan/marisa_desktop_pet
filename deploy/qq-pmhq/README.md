# QQ 协议端：PMHQ + LLBot（替换 NapCat）

## 这是什么

2026-09-30 起，MarisaBOT 的 QQ 侧从 **NapCat** 换成了 **PMHQ + LLBot**，部署在 Docker 里。

换的原因只有一个：**PMHQ 会要求把 QQ 相关信息与敏感环境数据上传到 Luckylillia 服务端做账号认证绑定**。
本方案用 [pmhq-bypass](https://github.com/Morpheus799/pmhq-bypass) 把这一步就地截断成本地伪造应答，
认证不再出本机。bypass 的逐行审计见 [`bypass/PROVENANCE.md`](bypass/PROVENANCE.md)。

## 架构

```
[Windows 宿主机]
  MarisaBOT 宿主 (bot.py)  +  MaiBot-Napcat-Adapter
        │  OneBot 11 正向 WebSocket 客户端，ws://127.0.0.1:3001
        │  ⚠ 适配器代码零改动：它连的端口没变，换的只是"谁在 3001 上听"
        ▼
[Docker] marisa-llbot   linyuchen/llbot:8.1.1
        │  PROTOCOL_MODE=pmhq → ws://pmhq:13000/ws
        │  对外提供 OneBot 11(3001) / Milky(3010) / Satori(5600) / WebUI(3080)
        ▼
[Docker] marisa-pmhq    marisa/pmhq-bypass:8.1.1  ← 官方 pmhq:8.1.1 + bypass
        │  内存 hook 注入容器内的 QQ 客户端（Xvfb 无头跑）
        │  认证：tlsserver 在 127.0.0.1:8443 现场自签
        │  api-auth.luckylillia.com 的证书，一律回 {"code":0}
        ▼
[Docker] QQ 客户端（镜像自带，跑在 Xvfb :99 上）
```

宿主机上的 3001 / 3080 都是**只绑回环**，不对外网暴露。

## ⚠ 版本锁死

bypass 的字节补丁只对 **pmhq 8.1.1** 生效（上游原话："其他版本请用户自行逆向"）。
所以 `Dockerfile.pmhq` 的基础镜像写死 `linyuchen/pmhq:8.1.1`——**不要改成 `latest`**。
升级基础镜像时 `patch_libpmhq` 会因字节不匹配直接拒绝，`Dockerfile.pmhq` 里的构建期断言
也会让镜像构建失败，不会出现"看起来起来了其实还在走联网认证"的情况。

## 首次部署

```powershell
cd D:\MarisaBOT\deploy\qq-pmhq

# 1) 生成 LLBot 配置：把适配器的 host/port/token 读出来对齐过去，并自动推断 QQ 号
#    （生成的 config/ 与 .env 都不入库）
powershell -NoProfile -ExecutionPolicy Bypass -File .\prepare-config.ps1

# 2) 构建并启动（首次会编译 bypass，需要几分钟）
docker compose up -d --build

# 3) 登录 QQ（二选一）
#    方式一：浏览器打开 http://127.0.0.1:3080 扫码
#    方式二：看容器日志里打印的二维码
docker compose logs -f llbot
```

登录成功后，登录态落在 Docker 卷 `marisa-qq_qq_login` 与 `./data/llbot/`，
以后重启容器会自动复用，不需要重复扫码。

## 日常启停

已接进仓库原有脚本，**不需要手敲 compose**：

- `启动一体化.bat` / `start_all.ps1` —— 第 1 步会拉起本目录的容器，并生成缺失的 LLBot 配置；
- `stop_all.ps1` —— 第 3 步用 `docker compose down` 停容器（保留卷，登录态不丢）。

单独操作时：

```powershell
docker compose ps                 # 看状态
docker compose logs -f llbot      # 跟 LLBot 日志
docker compose logs -f pmhq       # 跟 PMHQ / QQ 日志
docker compose down               # 停（保留数据）
docker compose down -v            # 停并删卷（会丢登录态，慎用）
```

## 与适配器的关系

`plugins/MaiBot-Napcat-Adapter` **一行代码都不用改**：它是 OneBot 11 正向 WebSocket 的客户端，
按 `[napcat_server]` 的 `host/port/token` 去连协议端。LLBot 的 OneBot 11 正向 WS 默认端口也是 3001，
`prepare-config.ps1` 会把适配器那份配置里的端口与 token 原样写进 LLBot，因此两端天然一致。

唯一需要注意的是：适配器调用了一批 OneBot 11 **扩展** action
（`get_ai_characters`、`get_ai_record`、`create_flash_task`、`get_collection_list` 等）。
这些不是 OneBot 11 标准，各协议端实现对它们的覆盖不同，换到 LLBot 后**个别功能**可能失效。
如果发现某个功能报"接口不存在"，就是这一类，属于协议端实现差异，不是配置问题。

## 已实测验证的证据

搭建过程中实际验证过的（不是推断）：

| 项 | 结果 |
|---|---|
| bypass 补丁字节 | 上游 `libpmhq.so.patched` 与官方 8.1.1 的 `libpmhq.so` 逐字节比对：**仅 5 字节不同**，偏移 `0x76326`，`0F 84 1F 01 00` → `E9 20 01 00 90`，与 `patch_libpmhq.c` 写死的替换完全一致 |
| 官方镜像待补丁字节 | `linyuchen/pmhq:8.1.1` 的 `/opt/libpmhq.so` 在 `0x76326` 处正是 `0f 84 1f 01 00 00` |
| 镜像构建 | 从源码 `gcc` 编译，构建期断言 `test e92001000090 = e92001000090` 通过 |
| 运行时进程树 | `pmhq`(launcher) → fork `tlsserver 8443` → exec `pmhq.real --auth-token=pmhq_bypass`；QQ 客户端被成功拉起 |
| 认证不外发 | 容器 TCP 表里**没有任何指向 Luckylillia 认证服务器的连接**；`tlsserver` 只绑 `127.0.0.1:8443`。外部连接只有腾讯自家的 QQ 服务器（`180.163.x`/`106.55.x`/`220.194.x`）与一个 Google 段端点（Electron 外壳的连通性检查），均属 QQ 客户端本体行为 |
| 健康检查 | `curl http://127.0.0.1:13000/health` 返回 **200** |
| 端到端 | 两个容器 Up，宿主机 `127.0.0.1:3001` 与 `127.0.0.1:3080` 均 LISTENING，LLBot 正常输出登录二维码 |

**未验证的**：登录后的实际消息收发（需要真实扫码）。首次部署后请跑一次群聊/私聊确认。

## ⚠ 媒体路径陷阱（换协议端后最容易踩的坑）

协议端从"与宿主同机的 NapCat"换成"容器里的 LLBot"之后，语义少了一条：
**协议端再也读不到宿主机的文件路径。**

LLBot 对媒体引用的判定（`checkUriType` + `uri2local`，见容器内 `/app/llbot/llbot.js`）：

| 引用形式 | LLBot 行为 |
|---|---|
| `base64://<数据>` | 解码后写入容器内临时文件 ✅ **不依赖宿主机** |
| `http(s)://…` | 由容器自行下载 ✅ |
| `file://D:\…` 或裸 Windows 路径 | `fs.existsSync` 检查 → 容器里不存在 → **抛「路径不存在」** ❌ |

出错时的日志形态：

```
[E] onebot11-adapter 发生错误 Error: 路径不存在
    at handleOb11RichMedia (/app/src/onebot11/transform/message/outgoing.ts:318:11)
```

**已经因此踩过的坑**：`plugins/marisa_voice` 的 `[voice] send_mode` 原默认是 `file`，
它把 wav 的**本地路径**当 URL 交给协议端（适配器的 `_normalize_file_reference` 会补成 `file://D:\…`），
NapCat 与宿主同机时没问题，换成容器化 LLBot 后必失败——机器人表现为回一句「语音发送失败了……」。
现已把代码默认值与配置都改为 `base64`。

**排查同类问题的通用做法**：只要某类消息带"本地文件"，先问一句"协议端在哪儿读这个文件"。

- **图片 / 表情**：MaiBot 侧本来就以内联 base64 下发（适配器 `_build_image_segments` 只认
  `binary_data_base64`），容器化后依然安全；
- **语音 / 视频**：适配器优先用内联 base64，**没有内联时才会退化成路径**——不要让它们退化；
- **显式以 `voiceurl` / `imageurl` / `videourl` / `file` 形式发送的**：天生就是路径或 URL，
  配本地路径必炸。要么改成内联形式，要么给一个容器能访问的 http URL。

## 排障

| 现象 | 处理 |
|---|---|
| 3001 一直不监听 | 先看 `docker compose logs llbot`：多半是还没登录。首次必须扫码 |
| `docker compose up` 报端口占用 | 老的 NapCat 还在跑：`stop_all.ps1`，或确认 3001 没被别的进程占 |
| pmhq 容器 unhealthy | `docker compose logs pmhq` 看 Xvfb/QQ 是否起来；无头容器里 QQ 崩了会打印进程表 |
| 镜像构建失败在 `patch_libpmhq` | 说明基础镜像 `libpmhq.so` 版本变了（不再是 8.1.1 的字节序列），不要绕过断言 |
| 需要看 QQ 界面 | 容器内跑在 Xvfb（`DISPLAY=:99`）上，没有可见界面；用 WebUI 3080 或日志 |

## 回退到 NapCat

本目录是纯增量，回退不需要改代码：

1. `docker compose down`（停容器）；
2. 把 `start_all.ps1` 第 1 步与 `stop_all.ps1` 第 3 步恢复成 NapCat 版本（见 git 历史）；
3. 适配器配置无需改动——NapCat 的 OneBot 11 正向 WS 也是 3001。

## 文件清单

| 路径 | 说明 |
|---|---|
| `Dockerfile.pmhq` | 官方 pmhq:8.1.1 + 从源码编译的 bypass，构建期校验补丁 |
| `docker-compose.yml` | pmhq + llbot 两个服务 |
| `prepare-config.ps1` | 从适配器配置推导 LLBot 配置（端口/token 对齐、自动推断 QQ 号） |
| `bypass/` | bypass 源码（5 个文件）+ `PROVENANCE.md` 审计与哈希 |
| `config/`、`data/`、`.env` | 本地生成物，不入库（见 `.gitignore`） |
