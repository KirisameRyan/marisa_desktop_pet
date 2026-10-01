# plugins 插件清单

## 这个文件为什么存在

根目录 `.gitignore` 里 `/plugins/*` 是**整体忽略**的。这是本仓库的既定约定：第三方插件各自是独立仓库，不进主仓库。

但约定在实际使用中失效了——有 4 个插件是主仓库自己维护的，它们既没有独立远端，也没被主仓库跟踪，代码只以散文件形式存在于被忽略的目录里。后果是：**新 clone 一份仓库，桌宠直接起不来，QQ 侧与识图也全部缺失**。

因此从 1.2.4 起调整如下，本文件是配套登记：

- 主仓库自己维护、缺了就起不来的**第一方插件** → 反白名单入库，随主仓库一起恢复；
- **第三方插件** → 仍按约定不进主仓库，但要能在本文件里查到来源，照着 clone 回来。

## 第一方插件（随主仓库入库）

| 目录 | plugin id | 版本 | plugin_type | 作用 | 缺了会怎样 |
|---|---|---|---|---|---|
| `marisa_desktop/` | `marisa-team.marisa-desktop` | 0.2.0 | adapter | 桌宠 Qt 客户端 ↔ MaiBot 的 WebSocket 适配器：上行以 `desktop` 平台、主人身份注入主链，下行把回复推回桌宠显示与播音 | **桌宠直接起不来**：`run_pet.py` 在 import 阶段就硬加载 `plugins/marisa_desktop/core/`，缺 `core/__init__.py` 会 `sys.path` 抢占后崩在导入期 |
| `marisa_gate/` | `marisa-team.marisa-gate` | 0.2.0 | normal | 入口门控（私聊仅主人放行、群消息需 @ 或叫出「魔理沙/Marisa」）、给消息打说话人标记、跨端好感度单一事实源、`/截图`、远程终端家族（`/终端`、`/关闭电脑`、`/取消关机`） | QQ 侧门控失效（未授权消息不再被吞掉），`/截图` 与远程终端指令全部消失 |
| `marisa_cv/` | `marisa-team.marisa-cv` | 1.0.0 | extension | 本地东方角色识图：工具 `marisa_identify_character` + 指令 `/识图`，由本地分类服务（`service/serve.py`，端口 9881）完成 | `/识图` 与主动识图工具不可用 |
| `marisa_voice/` | `marisa.voice` | 1.2.0 | extension | 注册 `marisa_say` 工具，让模型自己决定何时用本地 GPT-SoVITS 合成语音说出口 | 语音输出能力消失（纯文字聊天不受影响） |
| `marisa_dsh/` | `marisa.dsh` | 1.0.0 | extension | DSH 桥：主人用 QQ 远程让 DeepSeek Harness 跑任务（`/dsh <任务>`、`/dsh 继续`），结果回传 | 无法通过 QQ 远程驱动 DSH（其余功能不受影响） |

`hello_world_plugin/` 是官方示例插件，保留入库，运行时可不要。

### 第一方插件里不进版本控制的大文件

`plugins/marisa_cv/.gitignore` 已排除以下内容（合计约 1.08 GB），它们**不该进版本库**，用仓库里的脚本重建：

```
models/backbone/            # WD-ConvNeXt v3 骨干（ONNX + safetensors）
models/wd_features.onnx     # 导出的特征模型
models/head.npz             # 134 类逻辑回归头
models/backbone_download.log
```

重建路径（脚本都在 `plugins/marisa_cv/tools/`）：

1. `download_backbone.py` —— 拉取骨干权重；
2. `export_features.py` —— 导出 `wd_features.onnx`；
3. `convert_head.py` —— 生成 `head.npz`；
4. `validate_features.py` —— 用数据集校验特征一致性。

`models/labels.json`、`models/labels_zh.json`、`models/feature_variant.json` 体积很小，已入库。

识图服务使用的解释器不是本项目 `.venv`，而是外部 GPT-SoVITS runtime，详见 `docs/external-deps.md`。

## 第三方插件（各自的独立仓库，不随主仓库入库）

| 目录 | plugin id | 版本 | 远端 | 缺了会怎样 |
|---|---|---|---|---|
| `MaiBot-Napcat-Adapter/` | `maibot-team.napcat-adapter` | 1.4.0 | https://github.com/Mai-with-u/MaiBot-Napcat-Adapter.git | **QQ 侧完全不工作**（OneBot v11 正向 WebSocket 客户端）。注意：2026-09-30 起它连接的**协议端**已由 NapCat 换成 PMHQ + LLBot（见 `deploy/qq-pmhq/README.md`），适配器自身未改动，仍连 `127.0.0.1:3001` |
| `xuqian13_tts-voice-plugin/` | `xuqian13.tts-voice-plugin` | 3.3.0 | https://github.com/xuqian13/tts_voice_plugin.git | 另一套 TTS 语音插件，可选 |
| `internetsb_maizone/` | `internetsb.maizone` | 3.0.2 | https://github.com/internetsb/Maizone.git | 可选扩展 |
| `github_pan-ice_music/` | `github.pan-ice.music` | 1.4.6 | https://github.com/pan-ice/maibot-music.git | 可选扩展（点歌） |

其中三个的 `origin` 目前带 `gitproxy.mrhjx.cn` 代理前缀，直接 clone 时可按需替换成原始 GitHub 地址。

`data/` 是插件运行时数据目录，不进版本控制。

## 新机器 / 新 clone 的恢复步骤

1. `git clone` 主仓库 —— 4 个第一方插件已经包含在内；
2. 按上表把第三方插件 clone 到 `plugins/<目录名>`（目录名必须一致，宿主按目录名加载）；
3. 安装依赖：`uv sync`（依赖以 `pyproject.toml` 为准，`requirements.txt` 同步维护）；
4. 配好外部服务：GPT-SoVITS 语音服务、识图模型与 runtime、QQ 协议端，见 `docs/external-deps.md`；
5. 启动：`启动一体化.bat` 或 `start_all.ps1`。

## 契约回归测试

`pytests/plugins_test/test_marisa_plugin_contracts.py` 会守住上面登记的第一方插件：

- `_manifest.json` 是否为 v2 且 `id` / `version` / `plugin_type` / `host_application` 齐全；
- `config.toml` 是否声明 `[plugin].config_version`（缺了宿主加载插件会直接报错）；
- 关键文件是否**真的入了 git**——磁盘上有、git 里没有，就等于新 clone 缺文件；
- `run_pet.py` 的引导段是否仍然把插件 `core` 绑定进 `sys.modules`，并把标准输出固定为 UTF-8。

## 已知待办

- 第三方插件目前是**没有 `.gitmodules` 的裸嵌套仓库**，主仓库无法一键把它们拉起来。要彻底恢复可复现性，建议改成 submodule，或在启动脚本里声明"缺哪个、从哪个 origin 拉"。本轮未动这些仓库的工作树。
