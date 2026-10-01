# 插件约定与第一方插件 API

本文分两部分：

1. **插件约定**：本仓库（MarisaBOT）怎么发现、校验、加载一个插件，`_manifest.json` / `config.toml` / `plugin.py` 各要写什么；
2. **第一方插件 API**：`marisa_cv` / `marisa_desktop` / `marisa_gate` / `marisa_voice` 各自提供的指令、工具、网关与外部依赖。

- 事实来源：`src/plugin_runtime/` 下的宿主实现，以及四个插件目录里的 `_manifest.json`、`config.toml`、`plugin.py`（均为实读）。
- 行号会随重构漂移，**请按字段名 / 常量名 / 装饰器名检索**。
- 无法从仓库确认的内容统一放在文末「未确认项」。

## 1. 插件目录与加载

| 项 | 位置 | 依据 |
|---|---|---|
| 内置插件目录 | `src/plugins/built_in/` | `src/plugin_runtime/integration.py`（`_resolve_*_plugin_dirs`，注释「内置插件目录」） |
| 第三方插件目录 | `plugins/` | 同上，注释「第三方插件目录」 |
| 插件持久数据目录 | `data/plugins/` | `src/plugin_runtime/runner/plugin_paths.py` 的 `data_root` |
| 插件运行临时目录 | `temp/plugins/` | 同文件 `runtime_root` |
| 插件 SDK | `maibot-plugin-sdk`（按 `pyproject.toml` 声明） | 插件内统一 `from maibot_sdk import ...` |

一个插件目录至少要有 `plugin.py` 与 `_manifest.json`（`src/plugin_runtime/docker_layout_migration.py` 把这两个文件当作目录标记），配置用 `config.toml`，更新日志惯例放 `CHANGELOG.md`（由 manifest 的 `changelog` 字段指向）。

## 2. `_manifest.json`：Manifest v2 字段

宿主用强类型模型校验 manifest（`src/plugin_runtime/runner/manifest_validator.py`，`PluginManifest`）。当前只支持 `manifest_version = 2`（`SUPPORTED_MANIFEST_VERSIONS = [2]`）。

| 字段 | 必填 | 约束 / 说明 |
|---|---|---|
| `manifest_version` | ✅ | 字面量 `2` |
| `id` | ✅ | 稳定插件 ID。正则 `^[A-Za-z0-9_]+(?:[.-][A-Za-z0-9_]+)+$`，即字母/数字/下划线，至少一个 `.` 或 `-` 分隔，例如 `github.author.plugin`、`marisa-team.marisa-cv` |
| `version` | ✅ | **严格三段式**语义版本，如 `1.0.0`（`1.0` 不合法） |
| `name` | ✅ | 非空展示名 |
| `description` | ✅ | 非空描述 |
| `author` | ✅ | 作者信息（本仓库第一方插件用 `{name, url}`） |
| `license` | ✅ | 非空 |
| `urls` | ✅ | 相关链接（本仓库第一方插件填 `repository` / `homepage` / `documentation` / `issues`） |
| `host_application` | ✅ | `{min_version, max_version}`，**闭区间**；`min_version` 不能大于 `max_version` |
| `sdk` | ✅ | 同上，约束插件 SDK 版本区间 |
| `capabilities` | ✅ | 能力声明列表，不能为空项、不能重复 |
| `i18n` | ✅ | `{default_locale, supported_locales}`（第一方插件均为 `zh-CN`） |
| `plugin_type` | ✅（有默认） | 默认 `extension`；兼容别名 `type` |
| `dependencies` | ⭕ | 依赖声明列表（见下） |
| `llm_providers` | ⭕ | 静态声明的 LLM Provider |
| `display` | ⭕ | 展示元信息，目前只有 `icon`（本地图标仅支持 jpg/jpeg/png/svg/webp） |
| `changelog` | ⭕ | `http(s)` URL，或**插件目录内**的相对 `.md` 路径；禁止绝对路径、`..` 与前导斜杠 |

依赖条目（`dependencies[]`）第一方插件只用到 `{"type": "python_package", "name": ..., "version_spec": ...}`，例如 `marisa_voice` / `marisa_cv` 都声明 `httpx >=0.27.0`。

## 3. `config.toml` 约定

- 必须有 `[plugin]` 配置节，且 `plugin.config_version` **必填**。缺失时宿主直接报错：
  - `插件配置文件缺少 [plugin] 配置节，且必须提供 plugin.config_version 版本号`
  - `插件配置文件缺少 plugin.config_version 版本号，当前版本策略不再兼容无版本配置`
  （`src/plugin_runtime/runner/runner_main.py`）
- `plugin.config_version` 参与配置版本比较（旧配置会走升级/补齐流程），所以**改配置结构就要改这个版本号**。
- 其他键由插件自己定义，但本仓库惯例是**用 Pydantic 配置模型声明**（`PluginConfigBase` 子类），再在插件类上设 `config_model = XxxConfig`。这样 WebUI 插件配置页能自动渲染字段、分组、默认值、取值范围（`Field(..., description=..., ge=..., le=...)`）与依赖显隐（`json_schema_extra={"depends_on": ..., "depends_value": ...}`）。
- 配置热更新走 `async def on_config_update(self, scope, config_data, version)`；**只改 `config.toml` 里会被读取的字段才即时生效**，写进工具描述一类的字段需要热重载插件（`marisa_voice` 的 `[voice] use_hint/length_hint` 就是这种）。
- 第一方插件统一用 `[plugin] enabled` 控制启停。

## 4. `plugin.py` 约定与声明式接口

入口是模块级工厂函数 `create_plugin()`，返回插件类实例（骨架为 `MaiBotPlugin` 子类）。四个第一方插件都遵循这个约定。

插件用**装饰器声明**对外接口，宿主负责匹配与派发：

| 装饰器 | 给谁用 | 要点 |
|---|---|---|
| `@Tool(name, description, parameters=[ToolParameterInfo(...)], visibility=...)` | **LLM**（function calling） | 参数用 `ToolParamType.STRING / INTEGER / BOOLEAN` 声明；`required` 控制必填；`visibility="visible"` 表示对模型可见 |
| `@Command(id, description, pattern=..., permission=...)` | **用户斜杠指令** | `pattern` 是宿主用于匹配的正则（通常配合插件内 `cmd_pattern()` 拼「@/前缀/去〔〕」）；`permission="operator"` 表示**只有管理员/主人**可用，由宿主按 `plugin.permission` 名单先行拦截 |
| `@API(name, description, version, public)` | **其他插件**（跨插件调用） | `public=True` 时可通过 `ctx.api.call("<plugin-id>.<api-name>", ...)` 调用 |
| `@HookHandler(hook, name, mode=HookMode.BLOCKING, order=HookOrder.EARLY)` | **宿主消息链** | 可返回 `{"action": "abort"}` 静默截断，或 `{"action": "continue", "modified_kwargs": ...}` 放行；`chat.receive.before_process` 是最常用的门控点 |
| `@MessageGateway(name, route_type, platform, protocol, description)` | **平台接入（adapter 专用）** | 声明一条外部平台通道；配合 `ctx.gateway.route_message(...)` 把外部消息注入主链、`ctx.gateway.update_state(...)` 上报连接状态 |
| `@HomeCard(id, title, description, content, link_url, link_label, width, order)` | **WebUI 首页卡片** | 在 WebUI 首页展示入口卡片（`content` 支持 `markdown` / `list` / `actions` 等块） |

工具返回值约定：字典里 `{"success": bool, "content": str}`；`stop_after_execution: True` 表示**这一轮规划到此结束**（`marisa_voice` 的 `marisa_say` 用它实现「要么打字、要么说话」）。

## 5. `plugin_type` 与运行时分组

- `plugin_type` 目前仓库里出现三种取值：`extension`（`marisa_cv`、`marisa_voice`）、`normal`（`marisa_gate`）、`adapter`（`marisa_desktop`）。
- 运行时**只对 `adapter` 特殊处理**：宿主内部常量 `_ADAPTER_PLUGIN_TYPE = "adapter"`（`src/plugin_runtime/host/supervisor.py`），且平台接入链路会检查 `plugin_type == "adapter"`（`src/webui/routers/chat/routes.py`）。
- 应用启动时使用两种过滤模式（`src/plugin_runtime/integration.py`）：
  - `trusted_or_adapter`：内置目录 + 所有 `adapter` 插件，跑在「builtin」supervisor 里（内置目录是可信目录）；
  - `not_adapter`：其余第三方插件，跑在「third_party」supervisor 里。
- 因此在本仓库的实际加载路径下，`extension` 与 `normal` **没有行为差异**；只有显式传入精确的 `plugin_type_filter`（例如 `extension`）时才会区分。
- **写 adapter 插件要知道的**：adapter 是「把外部平台接进来」的类型，需要一个 `platform` 标识（`marisa_desktop` 用 `platform="desktop"`、`route_type="duplex"`），并自己实现本地传输（`marisa_desktop` 用 aiohttp 起本地 WebSocket 服务）。

## 6. 第一方插件：marisa_cv（魔理沙识图）

| 项 | 值 |
|---|---|
| manifest `id` | `marisa-team.marisa-cv` |
| `version` | `1.0.0` |
| `plugin_type` | `extension` |
| `capabilities` | `send.text`、`message.get_recent`、`config.get` |
| `dependencies` | `httpx >=0.27.0` |
| 兼容区间 | host `1.2.0 ~ 1.2.99`，sdk `2.5.0 ~ 2.99.99` |

**指令**

| 指令 id | 触发词 | 权限 | 行为 |
|---|---|---|---|
| `marisa_cv_identify` | `/识图`、`/认图`、`/认人`、`/看图` | 所有人 | 先发指令进入待图状态，再发图片；`[cv] pending_ttl_sec`（默认 120 秒）内没等到图就取消；同一人两次有 `[cv] cooldown_sec`（默认 30 秒）冷却 |

**工具**

| 工具名 | 参数 | 说明 |
|---|---|---|
| `marisa_identify_character` | `image_path`（可选） | 本地识别图片里的东方角色。`image_path` 留空时通过 `message.get_recent` 能力取当前会话最近一张图（`limit=10`，`include_binary_data=True`） |

**Hook / 首页卡片**

- `@HookHandler("chat.receive.before_process", name="marisa_cv_pending_image", mode=BLOCKING, order=EARLY)`：待图状态下拦截后续图片消息。
- `@HomeCard("marisa_cv_entry", ...)`：WebUI 首页入口卡片。

**依赖的外部服务 / 端口**

- 本地识图推理服务 `plugins/marisa_cv/service/serve.py`，默认 `http://127.0.0.1:9881`（`POST /classify`）。
- 该服务由**外部 GPT-SoVITS runtime 的 `python.exe`** 启动（`plugins/marisa_cv/service/start_cls_api.bat`），`onnxruntime / numpy / PIL / fastapi` 由那份运行时提供，不跑在仓库 `.venv` 里。
- 模型文件（`models/wd_features.onnx`、`head.npz`、`labels.json`、`labels_zh.json`）不入库，用 `plugins/marisa_cv/tools/` 下的脚本重建。详见 `docs/external-deps.md` 第 2 节。

## 7. 第一方插件：marisa_desktop（魔理沙桌面适配器）

| 项 | 值 |
|---|---|
| manifest `id` | `marisa-team.marisa-desktop` |
| `version` | `0.2.0` |
| `plugin_type` | **`adapter`** |
| `capabilities` | `send.text`、`send.custom`、`send.hybrid`、`chat.open_session`、`config.get`、`component.enable`、`component.disable`、`llm.generate`、`llm.transcribe_audio`、`render.html2png`、`api.call` |
| `dependencies` | 无 |
| 兼容区间 | host `1.2.0 ~ 1.2.99`，sdk `2.5.0 ~ 2.99.99` |

**平台接入**

```python
@MessageGateway(
    name="marisa_desktop_gateway",
    route_type="duplex",
    platform="desktop",
    protocol="marisa-desktop",
    description="桌宠本地 WebSocket 双工消息网关",
)
```

- 适配器自己起本地 WS 服务：`[server] host = "127.0.0.1"`、`port = 8400`（`plugins/marisa_desktop/config.toml`）；出站把主链消息推给已连接的桌宠客户端，入站用 `ctx.gateway.route_message(gateway_name=..., ...)` 注入主链。
- `[identity]`：`owner_user_id`、`owner_nickname`、`bot_account_id`（默认 `desktop-0`）。
- `[launcher]`：`auto_start_pet`、`pet_mode`（默认 `dev`）、`pet_exe`（`run_pet.py`）、`dev_python`（`.venv\Scripts\python.exe`）、`dev_workdir`、`check_interval_sec`（10.0）。
- `[patrol]`：主动巡检开关与节奏（`interval_seconds = 900`、`vision_model = "vlm"`、`text_model = "utils"`、`quiet_start_hour = 23`、`quiet_end_hour = 8`、`require_pet_online = true`）。

**工具（16 个，供 LLM 调用）**

| 工具名 | 参数 | 说明 |
|---|---|---|
| `marisa_play_music` | `song`（必填） | 点歌：主人在本机网易云播放，其他成员点歌则发群语音 |
| `marisa_recommend_music` | — | 播放今日魔理推荐歌单 |
| `marisa_music_control` | `action`（`pause`/`play`/`next`/`prev`） | 音乐控制 |
| `marisa_ncm_login` | — | 生成网易云扫码登录二维码（不打开客户端） |
| `marisa_open_app` | `app`（必填） | 打开本机应用或网址 |
| `marisa_volume` | `action`（`volume_up`/`volume_down`/`volume_mute`） | 系统音量 |
| `marisa_read_clipboard` | — | 读剪贴板 |
| `marisa_reminder_add` | 见工具描述 | 设置提醒 |
| `marisa_reminder_list` | — | 查看提醒列表 |
| `marisa_reminder_delete` | `index`（必填） | 删除一条提醒 |
| `marisa_favor` | — | 查询好感度/羁绊 |
| `marisa_uptime` | — | 本次开机时长 |
| `marisa_balance` | — | DeepSeek 账户余额 |
| `marisa_lock_screen` | — | 锁定屏幕 |
| `marisa_screenshot` | — | 截屏并把图片发给主人 |
| `marisa_weather` | `city`（必填） | 指定城市实时天气（没给城市时要求先反问） |

多数本机操作用 `_caller_authorized(platform, user_id)` 做主人校验。

**指令（20 个）**

| 指令 id | 触发词 | 权限 |
|---|---|---|
| `marisa_help` | `/help`、`/帮助`、`/指令` | 所有人 |
| `marisa_weather` | `/天气 <城市>` | operator |
| `marisa_music_play` | `/点歌`、`/播放歌曲` | 所有人 |
| `marisa_music_recommend` | `/推荐歌曲`、`/今日推荐`、`/来点音乐` | operator |
| `marisa_music_play_toggle` | `/播放`、`/继续` | operator |
| `marisa_music_pause` | `/暂停` | operator |
| `marisa_music_next` | `/下一首`、`/下首`、`/切歌` | operator |
| `marisa_music_prev` | `/上一首`、`/上首` | operator |
| `marisa_music_volume` | `/音量` | operator |
| `marisa_playlist_like` | `/我的歌单`、`/我喜欢的音乐` | operator |
| `marisa_open_app` | `/开软件`、`/打开` | operator |
| `marisa_clipboard` | `/剪贴板` | operator |
| `marisa_reminder_add` | `/提醒` | operator |
| `marisa_reminder_list` | `/提醒列表` | operator |
| `marisa_reminder_del` | `/提醒删除` | operator |
| `marisa_favor` | `/好感度` | operator |
| `marisa_balance` | `/余额` | operator |
| `marisa_uptime` | `/开机时长`、`/时长` | operator |
| `marisa_lock` | `/锁屏`、`/锁定` | operator |
| `d_ncm_login` | `/网易云登录`、`/扫码登录` | operator |

**跨插件 API**

| API | version | public | 说明 |
|---|---|---|---|
| `push_pet_frame` | `1` | ✅ | 向所有已连接的桌宠客户端广播一帧 JSON，返回送达客户端数。`marisa_voice` 就用它推「语音服务没起来」的气泡 |

**依赖的外部服务 / 端口**

- 本地 WS 网关 `ws://127.0.0.1:8400`（桌宠客户端 `plugins/marisa_desktop/core/maibot_client.py` 默认也连这里）。
- GPT-SoVITS `http://127.0.0.1:9880`（桌宠语音伴读，`core/tts.py`）。
- 网易云音乐本机客户端（`orpheus://` 协议，`native/ncm_client.py`）。
- `https://api.deepseek.com/user/balance`（余额）、`https://wttr.in/...`（天气）。

## 8. 第一方插件：marisa_gate（魔理沙守门人）

| 项 | 值 |
|---|---|
| manifest `id` | `marisa-team.marisa-gate` |
| `version` | `0.2.0` |
| `plugin_type` | `normal` |
| `capabilities` | `send.text`、`send.hybrid`、`config.get`、`chat.open_session`、`render.html2png`、`api.call` |
| `dependencies` | 无 |
| 兼容区间 | host `1.2.0 ~ 1.2.99`，sdk `2.5.0 ~ 2.99.99` |

**触发门控（Hook）**

`@HookHandler("chat.receive.before_process", name="marisa_trigger_gate", mode=BLOCKING, order=EARLY)`：消息进主链前判定是否放行 ——
- 私聊：只有主人放行（并给消息打上「主人」标记），陌生人私聊直接 `abort`；
- 群聊：所有群统一要求 @ 或明确提到魔理沙（含主人，防止无差别抢话）；`[rules] group_mode` 为 `whitelist` 时还要命中 `[rules] groups` 白名单；不满足即静默 `abort`；
- 斜杠指令走确定性路由：群内仍需 @/提到，且 `permission="operator"` 的指令只对主人生效；
- 非 `qq` 平台默认放行（`desktop` 通道只对「带方案编号的清理回执」做确定性拦截）；
- 主人名单以宿主全局 `plugin.permission` 的 `qq:` 条目为优先来源，为空时回退插件自己的 `[rules] owners`；
- 主人对终端待办的「执行 / 取消」回执、对清理方案的带编号回执都在这一层被优先拦截处理。

**指令（5 个，全部 `permission="operator"`）**

| 指令 id | 触发词 | 说明 |
|---|---|---|
| `marisa_screenshot` | `/截图`、`/截屏` | 截屏并回图 |
| `marisa_disk_report` | `/体检 [大文件\|重复\|临时]` | 电脑体检（只读扫描） |
| `marisa_project_audit` | `/项目`、`/项目审查` | 代码仓库占用与 Git 状态审查（只读） |
| `marisa_cleanup` | `/清理` | 生成清理方案（临时缓存 + 桌面整理），确认后才执行 |
| `marisa_cleanup_undo` | `/撤销`、`/清理撤销` | 撤销上一次清理执行 |

**工具（6 个）**

| 工具名 | 参数 | 说明 |
|---|---|---|
| `marisa_shell` | `command`（必填）、`workdir`（可选，默认 `D:\MarisaBOT`） | 在主人电脑执行 PowerShell/git 等命令；只读命令直跑，其余先提议等「执行」，危险命令直接拒绝 |
| `marisa_shutdown` | `seconds`、`cancel` | 关机/重启（带倒计时可取消）；仅在主人明确说「关机/重启」时可用 |
| `marisa_opencode` | `task`（必填）、`workdir`、`read_only` | 通过 `opencode` 执行代码/仓库任务；会话内禁止 commit/push |
| `marisa_disk_report` | `topic`（可选：大文件 / 重复 / 临时） | 只读体检 |
| `marisa_project_audit` | — | 只读审查代码仓库 |
| `marisa_cleanup_plan` | — | 生成清理方案并推送给主人确认，只生成不执行 |

**跨插件 API**

| API | version | public | 说明 |
|---|---|---|---|
| `cleanup_confirm` | `1` | ✅ | 接收桌宠弹窗对清理方案的确认/取消，与 QQ 侧确认共享同一份待确认方案 |

**配置（`plugins/marisa_gate/config.toml`）**

- `[rules]`：`group_mode`、`owners`、`groups`、`cooldown_sec`、`seed_favorability`、`fav_chat_cap_per_hour`、`fav_tool_cap_per_hour`、`deepseek_api_key`。
- `[terminal]`：`enabled`、`confirm_required`、`workdir_default`（`D:\MarisaBOT`）、`timeout_shell_sec`（60.0）、`timeout_opencode_sec`（900.0）、`output_cap_chars`（1500）、`opencode_cmd`（`opencode`）。
- `[cleanup]`：`roots`（清理扫描根白名单，留空回退桌面/下载/文档）、`project_roots`、`large_file_mb`（200）、`duplicate_mb`（10）、`max_plan_items`（200）、`allow_permanent_delete`（false）、`plan_ttl_sec`（600）、`desktop_organize`（true）、`desktop_min_age_hours`（24）。

**依赖的外部服务 / 端口**

- 本机 PowerShell、关机权限、`PATH` 里的 `opencode` 命令；工作目录默认 `D:\MarisaBOT`。
- 不监听端口。

## 9. 第一方插件：marisa_voice（魔理沙语音）

| 项 | 值 |
|---|---|
| manifest `id` | `marisa.voice` |
| `version` | `1.2.0` |
| `plugin_type` | `extension` |
| `capabilities` | `send.custom`、`send.text`、`config.get`、`api.call` |
| `dependencies` | `httpx >=0.27.0` |
| 兼容区间 | host `1.2.0 ~ 1.2.99`，sdk `2.5.0 ~ 2.99.99` |

**工具**

| 工具名 | 参数 | 说明 |
|---|---|---|
| `marisa_say` | `text`（必填） | 用当前音色把一句话说出去。长度必须在 `[voice] min_chars ~ max_chars`（默认 3~60）之间；成功后返回 `stop_after_execution: True`，**本轮不再补文字**；失败一律引导模型「直接用文字回复」 |

设计意图（插件文档字符串）：让**模型自己决定**何时开口，而不是插件偷偷把文字改写成语音 —— 否则模型在历史里看不到自己发过语音，也无从判断何时该开口。

**指令**

| 指令 id | 触发词 | 权限 | 说明 |
|---|---|---|---|
| `marisa_voice_say` | `/语音`、`/说话`、`/朗读` | 所有人 | 手动验证链路用；按调用者做 `[voice] cooldown_sec`（默认 30 秒）冷却，防止刷合成队列 |

**首页卡片与配置热更新**

- `@HomeCard("marisa_voice_entry", ...)`：WebUI 首页入口，链接 `/plugin-config?plugin=marisa.voice`。
- `on_config_update(scope, config_data, version)`：配置更新后重读语音开关与发送方式。

**配置要点**

- `[voice]`：`enabled`、`min_chars`（3）、`max_chars`（60）、`send_mode`（`base64` 默认，内联音频、协议端无需读本机文件且会写进对话历史；`file` 只发本地路径，仅当协议端与本机同文件系统时可用）、`cooldown_sec`（30.0）、`use_hint` / `length_hint`（拼进工具描述，改后需热重载）。
- `[service]`：`api_url`（`http://127.0.0.1:9880`）、`ref_audio_path`、`prompt_text`、`prompt_lang`、`text_lang`、`speed_factor`、`synthesize_timeout_sec`（45.0）、`auto_start_api`、`api_start_script`、`api_start_timeout_sec`（180.0）、`prewarm`。

**依赖的外部服务 / 端口**

- 本地 GPT-SoVITS `http://127.0.0.1:9880`（`POST /tts`；存活探测 `GET /`）。
- 跨插件调用 `marisa-team.marisa-desktop` 的 `push_pet_frame` API：语音服务预热失败时给桌宠推气泡（`capabilities` 里的 `api.call` 就是为此）。
- 输出缓存 `plugins/marisa_voice/data/tts/`；启动日志与跨进程锁 `data/voice_api.log`、`data/voice_api.start.lock`。

## 10. 四个插件的外部依赖对照

| 插件 | 本地端口 | 外部网络 | 本机程序 / 协议 |
|---|---|---|---|
| `marisa_cv` | 9881（识图服务，由它调用） | — | 依赖外部 GPT-SoVITS runtime 的 Python 解释器 |
| `marisa_desktop` | 8400（自带 WS 网关） | DeepSeek 余额、wttr.in、music.163.com | 网易云客户端（`orpheus://`）、GPT-SoVITS 9880（语音伴读） |
| `marisa_gate` | — | — | PowerShell、`opencode` CLI |
| `marisa_voice` | 9880（调用方，不监听） | — | GPT-SoVITS 服务与 runtime |

各依赖的默认路径、缺失表现与配置方法见 `docs/external-deps.md`。

## 11. 未确认项

- `marisa_gate` 的 manifest 描述里提到「天气·好感·截图·锁屏等主人命令」，但 `plugin.py` 里实际只有本节列出的 5 个 `@Command`；天气/好感/锁屏等能力由 `marisa_desktop` 提供（见第 7 节）。`marisa_gate` 是否通过 Hook 内的自然语言分支额外处理这些意图，**本文未逐条确认**。
- `@Tool` 的 `visibility` 字段除 `"visible"` 外的其他取值语义，本文未在宿主侧逐一确认。
- `plugin_type` 除 `extension` / `normal` / `adapter` 外是否还有其他被认可的取值，宿主 manifest 校验**不限制取值**（`plugin_type: str`），因此无法从代码给出封闭列表；本文只列出仓库中实际出现的三种。
