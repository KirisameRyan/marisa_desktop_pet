# 外部依赖与本地服务

本文说明 MarisaBOT 跑起来之后**必须或可选依赖的外部东西**：本地推理服务、QQ 协议端、云端 API 与本机客户端。

- 事实来源：本仓库的配置文件与代码**实读**（`config.toml`、`plugin.py`、`service/*.py`、`start_all.ps1`、`docker-compose.yml`）。
- 所有端口、路径、默认值都可在文末「核对位置」一列指向的文件里复核；行号可能随重构漂移，**请按字段名/常量名检索**。
- 本文不写没有证据的数字；无法从仓库确认的，统一放在最后一节「未确认项」。

## 0. 一览表

| 依赖 | 角色 | 默认地址 / 端口 | 缺失时的表现 | 配置位置 |
|---|---|---|---|---|
| GPT-SoVITS 本地语音服务 | 语音合成（TTS） | `http://127.0.0.1:9880` | 语音降级为文字，插件预热失败写 `data/voice_api.log` | `plugins/marisa_voice/config.toml` `[service]` |
| 本地识图推理服务（marisa_cv） | 东方角色识别 | `http://127.0.0.1:9881` | `marisa_identify_character` 工具与 `/识图` 返回失败文案 | `plugins/marisa_cv/config.toml` `[service]` |
| QQ 协议端（PMHQ + LLBot，Docker） | QQ 消息收发 | `127.0.0.1:3001`（适配器连接）；LLBot WebUI `3080` | QQ 通道失效（桌面网关与 WebUI 不受影响） | `deploy/qq-pmhq/docker-compose.yml`、`plugins/MaiBot-Napcat-Adapter/config.toml` `[napcat_server]` |
| 云端 LLM 供应商 | 对话 / 推理 / 向量 | `https://api.deepseek.com` 等 | 主链无法生成回复 | `config/model_config.toml` `[[api_providers]]` |
| DeepSeek 余额 API | `/余额`、`marisa_balance` | `https://api.deepseek.com/user/balance` | 命令与工具返回失败文案 | 桌宠 `config.json` 的 DeepSeek 配置 |
| wttr.in | 天气查询 | `https://wttr.in/<city>?format=j1` | `/天气`、`marisa_weather` 失败 | 无需配置 |
| 网易云音乐本机客户端 | 本机点歌 / 播放控制 | Windows `orpheus://` 协议 | 点歌类指令失败 | 桌宠 `config.json` |
| opencode CLI | 远程代码任务 | 本机 `opencode` 命令 | `marisa_opencode` 工具失败 | `plugins/marisa_gate/config.toml` `[terminal] opencode_cmd` |

## 1. 本地 GPT-SoVITS 语音服务（默认 9880）

### 作用

- 给 `plugins/marisa_voice`（魔理沙语音插件）提供**本地**语音合成：模型调用 `marisa_say` 工具或用户发 `/语音 <文本>` 时，插件把文本 POST 给该服务，拿到 wav 再发出去。
- 同一个服务也被桌面端复用：`plugins/marisa_desktop/core/tts.py` 读取桌宠 `config.json` 的 `gpt_sovits_api_url`（`plugins/marisa_desktop/core/config_manager.py:39` 默认同样是 `http://127.0.0.1:9880`），并写同一份启动日志 `data/voice_api.log`。
- **它同时也是识图服务的解释器来源**：`plugins/marisa_cv/service/start_cls_api.bat` 用该 runtime 的 `python.exe` 启动识图服务（见第 2 节）。也就是说「GPT-SoVITS 那份 Python 运行时」是本仓库两个本地推理服务的公共依赖。

### 默认端口与路径

| 项 | 默认值 | 说明 |
|---|---|---|
| API 地址 | `http://127.0.0.1:9880` | `[service] api_url`；请求路径 `POST {api_url}/tts`（`plugins/marisa_voice/plugin.py:548`），存活探测 `GET {api_url}/`（同文件 `:587-592`） |
| 参考音频 | `D:\GPT-SoVITS-v4-20250421fix(1)\GPT-SoVITS-v4-20250422fix\output\model.wav` | `[service] ref_audio_path`；决定音色，配置里注明「3-10 秒」 |
| 参考文本 | `而且这次不一样，动机和证据我都凑齐了。` | `[service] prompt_text`，必须与参考音频逐字一致 |
| 自动拉起脚本 | `D:\GPT-SoVITS-v4-20250421fix(1)\GPT-SoVITS-v4-20250422fix\run_api.bat` | `[service] api_start_script`，仅在 `auto_start_api = true` 且探测不到服务时使用 |
| 启动等待上限 | `180.0` 秒 | `api_start_timeout_sec`（配置注释：首次加载模型约需一分钟） |
| 单次合成超时 | `45.0` 秒 | `synthesize_timeout_sec` |
| 输出缓存 | `plugins/marisa_voice/data/tts/tts_<12位hex>.wav` | 落盘目录；`_prune_cache(out_dir, keep_hours=24.0)` 会清理 24 小时前的缓存 |
| 启动日志 | `data/voice_api.log` | 插件侧 `_VOICE_API_LOG`；跨进程启动锁 `data/voice_api.start.lock` |

### 缺了会怎样

- 插件在 `_synthesize` 里逐项前置检查，任一不满足就返回 `None`，**不会抛异常打断对话**：
  - 未填参考音频或参考文本 → 日志 `参考音频或参考文本未填写，无法合成`；
  - 参考音频文件不存在 → 日志 `参考音频不存在: <路径>`（`plugins/marisa_voice/plugin.py:521-522`）；
  - 服务地址为空 → 日志 `未配置语音服务地址`；
  - HTTP 非 200 或响应体小于 100 字节 → 日志 `合成返回异常`；
  - 返回内容不是合法 wav → 日志 `返回内容不是有效音频`；
  - 判定为「合成退化」（音频时长超过 `max(下限, 字数 × 每字系数)`）→ 日志 `合成退化`，同样放弃。
- 对模型的表现：`marisa_say` 返回 `{"success": False, "content": "语音合成失败（服务可能未启动、或参考音频配置有误），请直接用文字回复。"}`（同文件 `:420-424`）；`/语音` 则回一句「语音没合成出来……检查下语音服务是否启动、参考音频配置对不对。」（同文件 `:478-481`）。**结果就是这一轮改用文字，不会卡死回复。**
- 宿主启动预热（`prewarm = true`）失败时，会写 `data/voice_api.log`，并用 `marisa_team.marisa_desktop.push_pet_frame` 给桌宠推一条气泡：「语音服务没起来だぜ……主人看看 data/voice_api.log 吧。」（同文件 `:308-312`）。

### 怎么配

1. 改 `plugins/marisa_voice/config.toml` 的 `[service]`（或在 WebUI 插件配置页改，字段与 `ServiceConfig` 一一对应）：
   - `api_url`：换端口或换机器时改这里；
   - `ref_audio_path` / `prompt_text` / `prompt_lang` / `text_lang` / `speed_factor`：音色与语种；
   - `auto_start_api` / `api_start_script` / `api_start_timeout_sec`：是否自动拉起、用哪个脚本；
   - `prewarm`：是否在宿主启动时预热。
2. 语音策略在 `[voice]`：`enabled`、`min_chars`（默认 3）、`max_chars`（默认 60）、`send_mode`（默认 `base64`；`file` 只发本地路径，**协议端容器化后必失败**，见 `deploy/qq-pmhq/README.md` 的「媒体路径陷阱」）、`cooldown_sec`（默认 `30.0`）、`use_hint` / `length_hint`（会拼进工具描述，**改完需热重载插件**）。
3. 桌宠侧独立配置：桌宠 `config.json` 的 `gpt_sovits_api_url`；桌宠与插件共用同一个日志和启动锁，避免重复拉起。

## 2. 本地识图服务（默认 9881）

### 作用

识别图片里的东方角色：WD-ConvNeXt v3 的 ONNX 特征模型 + 数据集自带的 134 类逻辑回归头，只回答「图里有谁」，不给坐标。服务代码是 `plugins/marisa_cv/service/serve.py`，由 `plugins/marisa_cv/plugin.py` 通过 HTTP 调用。

### 运行链路（重要，容易踩错）

- 识图服务**不跑在仓库的 `.venv` 里**。`plugins/marisa_cv/service/start_cls_api.bat` 只有两步：
  1. `cd /d "%~dp0"`
  2. `"D:\GPT-SoVITS-v4-20250421fix(1)\GPT-SoVITS-v4-20250422fix\runtime\python.exe" -u serve.py`
- 也就是说，**这份外部 GPT-SoVITS runtime 同时充当识图服务的解释器与依赖来源**：`onnxruntime`、`numpy`、`PIL`、`fastapi`、`uvicorn` 由它提供。因此「`.venv` 里没有 onnxruntime」是**正常现象**，不代表识图缺依赖。若要排查识图起不来，先看这份 runtime 是否完整、`models/` 是否齐（见下）。
- 本机现状（Lead 实测）：`127.0.0.1:9881` 处于 LISTENING，识图链路是通的。

### 端口与接口

| 项 | 值 | 依据 |
|---|---|---|
| 默认端口 | `9881` | `serve.py` 的 `_DEFAULT_PORT = 9881` |
| 端口覆盖 | 环境变量 `MARISA_CV_PORT` | `serve.py` 的 `main()` 读取该变量 |
| 监听地址 | `127.0.0.1` | `uvicorn.run(app, host="127.0.0.1", ...)` |
| 健康检查 | `GET /` 与 `GET /health` | 返回 `service`、`engine`、`classes`、`input_size` |
| 识别接口 | `POST /classify` | 请求体 `image_base64`（可带 data URI 前缀）、`threshold`、`top_k`；`top_k` 上限 10 |

### 模型文件（不入库，需按需重建）

服务启动加载时会**同步校验四个文件是否都在**，缺一个就立刻 `FileNotFoundError("模型文件缺失: <路径>")`（`serve.py:62-64`）：

| 文件 | 作用 |
|---|---|
| `plugins/marisa_cv/models/wd_features.onnx` | WD-ConvNeXt v3 特征模型（输入 NHWC 0-255，输出 `[B,1024]`） |
| `plugins/marisa_cv/models/head.npz` | 134 类逻辑回归头（`W`/`b`） |
| `plugins/marisa_cv/models/labels.json` | 类别名（Danbooru tag）与元信息 |
| `plugins/marisa_cv/models/labels_zh.json` | tag → 中文角色名 |

**大文件不入库是本仓库的既有约定**：`plugins/marisa_cv/.gitignore` 已排除 `models/backbone/`、`models/wd_features.onnx`、`models/head.npz`、`models/backbone_download.log`。克隆仓库后按下面的 bootstrap 路径重建（`plugins/marisa_cv/tools/` 下均为可重复执行的脚本）：

1. `tools/download_backbone.py` → 下载 WD-ConvNeXt v3 到 `models/backbone/`；
2. `tools/export_features.py` → 从 `models/backbone/model.onnx` 导出 `models/wd_features.onnx` 与口径说明 `models/feature_variant.json`；
3. `tools/convert_head.py` → 产出 `models/head.npz` 与 `models/labels.json`；
4. `tools/validate_features.py` → 用 `wd_features.onnx` + `head.npz` 跑一遍 134 类 LR，校验口径（阈值 0.2）。

`models/labels_zh.json`（中文名映射）在脚本清单中未见生成步骤，属于随数据集提供的文件；**该文件的来源未在 `tools/` 中确认**。

### 缺了会怎样

- 插件 `_classify` 失败即返回 `None`，工具侧给出失败文案，同样不打断对话：
  - 未配置 `api_url` → 日志 `未配置识图服务地址`；
  - 服务不可用：若 `auto_start_api = true`，插件会尝试拉起（等待上限 `api_start_timeout_sec = 120.0`）；仍失败则记录警告，并按配置写 `data/anime_cv.log`（`config.toml` 注释）；
  - HTTP 非 200 → 日志 `识图返回异常`；响应非 JSON → 日志 `识图返回不是 JSON`。
- 服务本身缺模型文件时**是硬失败**：进程启动即抛 `FileNotFoundError`，不会静默降级 —— 这是有意为之（不要用兜底掩盖）。

### 怎么配

- `plugins/marisa_cv/config.toml` `[service]`：`api_url`（默认 `http://127.0.0.1:9881`）、`request_timeout_sec`（30.0）、`auto_start_api`（true）、`api_start_script`（留空使用插件内置 `service/start_cls_api.bat`）、`api_start_timeout_sec`（120.0）、`prewarm`（true）。
- `[cv]`：`enabled`、`threshold`（0.2）、`top_k`（3）、`cooldown_sec`（30.0）、`pending_ttl_sec`（120.0，`/识图` 后等图的秒数）。
- 若把服务挪到别的端口，除改 `api_url` 外，还要让 `start_cls_api.bat` 或在启动前设置 `MARISA_CV_PORT`，两侧保持一致。

## 3. QQ 协议端：PMHQ + LLBot（3001 / 3080）

> 2026-09-30 起，QQ 侧由 **NapCat** 换成 **PMHQ + LLBot**（跑在 Docker 里）。
> 完整部署说明、版本锁定与回退方式见 [`deploy/qq-pmhq/README.md`](../deploy/qq-pmhq/README.md)；
> bypass 的源码审计见 [`deploy/qq-pmhq/bypass/PROVENANCE.md`](../deploy/qq-pmhq/bypass/PROVENANCE.md)。
> 适配器本身没有变，下面仍适用于 `plugins/MaiBot-Napcat-Adapter`。

### 作用

LLBot（LuckyLilliaBot）是 QQ 协议端，对外提供 OneBot 11 / Milky / Satori。
它的 QQ 接入走 PMHQ（内存 hook 注入 QQ 客户端），PMHQ 的账号认证被本仓库的 bypass 就地截断成本地伪造应答，
不再上传到 Luckylillia 服务端。MaiBot 通过适配器 `plugins/MaiBot-Napcat-Adapter`
（`_manifest.json` 中 `plugin_type` 为 `adapter`）连接 LLBot 的 OneBot 11 正向 WS，QQ 才成为平台之一。

### 端口与默认配置

| 项 | 值 | 依据 |
|---|---|---|
| 适配器连接地址 | `127.0.0.1:3001` | `plugins/MaiBot-Napcat-Adapter/config.toml` `[napcat_server] host/port`；LLBot 的 OneBot 11 正向 WS 默认同为 3001 |
| 连接 token | 空字符串（当前实际值） | 同节 `token`；由 `deploy/qq-pmhq/prepare-config.ps1` 原样写进 LLBot 配置，两端天然一致 |
| 心跳 / 重连 / 动作超时 | `30.0` / `5.0` / `15.0` 秒 | 同节 `heartbeat_interval` / `reconnect_delay_sec` / `action_timeout_sec` |
| LLBot WebUI | `3080` | `deploy/qq-pmhq/docker-compose.yml` 的 `127.0.0.1:3080:3080`；首次扫码登录在这里 |
| PMHQ API | `13000`（仅容器网络内） | 同上 compose；容器之间用 `pmhq:13000` 通信，不发布到宿主机 |
| LLBot 其他协议 | Milky `3010`、Satori `5600` | 均默认关闭，见 `prepare-config.ps1` 生成的默认配置 |
| 一体化启动的探测端口 | `3001` | `start_all.ps1` 用 `Test-PortOpen 3001` 判断是否已在运行，未就绪则 `docker compose up -d --build`（等待 180 秒，首次需先扫码） |

`stop_all.ps1` 停止时用 `docker compose down`，不再按端口杀进程
（宿主机上的 3001/3080 是 Docker 的端口代理，杀掉会打坏 Docker 网络而容器还活着）。

### 缺了会怎样

- QQ 平台收不到消息、也发不出去：主链没有 QQ 侧的聊天流来源。
- **桌面端与 WebUI 不受影响**：`marisa_desktop` 是独立适配器（`platform="desktop"`，本地 WS 网关 `127.0.0.1:8400`），WebUI 是 `webui` 平台（MaiBot WebUI 端口 `8001`，见 `config/bot_config.toml` `[webui] port`）。这两条通道不经过 QQ 协议端。
- 适配器侧重连由 `reconnect_delay_sec` 控制，协议端未起来时只会反复重试并刷日志，不会让宿主崩溃。

### 怎么配

1. `plugins/MaiBot-Napcat-Adapter/config.toml`：
   - `[napcat_server] host/port/token`：与协议端实际监听一致（当前 `127.0.0.1:3001`，token 为空）；
   - `[chat]`：`group_list_type` / `group_list`、`private_list_type` / `private_list` 白名单，`ban_user_id`，`ban_qq_bot`（默认 `true`，即忽略其他 bot 的消息）；
   - `[notice]`：各类事件开关（戳一戳、撤回、禁言、群成员增减等）；
   - `[filters]`：`ignore_self_message`（默认 `true`）、正则过滤。
2. LLBot 侧配置**不要手改**：跑 `deploy/qq-pmhq/prepare-config.ps1` 从适配器配置推导生成，
   它会把端口与 token 对齐过去。改动适配器端口后重新生成即可。
3. 一体化启动：双击 `启动一体化.bat`（内部调 `start_all.ps1`），会按 QQ协议端(容器) → MaiBot → 桌宠的顺序幂等拉起。

### 已知限制

适配器调用了一批 OneBot 11 **扩展** action（`get_ai_characters`、`get_ai_record`、`create_flash_task`、
`get_collection_list` 等）。这些不是 OneBot 11 标准，各协议端覆盖不同，换到 LLBot 后**个别功能**可能失效；
这属于协议端实现差异，不是配置问题。

## 4. 其他外部依赖

### 云端 LLM 供应商

`config/model_config.toml` 的 `[[api_providers]]` 当前声明了三家：

| 名称 | base_url |
|---|---|
| DeepSeek | `https://api.deepseek.com` |
| Zhipu | `https://open.bigmodel.cn/api/paas/v4` |
| SiliconFlow | `https://api.siliconflow.cn/v1` |

`[[models]]` 里引用了 `deepseek-reasoner` / `deepseek-chat` / `deepseek-v4-flash` / `glm-4.6v-flash` / `glm-4v-flash` / `Pro/BAAI/bge-m3`（向量）/ `XingChenAGI/XingChenGSR-V1.0` 等（文件名与模型规格以该文件为准）。缺 key 或网络不通时主链无法生成回复；这是**主链必需依赖**，与其他本地服务不同。

### DeepSeek 余额查询

`/余额` 指令与 `marisa_balance` 工具请求 `https://api.deepseek.com/user/balance`（`plugins/marisa_desktop/native/balance.py:13`，`plugins/marisa_desktop/plugin.py:1367`）。缺网络或 key 时返回失败文案。

### 天气

`/天气 <城市>` 与 `marisa_weather` 工具请求 `https://wttr.in/<city>?format=j1`（`plugins/marisa_desktop/plugin.py:1433`、`plugins/marisa_desktop/native/weather.py:33`）；未指定城市时会先用 `https://whois.pconline.com.cn/ipJson.jsp?json=true` 尝试定位（`native/weather.py:10`）。纯外部 HTTP，无需配置。

### 网易云音乐本机客户端

本机点歌 / 播放控制依赖**已安装的网易云音乐客户端**与 `orpheus://` 协议：`plugins/marisa_desktop/native/ncm_client.py` 会从注册表 `Software\Classes\orpheus\shell\open\command` 动态探测客户端路径并修补协议（HKCU 覆盖 HKLM，无需管理员），再通过 `orpheus://<base64 JSON>` 播放单曲或歌单。扫码登录二维码内容为 `https://music.163.com/login?codekey=<uuid>`（`plugins/marisa_desktop/native/third_party/cloud_music_mcp/auth.py:118`）。客户端未安装或协议未注册时，点歌类指令失败。

### opencode CLI 与本机 shell

`plugins/marisa_gate/config.toml` `[terminal]` 设定：`workdir_default = "D:\\MarisaBOT"`、`timeout_shell_sec = 60.0`、`timeout_opencode_sec = 900.0`、`output_cap_chars = 1500`、`opencode_cmd = "opencode"`、`confirm_required = true`。`marisa_shell` / `marisa_shutdown` / `marisa_opencode` 三个工具因此依赖本机 PowerShell、关机权限与 PATH 里的 `opencode` 命令。

## 5. 端口全景（来自启动脚本与配置实读）

| 端口 | 用途 | 依据 |
|---|---|---|
| `3001` | QQ 协议端 OneBot 11 正向 WS（适配器连接） | `deploy/qq-pmhq/docker-compose.yml`、`plugins/MaiBot-Napcat-Adapter/config.toml` |
| `3080` | LLBot WebUI（首次扫码登录） | `deploy/qq-pmhq/docker-compose.yml`、`start_all.ps1` |
| `8001` | MaiBot WebUI / API | `config/bot_config.toml` `[webui] port`、`start_all.ps1` |
| `8400` | 桌宠本地 WS 网关 | `plugins/marisa_desktop/config.toml` `[server] port`、`start_all.ps1` |
| `9880` | GPT-SoVITS 语音服务 | `plugins/marisa_voice/config.toml` |
| `9881` | 本地识图服务 | `plugins/marisa_cv/config.toml`、`serve.py` |

桌宠开发服务另有固定 `7999` 端口（WebUI 开发环境约定，见 `AGENTS.md`），与上表的运行期端口不是一回事。

## 6. 未确认项

以下内容无法从仓库确认，本文不给结论：

- `8001` 在 `[webui] host` 里是列表（`config/bot_config.toml` 写入的是数组形式），实际绑定地址取决于运行期；如需精确结论请查该文件当前值。
- `stop_all.ps1` 会清理 `4001` 端口，但该端口在仓库中**没有**任何配置或代码说明其用途，**用途未确认**。
- `plugins/marisa_cv/models/labels_zh.json` 的生成/分发来源未在 `tools/` 脚本中确认（其余三个模型文件都有对应脚本）。
- `plugins/marisa_voice/config.toml` 里的 GPT-SoVITS 参考音频与 `run_api.bat` 路径是**本机绝对路径**，换机器必须改；仓库内没有随附的默认安装位置约定。
