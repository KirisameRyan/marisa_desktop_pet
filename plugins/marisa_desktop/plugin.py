# -*- coding: utf-8 -*-
"""魔理沙桌面适配器：把桌宠 Qt 客户端接入 MaiBot（P2 v0.1）

架构：桌宠(客户端) --本地 WS--> 本适配器(duplex MessageGateway) --platform_io--> MaiBot 主链
- 上行：桌宠聊天文本 -> 以 desktop 平台、主人身份注入 Host，走 Maisaka 主脑
  （与 QQ 同一人格/同一大脑，A_memorix 以人物为跨端记忆骨架）
- 下行：主脑/命令的回复经网关回调推回给桌宠显示与播音
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx

from maibot_sdk import API, Command, Field, MaiBotPlugin, MessageGateway, PluginConfigBase, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

_PLUGIN_DIR = Path(__file__).resolve().parent
_NATIVE_DIR = _PLUGIN_DIR / "native"
if str(_NATIVE_DIR) not in sys.path:
    sys.path.insert(0, str(_NATIVE_DIR))

# 桌宠启动输出：拉起的进程必须留下现场（此前 Popen 不重定向，"秒退"类问题完全没法查）
_PET_LOG = _PLUGIN_DIR.parent.parent / "data" / "pet.log"


def _open_pet_log() -> Any:
    """打开桌宠输出日志（追加）；打不开时退回丢弃输出。"""

    try:
        _PET_LOG.parent.mkdir(parents=True, exist_ok=True)
        return open(_PET_LOG, "ab")
    except OSError:
        return subprocess.DEVNULL


def _load_plugin_module(module_stem: str) -> Any:
    """按文件路径加载插件目录下的模块。

    插件目录下还有 ``core``、``pet`` 等通用名目录，直接把它加入 sys.path 会
    遮蔽宿主同名模块，因此这里显式按文件位置加载。
    """

    module_name = f"marisa_desktop_{module_stem}"
    if (cached := sys.modules.get(module_name)) is not None:
        return cached
    spec = importlib.util.spec_from_file_location(module_name, _PLUGIN_DIR / f"{module_stem}.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载插件模块: {_PLUGIN_DIR / (module_stem + '.py')}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_help_menu = _load_plugin_module("help_menu")
collect_command_entries = _help_menu.collect_command_entries
render_html = _help_menu.render_html
render_text = _help_menu.render_text

_patrol_module = _load_plugin_module("patrol")
PatrolDirector = _patrol_module.PatrolDirector

try:
    from actions import ACTIONS
except Exception as _e:
    print(f"[marisa_desktop] load actions error: {_e}")
    ACTIONS = None

from windows import foreground_app_info  # noqa: E402  （native 目录已在 sys.path 上）

try:
    from aiohttp import WSMsgType, web

    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover
    AIOHTTP_AVAILABLE = False

GATEWAY_NAME = "marisa_desktop_gateway"

_AT_MARK = r"(?:\s*〔[^〕]*〕\s*)?(?:\s*(?:\[At:[^\]]*\]|@[\w\u4e00-\u9fa5]+)\s*)*"
_TRIGGER = r"(?:魔理沙[，,、\s]*)?"


def cmd_pattern(inner: str) -> str:
    """统一命令正则前缀：容忍 @At 与「魔理沙」前缀。"""
    return "^" + _AT_MARK + _TRIGGER + "(?P<raw>" + inner + ")$"


_RE_AT = re.compile(r"\[At:[^\]]*\]")
_RE_LEADING_AT = re.compile(r"^\s*\[At:[^\]]*\]\s*")

ACTION_TIMEOUT_SEC = 25.0

# ---- QQ 原生命令：桌宠能力注册为 /斜杠命令，默认仅授权管理员（operator） ----

OWNER_DENY_TEXT = "这个操作得让主人来だぜ～（桌宠指令仅授权主人可用）"

# /help 菜单的补充说明（命令清单本身由 help_menu 从组件声明动态生成）
HELP_FOOTER_NOTE = (
    "需要确认的指令会给『执行 P编号』口令，回复口令即生效。\n"
    "/指令默认仅授权主人可用；想让某个群也能用某条指令，可在 WebUI『机器人设置→命令权限』"
    "给对应指令添加放行群（allow_chats）だぜ。"
)


class DesktopMainConfig(PluginConfigBase):
    """插件开关。"""

    __ui_label__ = "插件"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用桌面适配器")
    config_version: str = Field(default="0.1.0", description="配置版本")


class DesktopServerConfig(PluginConfigBase):
    """本地服务。"""

    __ui_label__ = "本地服务"
    __ui_order__ = 1

    host: str = Field(default="127.0.0.1", description="WS 监听地址")
    port: int = Field(default=8400, description="WS 监听端口")


class DesktopIdentityConfig(PluginConfigBase):
    """身份与会话。"""

    __ui_label__ = "身份与会话"
    __ui_order__ = 2

    owner_user_id: str = Field(default="3895418161", description="桌面端对应的主人用户 ID")
    owner_nickname: str = Field(default="主人", description="桌面端主人昵称（进入记忆的称呼）")
    bot_account_id: str = Field(default="desktop-0", description="desktop 平台账号 ID（路由用）")


class LauncherConfig(PluginConfigBase):
    """桌宠守护（拓扑 B：MaiBot 拉起桌宠并自动复活）。"""

    __ui_label__ = "桌宠守护"
    __ui_order__ = 3

    auto_start_pet: bool = Field(default=True, description="是否由 MaiBot 守护拉起桌宠")
    pet_mode: str = Field(default="auto", description="运行形态：auto/exe/dev（auto 优先 exe）")
    pet_exe: str = Field(
        default=r"D:\MarisaBOT\dist\魔理沙桌宠\魔理沙桌宠.exe",
        description="打包版桌宠 EXE 路径",
    )
    dev_python: str = Field(
        default=r"C:\Users\49713\AppData\Local\Programs\Python\Python312\python.exe",
        description="开发版 Python 解释器（含 PyQt6）",
    )
    dev_script: str = Field(default=r"D:\MarisaBOT\run_pet.py", description="开发版桌宠入口脚本")
    dev_workdir: str = Field(default=r"D:\MarisaBOT", description="开发版工作目录")
    check_interval_sec: float = Field(default=10.0, description="存活检查间隔（秒）")


class PatrolConfig(PluginConfigBase):
    """桌面主动巡检：定时看一眼屏幕，主动找主人搭话。"""

    __ui_label__ = "主动巡检"
    __ui_order__ = 4

    enabled: bool = Field(default=True, description="是否开启桌面定时巡检（桌宠主动搭话）")
    interval_seconds: int = Field(default=900, description="巡检间隔（秒），建议 300~1800")
    vision_model: str = Field(default="vlm", description="负责看屏幕的视觉模型任务名")
    text_model: str = Field(default="utils", description="负责组织台词的文本模型任务名")
    quiet_start_hour: int = Field(default=23, description="免打扰起始小时（含），0~23")
    quiet_end_hour: int = Field(default=8, description="免打扰结束小时（不含），0~23")
    require_pet_online: bool = Field(default=True, description="桌宠未连接时不巡检（避免自说自话）")


class MarisaDesktopConfig(PluginConfigBase):
    """魔理沙桌面适配器配置。"""

    plugin: DesktopMainConfig = Field(default_factory=DesktopMainConfig)
    server: DesktopServerConfig = Field(default_factory=DesktopServerConfig)
    identity: DesktopIdentityConfig = Field(default_factory=DesktopIdentityConfig)
    launcher: LauncherConfig = Field(default_factory=LauncherConfig)
    patrol: PatrolConfig = Field(default_factory=PatrolConfig)


def _to_text(message: Dict[str, Any]) -> str:
    """从出站 MessageDict 提取纯文本。"""
    raw = message.get("raw_message")
    text_parts: List[str] = []
    if isinstance(raw, list):
        for seg in raw:
            if not isinstance(seg, dict):
                continue
            if seg.get("type") == "text":
                data = seg.get("data") or seg.get("content") or {}
                if isinstance(data, dict):
                    text_parts.append(str(data.get("text") or ""))
                else:
                    text_parts.append(str(data))
            elif seg.get("type") == "image":
                text_parts.append("[图片]")
    if text_parts:
        return "\n".join(x for x in text_parts if x)
    return str(message.get("processed_plain_text") or message.get("display_message") or "").strip()


class MarisaDesktopPlugin(MaiBotPlugin):
    """魔理沙桌面适配器插件。"""

    config_model = MarisaDesktopConfig

    def __init__(self) -> None:
        super().__init__()
        self._clients: Set[Any] = set()
        self._server_task: Optional[asyncio.Task] = None
        self._app: Any = None
        self._runner: Any = None
        self._launcher_task: Optional[asyncio.Task] = None
        self._pet_proc: Optional[subprocess.Popen] = None
        self._launcher_checked_initial: bool = False
        self._patrol_task: Optional[asyncio.Task] = None
        self._patrol: Any = None

    # ───────────────────────── 生命周期 ─────────────────────────

    async def on_load(self) -> None:
        """启动本地 WS 服务。"""
        if not AIOHTTP_AVAILABLE:
            self.ctx.logger.info("[marisa_desktop] aiohttp 不可用，桌面适配器无法启动")
            return
        if not self.config.plugin.enabled:
            return
        app = web.Application()
        app.router.add_get("/", self._handle_ws)
        self._app = app
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        if not await self._start_site_with_retry():
            return
        self.ctx.logger.info(
            f"[marisa_desktop] WS 服务已启动 ws://{self.config.server.host}:{self.config.server.port}"
        )
        try:
            accepted = await self.ctx.gateway.update_state(
                gateway_name=GATEWAY_NAME,
                ready=True,
                platform="desktop",
                account_id=str(self.config.identity.bot_account_id),
                scope="",
                metadata={"ws_url": f"ws://{self.config.server.host}:{self.config.server.port}"},
            )
            self.ctx.logger.info("[marisa_desktop] 网关就绪状态上报 accepted=%s", accepted)
        except Exception as exc:
            self.ctx.logger.info("[marisa_desktop] 网关状态上报失败: %s", exc)

        if self.config.launcher.auto_start_pet:
            self._launcher_task = asyncio.create_task(self._launcher_loop())
            self.ctx.logger.info("[marisa_desktop] 桌宠守护已启动")

        if self.config.patrol.enabled:
            self._patrol = PatrolDirector(llm_call=self._patrol_llm, logger=self.ctx.logger)
            self._patrol_task = asyncio.create_task(self._patrol_loop())
            self.ctx.logger.info(
                "[marisa_desktop] 主动巡检已启动，间隔 %s 秒",
                self.config.patrol.interval_seconds,
            )

    async def _start_site_with_retry(self, attempts: int = 6, delay: float = 2.0) -> bool:
        """启动网关监听，端口被上一个实例短暂占用时退避重试。

        插件热重载时旧进程可能还没释放 8400，直接绑定会失败并让插件停在
        「加载成功但服务没起来」的半死状态；这里退避重试后仍失败才放弃，
        并把原因写进日志。
        """

        port = int(self.config.server.port)
        for attempt in range(1, attempts + 1):
            site = web.TCPSite(self._runner, self.config.server.host, port)
            try:
                await site.start()
                return True
            except OSError as exc:
                if attempt >= attempts:
                    self.ctx.logger.info(
                        "[marisa_desktop] 网关端口 %s 绑定失败（已重试 %s 次）: %s",
                        port, attempts, exc,
                    )
                    return False
                self.ctx.logger.info(
                    "[marisa_desktop] 网关端口 %s 被占用，%.1fs 后重试（%s/%s）",
                    port, delay, attempt, attempts,
                )
                await asyncio.sleep(delay)
        return False

    # ───────────────────────── 主动巡检 ─────────────────────────

    async def _patrol_llm(self, *, model: str, prompt: Any) -> str:
        """调用宿主模型能力并取出纯文本回复。"""

        result = await self.ctx.llm.generate(prompt=prompt, model=model)
        if not isinstance(result, dict) or not result.get("success"):
            detail = result.get("error") if isinstance(result, dict) else result
            self.ctx.logger.info("[marisa_desktop] 巡检模型调用失败: %s", detail)
            return ""
        return str(result.get("response") or "")

    def _in_quiet_hours(self) -> bool:
        """是否处于免打扰时段（支持跨零点，如 23 点到次日 8 点）。"""

        start = int(self.config.patrol.quiet_start_hour) % 24
        end = int(self.config.patrol.quiet_end_hour) % 24
        if start == end:
            return False
        hour = datetime.now().hour
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    async def _patrol_loop(self) -> None:
        """定时巡检：看一眼屏幕，主动找主人搭话。"""

        interval = max(60, int(self.config.patrol.interval_seconds))
        await asyncio.sleep(min(90.0, interval))  # 等主链与桌宠就绪
        while True:
            try:
                if self.config.patrol.enabled and not self._in_quiet_hours():
                    await self._patrol_tick()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                self.ctx.logger.info("[marisa_desktop] 巡检异常: %s", exc)
            await asyncio.sleep(interval)

    async def _patrol_tick(self) -> None:
        """执行一次巡检并把手记推给桌宠播报。"""

        if self.config.patrol.require_pet_online and not self._clients:
            return
        if ACTIONS is None:
            return

        self._patrol.observe_foreground(foreground_app_info())

        try:
            _path, image_b64 = ACTIONS.capture_screenshot()
        except Exception as exc:
            self.ctx.logger.info("[marisa_desktop] 巡检截图失败: %s", exc)
            return
        if not image_b64:
            return

        observation = await self._patrol_llm(
            model=str(self.config.patrol.vision_model),
            prompt=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "用不超过 80 字描述这张电脑屏幕上主人正在进行的活动"
                                "（例如写代码、看视频、浏览网页、玩游戏、离开座位），"
                                "只描述看到的画面事实，不要评价。"
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                        },
                    ],
                }
            ],
        )
        if not observation:
            return

        favorability = self._fav_value()
        record = await self._patrol.generate(
            observation=observation,
            favorability=favorability,
            rank_name=self._favor_rank(favorability),
            model=str(self.config.patrol.text_model),
        )
        if record is None:
            return
        await self._push_to_pet(record.text)
        self.ctx.logger.info(
            "[marisa_desktop] 巡检搭话（角度=%s 活动=%s）: %s",
            record.angle, record.category, record.text,
        )

    async def _push_to_pet(self, text: str) -> None:
        """把文本推给所有已连接的桌宠客户端（气泡 + 语音）。"""

        payload = {"type": "bot_message", "text": text, "message_id": "", "raw_message": []}
        for ws in list(self._clients):
            try:
                await ws.send_str(json.dumps(payload, ensure_ascii=False))
            except Exception:
                self._clients.discard(ws)

    @API(
        "push_pet_frame",
        description="向所有已连接的桌宠客户端推送一帧 JSON（宿主内部使用）",
        version="1",
        public=True,
    )
    async def api_push_pet_frame(self, frame: Optional[Dict[str, Any]] = None, **kwargs: Any) -> Dict[str, Any]:
        """把任意一帧 JSON 广播给桌宠，返回送达的客户端数量。"""

        del kwargs
        payload = frame if isinstance(frame, dict) else {}
        if not payload:
            return {"delivered": 0, "error": "frame 为空"}
        delivered = 0
        for ws in list(self._clients):
            try:
                await ws.send_str(json.dumps(payload, ensure_ascii=False))
                delivered += 1
            except Exception:
                self._clients.discard(ws)
        return {"delivered": delivered}

    async def on_unload(self) -> None:
        """关闭 WS 服务与桌宠守护。"""
        if self._patrol_task:
            self._patrol_task.cancel()
            try:
                await self._patrol_task
            except Exception:
                pass
            self._patrol_task = None
        if self._launcher_task:
            self._launcher_task.cancel()
            try:
                await self._launcher_task
            except Exception:
                pass
            self._launcher_task = None
        for ws in list(self._clients):
            try:
                await ws.close()
            except Exception:
                pass
        self._clients.clear()
        if self._runner:
            try:
                await self._runner.cleanup()
            except Exception:
                pass
        self._runner = None

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        """配置热更新。"""

    # ───────────────────────── 下行（Host -> 桌宠） ─────────────────────────

    @MessageGateway(
        name=GATEWAY_NAME,
        route_type="duplex",
        platform="desktop",
        protocol="marisa-desktop",
        description="桌宠本地 WebSocket 双工消息网关",
    )
    async def handle_desktop_outbound(
        self,
        message: Dict[str, Any],
        route: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """接收 Host 出站消息并推送给已连接的桌宠客户端。"""
        del metadata
        del kwargs
        try:
            text = _to_text(message)
            payload = {
                "type": "bot_message",
                "text": text,
                "message_id": str(message.get("message_id") or ""),
                "raw_message": message.get("raw_message") or [],
            }
            for ws in list(self._clients):
                try:
                    await ws.send_str(json.dumps(payload, ensure_ascii=False))
                except Exception:
                    self._clients.discard(ws)
            if text:
                self.ctx.logger.info(f"[marisa_desktop] -> 桌宠: {text[:80]}")
            return {"success": True, "external_message_id": None}
        except Exception as exc:
            self.ctx.logger.info("[marisa_desktop] outbound error: %s", exc)
            return {"success": False, "error": str(exc)}

    # ───────────────────────── 上行（桌宠 -> Host） ─────────────────────────

    async def _handle_ws(self, request: Any) -> Any:
        """桌宠客户端 WebSocket 长连接。"""
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self._clients.add(ws)
        self.ctx.logger.info("[marisa_desktop] 桌宠已连接")
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except Exception:
                        continue
                    await self._handle_client_payload(data)
                elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                    break
        finally:
            self._clients.discard(ws)
            self.ctx.logger.info("[marisa_desktop] 桌宠已断开")
        return ws

    async def _handle_client_payload(self, data: Dict[str, Any]) -> None:
        """处理桌宠上行消息：普通聊天注入主链；action_result 唤醒等待方。"""
        kind = str(data.get("type") or "")
        cfg = self.config
        if not cfg.plugin.enabled:
            return
        kind = str(data.get("type") or "chat")
        if kind == "voice":
            await self._handle_voice_payload(data)
            return
        if kind == "favor":
            await self._handle_favor_payload(data)
            return
        if kind == "confirm_result":
            await self._handle_confirm_result(data)
            return
        if kind != "chat":
            return
        text = str(data.get("text") or "").strip()
        if not text:
            return
        # 桌面斜杠指令：确定性直通，不注入主链（避免 AI 插话/重复）
        if text.startswith("/"):
            await self._handle_desktop_slash(text)
            return
        await self._inject_desktop_message(text, source="qt_pet")

    async def _handle_voice_payload(self, data: Dict[str, Any]) -> None:
        """语音输入：识别成文字后按普通桌面消息注入主链。

        语音与打字在注入之后没有任何区别，因此桌宠只是多了一只耳朵，
        大脑、记忆、人格仍然是主链那一颗。
        """

        audio_base64 = str(data.get("audio_base64") or "")
        if not audio_base64:
            return
        seconds = float(data.get("seconds") or 0.0)
        try:
            result = await self.ctx.llm.transcribe_audio(audio_base64=audio_base64)
        except Exception as exc:
            self.ctx.logger.info("[marisa_desktop] 语音识别调用失败: %s", exc)
            await self._push_to_pet("唔…语音识别没连上，主人稍后再试だぜ。")
            return

        text = str(result.get("text") or "").strip() if isinstance(result, dict) else ""
        if not text:
            detail = str(result.get("error") or "") if isinstance(result, dict) else ""
            self.ctx.logger.info("[marisa_desktop] 语音识别未返回文本: %s", detail)
            await self._push_to_pet(
                "没听清主人说了什么だぜ…再按一次说慢一点？"
                if not detail
                else f"语音识别出错了：{detail[:60]}"
            )
            return

        self.ctx.logger.info(
            "[marisa_desktop] 语音识别成功（%.1fs）: %s", seconds, text[:80]
        )
        await self._inject_desktop_message(text, source="qt_pet_voice")

    async def _resolve_desktop_session_id(self, owner_id: str, self_id: str) -> str:
        """解析桌面平台的真实聊天流 ID。

        注入的消息必须带上真实 session_id，否则宿主派发命令时拿不到 stream_id，
        指令回复会被静默丢弃（宿主只负责创建会话，不会回填到消息上）。
        这里通过宿主能力按 platform/account/scope 解析，不自行计算哈希。
        """

        try:
            opened = await self.ctx.chat.open_session(
                platform="desktop",
                chat_type="private",
                user_id=owner_id,
                account_id=self_id,
                scope=GATEWAY_NAME,
            )
        except Exception as exc:
            self.ctx.logger.info("[marisa_desktop] 解析桌面会话失败: %s", exc)
            return ""
        if isinstance(opened, dict):
            return str(opened.get("session_id") or opened.get("stream_id") or "")
        return ""

    async def _inject_desktop_message(self, text: str, *, source: str) -> None:
        """把一段文本当作主人的桌面发言注入主链。"""

        cfg = self.config
        owner_id = str(cfg.identity.owner_user_id)
        nickname = str(cfg.identity.owner_nickname or "主人")
        self_id = str(cfg.identity.bot_account_id or "desktop-0")
        session_id = await self._resolve_desktop_session_id(owner_id, self_id)
        now = str(time.time())
        mid = f"desktop-{uuid.uuid4().hex[:16]}"
        message_info = {
            "user_info": {
                "user_id": owner_id,
                "user_nickname": nickname,
                "user_cardname": None,
            },
            "additional_config": {
                "self_id": self_id,
                "platform_io_account_id": self_id,
                "desktop_source": source,
                # 桌宠经本机 WS 网关注入，是主人本机的可信入口；
                # 显式声明后宿主才会按「本地操作员」放行 operator 命令
                "is_local_operator": True,
            },
        }
        message_dict: Dict[str, Any] = {
            "message_id": mid,
            "timestamp": now,
            "platform": "desktop",
            "message_info": message_info,
            "raw_message": [{"type": "text", "data": text}],
            "is_mentioned": False,
            "is_at": False,
            "is_emoji": False,
            "is_picture": False,
            "is_command": text.startswith("/"),
            "is_notify": False,
            "session_id": session_id,
            "processed_plain_text": text,
            "display_message": text,
        }
        accepted = await self._try_route(mid, message_dict, self_id)
        if accepted:
            self.ctx.logger.info(f"[marisa_desktop] <- 桌宠({owner_id}): {text[:80]} -> Host accepted")
        else:
            self.ctx.logger.info("[marisa_desktop] Host 拒绝注入该消息（可能是平台/网关未就绪）")

    async def _try_route(self, mid: str, message_dict: Dict[str, Any], self_id: str) -> bool:
        try:
            return bool(
                await self.ctx.gateway.route_message(
                    gateway_name=GATEWAY_NAME,
                    message=message_dict,
                    route_metadata={"self_id": self_id, "connection_id": GATEWAY_NAME},
                    external_message_id=mid,
                    dedupe_key=mid,
                )
            )
        except Exception as exc:
            self.ctx.logger.info("[marisa_desktop] route_message error: %s", exc)
            return False


    async def _launcher_loop(self) -> None:
        """桌宠守护：不在则拉起，崩溃自动复活。"""
        await asyncio.sleep(3.0)  # 等主链就绪
        while True:
            try:
                if not self.config.plugin.enabled or not self.config.launcher.auto_start_pet:
                    await asyncio.sleep(float(self.config.launcher.check_interval_sec))
                    continue
                if not self._pet_running():
                    self._spawn_pet()
                await asyncio.sleep(float(self.config.launcher.check_interval_sec))
            except asyncio.CancelledError:
                return
            except Exception as exc:
                self.ctx.logger.info("[marisa_desktop] 守护循环异常: %s", exc)
                await asyncio.sleep(float(self.config.launcher.check_interval_sec))

    def _pet_cmd(self) -> List[str]:
        """根据运行形态选择桌宠启动命令。"""
        mode = str(self.config.launcher.pet_mode or "auto").strip().lower()
        exe = str(self.config.launcher.pet_exe or "")
        if mode == "exe" or (mode == "auto" and exe and os.path.exists(exe)):
            return [exe]
        return [
            str(self.config.launcher.dev_python or sys.executable),
            str(self.config.launcher.dev_script or ""),
        ]

    def _pet_running(self) -> bool:
        """判断桌宠是否已在运行。"""
        # 1) 自己拉起的子进程还活着 → 视为运行中（最可靠）
        if self._pet_proc is not None and self._pet_proc.poll() is None:
            return True
        mode = str(self.config.launcher.pet_mode or "auto").strip().lower()
        exe = str(self.config.launcher.pet_exe or "")
        try:
            if mode == "exe" or (mode == "auto" and exe and os.path.exists(exe)):
                name = os.path.basename(exe).lower()
                base = name[:-4] if name.endswith(".exe") else name
                out = subprocess.run(
                    ["tasklist", "/FI", f"IMAGENAME eq {name}", "/NH"],
                    capture_output=True,
                    timeout=8,
                )
                raw = out.stdout or b""
                if isinstance(raw, bytes):
                    raw = raw.decode("gbk", errors="ignore")
                raw = raw.lower()
                return base in raw or name in raw
            out = subprocess.run(
                [
                    "powershell", "-NoProfile", "-Command",
                    "@(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" "
                    "| Where-Object { $_.CommandLine -like '*run_pet.py*' }).Count",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            # 按整数比较：进程数含 0 的数字（10/20/101…）用子串匹配会被误判为“没在跑”
            return int((out.stdout or "").strip() or "0") > 0
        except Exception:
            # 探测失败时退回“只看自己拉起的进程”
            return bool(self._pet_proc is not None and self._pet_proc.poll() is None)

    def _spawn_pet(self) -> None:
        """拉起桌宠（隐藏窗口；stdout/stderr 追加写 data/pet.log）。"""
        try:
            cmd = self._pet_cmd()
            if not cmd or not os.path.exists(cmd[0]):
                self.ctx.logger.info("[marisa_desktop] 桌宠启动命令无效，跳过: %s", cmd)
                return
            create_no_window = 0x08000000
            kwargs: Dict[str, Any] = {
                "creationflags": create_no_window,
                "close_fds": True,
                "stdout": _open_pet_log(),
                "stderr": subprocess.STDOUT,
                "stdin": subprocess.DEVNULL,
            }
            if str(self.config.launcher.pet_mode).strip().lower() == "dev" or not os.path.exists(
                str(self.config.launcher.pet_exe or "")
            ):
                kwargs["cwd"] = str(self.config.launcher.dev_workdir or os.getcwd())
            self._pet_proc = subprocess.Popen(cmd, **kwargs)
            self.ctx.logger.info("[marisa_desktop] 已拉起桌宠: %s（输出见 %s）", cmd, _PET_LOG)
            asyncio.create_task(self._watch_pet_process(self._pet_proc))
        except Exception as exc:
            self.ctx.logger.info("[marisa_desktop] 拉起桌宠失败: %s", exc)

    async def _watch_pet_process(self, process: Any) -> None:
        """等待桌宠退出并记录退出码，便于定位「拉起就秒退」类问题。"""

        try:
            code = await asyncio.to_thread(process.wait)
        except Exception as exc:
            self.ctx.logger.info("[marisa_desktop] 等待桌宠退出失败: %s", exc)
            return
        self.ctx.logger.info(
            "[marisa_desktop] 桌宠进程已退出，退出码=%s（输出见 %s）", code, _PET_LOG
        )


    # ───────────────────────── 远程执行命令（主人 → 桌宠 action） ─────────────────────────

    @staticmethod
    def _clean_raw(raw: str) -> str:
        """去掉 @At/提及痕迹与「魔理沙」前缀，供意图分类。"""
        t = _RE_AT.sub("", raw or "")
        t = re.sub(r"@[\w\u4e00-\u9fa5]+", "", t)
        for w in ("魔理沙", "雾雨魔理沙", "marisa", "Marisa", "MARISA"):
            t = t.replace(w, "")
        return t.strip().strip("，,。！？!?")

    def _raw(self, kwargs: Any) -> str:
        """提取命令原始文本（命令组捕获的 raw 优先，否则取 text）。"""
        mg = kwargs.get("matched_groups") if isinstance(kwargs, dict) else None
        if isinstance(mg, dict) and mg.get("raw"):
            return str(mg["raw"]).strip()
        txt = kwargs.get("text") if isinstance(kwargs, dict) else None
        return str(txt or "").strip()

    async def _try_reply(self, session_id: str, text: str) -> None:
        """尽力回复（无有效 stream/session 时静默失败）。"""
        if not session_id:
            return
        try:
            await self.ctx.send.text(text, session_id)
        except Exception:
            pass

    async def _notify_pet_bubble(self, text: str) -> None:
        """广播轻量气泡通知给连接的桌宠。"""
        if not self._clients or not text:
            return
        payload = {"type": "bot_message", "text": text, "message_id": "", "raw_message": []}
        for ws in list(self._clients):
            try:
                await ws.send_str(json.dumps(payload, ensure_ascii=False))
            except Exception:
                self._clients.discard(ws)

    def _no_actions_text(self) -> str:
        """桌面执行引擎缺失时的统一提示。

        桌面能力只有宿主侧 native 引擎一份实现，桌宠不再执行动作；
        引擎没加载起来属于环境问题，直接告诉主人而不是悄悄降级。
        """
        self.ctx.logger.info("[marisa_desktop] 桌面原生执行引擎不可用，动作无法执行")
        return "桌面执行引擎没加载起来，主人看看插件日志だぜ。"

    async def _ncm_login_send(self, stream_id: str) -> Optional[str]:
        """网易云扫码登录并尽可能把二维码图送到会话。

        二维码由宿主侧原生引擎生成；已通过 hybrid 送达 stream_id 会话时
        返回 None（无需再回复），否则返回要回复的文本。
        """
        if ACTIONS is None:
            return self._no_actions_text()
        info = ACTIONS.get_ncm_login_qr()
        text = str(info.get("text") or "二维码生成失败だぜ……")
        b64 = str(info.get("b64") or "")
        if b64 and stream_id:
            try:
                await self.ctx.send.hybrid(
                    [{"type": "text", "content": text}, {"type": "image", "content": b64}],
                    stream_id,
                )
                return None
            except Exception:
                pass
        return text

    # ---------------- 原生权限（与宿主 plugin.permission 单源对齐） ----------------

    async def _owner_user_ids(self) -> Set[str]:
        """解析主人名单。

        宿主全局 ``plugin.permission`` 是唯一权威来源（兼容 ``qq:123`` 与裸号
        ``123`` 两种写法，QQ 号全局唯一，按用户号匹配可免疫平台串差异）；名单
        为空时回退插件自身的 ``identity.owner_user_id``，与门控插件保持同一套判定。
        """

        permission = await self.ctx.config.get("plugin.permission", []) or []
        allowed: Set[str] = set()
        for item in permission:
            item = str(item or "").strip()
            if not item:
                continue
            allowed.add(item.split(":", 1)[1] if ":" in item else item)
        if not allowed:
            fallback_owner = str(self.config.identity.owner_user_id or "").strip()
            if fallback_owner:
                allowed.add(fallback_owner)
        return allowed

    async def _caller_authorized(self, platform: str = "", user_id: str = "") -> bool:
        """运行时调用者鉴权（工具侧唯一一层）。

        放行条件：桌宠 desktop 通道（WS 注入身份即主人）、本地控制台平台，
        或 user_id 命中主人名单。命令侧不重复调用本方法：宿主已对
        ``permission="operator"`` 的命令做过统一拦截。
        """

        platform = str(platform or "").strip()
        if platform in ("desktop", "bot_console", "maisaka_cli"):
            return True
        user_id = str(user_id or "").strip()
        if not user_id:
            return False
        allowed = await self._owner_user_ids()
        if user_id in allowed:
            return True
        self.ctx.logger.info(
            "[marisa_desktop] 权限拦截：platform=%s user_id=%s 名单=%s",
            platform, user_id, sorted(allowed),
        )
        return False

    @staticmethod
    def _parse_slash_text(text: str) -> Tuple[str, str]:
        """把 "/点歌 Bad Apple"（已去 @/魔理沙前缀）拆成 (cmd, args)。"""
        t = (text or "").strip()
        if not t.startswith("/"):
            return "", ""
        parts = t[1:].split(None, 1)
        cmd = str(parts[0]).lower()
        args = (parts[1] if len(parts) > 1 else "").strip()
        return cmd, args

    async def _dispatch_qq_command(self, kwargs: Any, allowed_cmds: Tuple[str, ...]) -> Tuple[bool, str, bool]:
        """QQ 侧 /指令统一入口：解析 → 执行内核 → 回复并拦截主链。

        鉴权只在宿主一层完成：``@Command(..., permission="operator")`` 的命令
        在进入本方法前已被 host 按 plugin.permission 与 command_permissions 拦下，
        这里不再重复校验，避免与宿主名单产生第二套判定标准。
        """
        if not isinstance(kwargs, dict):
            return False, "", False
        stream_id = str(kwargs.get("stream_id") or "")
        text = self._clean_raw(self._raw(kwargs))
        cmd, args = self._parse_slash_text(text)
        if cmd not in allowed_cmds:
            return False, "", False
        result = await self._run_slash(
            cmd,
            args,
            stream_id,
            platform=str(kwargs.get("platform") or ""),
            user_id=str(kwargs.get("user_id") or ""),
        )
        if result:
            await self._try_reply(stream_id, result)
        return True, "", True

    def _fav_value(self) -> int:
        """读取跨端好感度（MaiBot 单源文件）。"""
        try:
            path = _PLUGIN_DIR.parent.parent / "data" / "marisa" / "favorability.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            return int(data.get("favorability", 0))
        except Exception:
            return 0

    async def _handle_favor_payload(self, data: Dict[str, Any]) -> None:
        """桌宠上报互动事件，由宿主统一记账（好感度只有这一份文件）。

        与 marisa_gate 共用 data/marisa/favorability.json，双方都按
        「先重读磁盘再改」写入，避免各自的内存副本互相覆盖。
        """

        reason = str(data.get("reason") or "桌面互动")
        try:
            delta = int(data.get("delta") or 1)
        except (TypeError, ValueError):
            delta = 1
        delta = max(0, min(5, delta))

        path = _PLUGIN_DIR.parent.parent / "data" / "marisa" / "favorability.json"
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            current = {"favorability": self._fav_value(), "total_interactions": 0, "history": {}}

        now = time.time()
        history = current.setdefault("history", {})
        hour_key = str(int(now // 3600))
        stats = history.setdefault(hour_key, {"count": 0, "sum": 0})
        stats["count"] = int(stats.get("count", 0)) + 1
        stats["sum"] = int(stats.get("sum", 0)) + delta

        old = int(current.get("favorability", 0))
        current["favorability"] = max(0, min(100, old + delta))
        current["total_interactions"] = int(current.get("total_interactions", 0)) + 1
        current["updated_at"] = now
        if len(history) > 48:
            for key in list(history)[:-48]:
                history.pop(key, None)

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
            self.ctx.logger.info(
                "[marisa_desktop] 好感度 +%s（%s）-> %s",
                delta, reason, current["favorability"],
            )
        except Exception as exc:
            self.ctx.logger.info("[marisa_desktop] 写入好感度失败: %s", exc)

    async def _handle_confirm_result(self, data: Dict[str, Any]) -> None:
        """桌宠弹窗的确认结果：转发给门控插件的清理引擎执行。"""

        req_id = str(data.get("req_id") or "")
        try:
            plan_code = int(data.get("plan_code") or 0)
        except (TypeError, ValueError):
            plan_code = 0
        approved = bool(data.get("approved"))
        selected_ids: List[int] = []
        if isinstance(data.get("selected"), list):
            for item in data["selected"]:
                try:
                    selected_ids.append(int(item))
                except (TypeError, ValueError):
                    continue
        permanent = bool(data.get("permanent"))

        try:
            result = await self.ctx.api.call(
                "marisa-team.marisa-gate.cleanup_confirm",
                plan_code=plan_code,
                req_id=req_id,
                approved=approved,
                selected=selected_ids or None,
                permanent=permanent,
            )
        except Exception as exc:
            self.ctx.logger.info("[marisa_desktop] 清理确认转发失败: %s", exc)
            await self._push_to_pet(f"确认没送出去：{str(exc)[:80]}")
            return
        if not isinstance(result, dict) or not result.get("accepted"):
            detail = str((result or {}).get("error") or "方案可能已过期") if isinstance(result, dict) else "方案可能已过期"
            await self._push_to_pet(f"确认没有生效：{detail}")
            return
        self.ctx.logger.info(
            "[marisa_desktop] 清理方案 P%s 桌宠确认：approved=%s selected=%s permanent=%s",
            plan_code, approved, selected_ids, permanent,
        )
        if approved:
            await self._push_to_pet("收到，开始执行清理だぜ…")
        else:
            await self._push_to_pet("好，方案取消だぜ。")

    @staticmethod
    def _favor_rank(favorability: int) -> str:
        """好感度对应的羁绊称号（与 marisa_gate 单源口径一致）。"""

        if favorability >= 80:
            return "灵魂挚友"
        if favorability >= 60:
            return "亲密搭档"
        if favorability >= 40:
            return "可靠的搭档"
        if favorability >= 20:
            return "熟络的助手"
        return "初识的魔法使"

    # ---------------- 音乐播发桥接（群友点歌走群语音，主人走电脑外放） ----------------

    def _get_pan_ice_api(self) -> Any:
        """获取 github_pan-ice_music 的 MusicSearchClient 实例。"""
        if hasattr(self, "_pan_ice_music_api") and self._pan_ice_music_api is not None:
            return self._pan_ice_music_api
        try:
            plugins_root = _PLUGIN_DIR.parent
            if str(plugins_root) not in sys.path:
                sys.path.insert(0, str(plugins_root))
            import importlib
            mod = importlib.import_module("github_pan-ice_music.music_api")
            self._pan_ice_music_api = mod.MusicSearchClient()
            return self._pan_ice_music_api
        except Exception as exc:
            self.ctx.logger.warning("[marisa_desktop] 加载 github_pan-ice_music.music_api 失败: %s", exc)
            return None

    async def _send_group_member_voice_music(self, song_name: str, stream_id: str) -> tuple[bool, str]:
        """为群友/非主人搜索歌曲并以语音条形式发送到群聊/私聊。"""
        if not song_name:
            return False, "还没有告诉我歌名だぜ！"
        if not stream_id:
            return False, "未获取到有效的消息会话，无法发送语音条だぜ。"

        api = self._get_pan_ice_api()
        if not api:
            return False, "音乐搜索服务暂时不可用だぜ……"

        # 优先在网易云(163)搜索，失败换 QQ 音乐(qq)
        for platform in ("163", "qq"):
            try:
                results = await api.search(song_name, platform, limit=3)
            except Exception as exc:
                self.ctx.logger.warning("[marisa_desktop] 搜歌异常(platform=%s, kw=%s): %s", platform, song_name, exc)
                continue

            if not results:
                continue

            for song in results:
                try:
                    media_id = song.media_id
                    if song.platform == "qq" and not media_id:
                        detail = await api.get_qq_song_detail(song.song_id)
                        if detail:
                            media_id = detail.media_id

                    audio_url = await api.get_song_url(song.song_id, song.platform, media_id, mp3_only=False)
                    if not audio_url:
                        continue

                    # 通过 voiceurl 发送语音消息段给群友
                    sent = await self.ctx.send.custom("voiceurl", {"url": audio_url}, stream_id)
                    if sent:
                        return True, f"已为群友送上歌曲《{song.name} - {song.artists}》♪"
                except Exception as exc:
                    self.ctx.logger.warning("[marisa_desktop] 音频直链发送失败(%s): %s", song.display(), exc)
                    continue

        # 如果没有获取到可用语音直链，提示群友
        return False, f"未能在音乐平台找到《{song_name}》的可播放音频だぜ……"

    # ---------------- 工具：音乐（本地原生直连播放） ----------------

    @Tool(
        "marisa_play_music",
        description="点歌播放音乐。主人点歌时会在主人电脑上网易云播放；群友/其他成员点歌时会发送语音条到群聊中。",
        parameters=[ToolParameterInfo(name="song", param_type=ToolParamType.STRING, description="歌名，如：Bad Apple、千本樱", required=True)],
    )
    async def tool_play_music(self, song: str = "", **kwargs: Any) -> str:
        song_name = str(song or "").strip()
        if not song_name:
            return "需要告诉我歌名だぜ……"

        stream_id = str(kwargs.get("stream_id") or "")
        is_owner = await self._caller_authorized(str(kwargs.get("platform") or ""), str(kwargs.get("user_id") or ""))

        if is_owner:
            # 主人点歌：在电脑本地网易云播放
            if ACTIONS:
                res = ACTIONS.play_music(song_name)
                asyncio.create_task(self._notify_pet_bubble(f"正在播放：{song_name} ♪"))
                return res
            return self._no_actions_text()
        else:
            # 群友点歌：发送语音条到群聊天
            ok, msg = await self._send_group_member_voice_music(song_name, stream_id)
            return msg

    @Tool(
        "marisa_recommend_music",
        description="主人想听音乐但没点具体歌（如：放首歌/来点音乐/不知道听什么/推荐歌曲）时使用，在主人电脑上播放今日魔理推荐歌单。",
    )
    async def tool_recommend_music(self, **kwargs: Any) -> str:
        if not await self._caller_authorized(str(kwargs.get("platform") or ""), str(kwargs.get("user_id") or "")):
            return OWNER_DENY_TEXT
        del kwargs
        if ACTIONS:
            res = ACTIONS.play_recommend()
            asyncio.create_task(self._notify_pet_bubble("播放今日推荐歌单だぜ！♪"))
            return res
        return self._no_actions_text()
    @Tool(
        "marisa_music_control",
        description="控制主人电脑上正在播放的音乐（暂停/继续/下一首/上一首）。",
        parameters=[ToolParameterInfo(name="action", param_type=ToolParamType.STRING, description="取值为 pause / play / next / prev", required=True)],
    )
    async def tool_music_control(self, action: str = "", **kwargs: Any) -> str:
        if not await self._caller_authorized(str(kwargs.get("platform") or ""), str(kwargs.get("user_id") or "")):
            return OWNER_DENY_TEXT
        del kwargs
        act = str(action or "").strip().lower()
        if act not in ("pause", "play", "next", "prev"):
            return "请指定 pause/play/next/prev"
        if ACTIONS:
            return ACTIONS.control_music(act)
        return self._no_actions_text()
    @Tool(
        "marisa_ncm_login",
        description="主人要登录/绑定/重新登录网易云账号时使用（比如：网易云登录、扫码登录、弹一个登录二维码、怎么绑定网易云、日推做不了要登录）。生成网易云登录二维码发给主人，手机扫码即完成绑定；本工具不会去打开网易云客户端应用。",
    )
    async def tool_ncm_login(self, **kwargs: Any) -> str:
        stream_id = str(kwargs.get("stream_id") or "")
        if not await self._caller_authorized(str(kwargs.get("platform") or ""), str(kwargs.get("user_id") or "")):
            return OWNER_DENY_TEXT
        text = await self._ncm_login_send(stream_id)
        if text is None:
            return "已生成网易云登录二维码并发给主人，用手机网易云 App 扫一扫即可绑定♪"
        return text

    # ---------------- 工具：系统/桌面（本地原生直接操控） ----------------

    @Tool(
        "marisa_open_app",
        description="在主人的电脑上打开应用或网站（如 VSCode、Steam、QQ、浏览器网址）。",
        parameters=[ToolParameterInfo(name="app", param_type=ToolParamType.STRING, description="应用名或网址", required=True)],
    )
    async def tool_open_app(self, app: str = "", **kwargs: Any) -> str:
        if not await self._caller_authorized(str(kwargs.get("platform") or ""), str(kwargs.get("user_id") or "")):
            return OWNER_DENY_TEXT
        del kwargs
        if not app or len(str(app).strip()) < 2:
            return "想让我打开什么呀？"
        if ACTIONS:
            res = ACTIONS.open_app(str(app).strip())
            asyncio.create_task(self._notify_pet_bubble(f"已为你打开 {app} だぜ！"))
            return res
        return self._no_actions_text()
    @Tool(
        "marisa_volume",
        description="调节主人电脑音量（加大/减小/静音切换）。",
        parameters=[ToolParameterInfo(name="action", param_type=ToolParamType.STRING, description="取值为 volume_up / volume_down / volume_mute", required=True)],
    )
    async def tool_volume(self, action: str = "", **kwargs: Any) -> str:
        if not await self._caller_authorized(str(kwargs.get("platform") or ""), str(kwargs.get("user_id") or "")):
            return OWNER_DENY_TEXT
        del kwargs
        act = str(action or "").strip().lower()
        if act not in ("volume_up", "volume_down", "volume_mute"):
            return "请指定 volume_up/volume_down/volume_mute"
        if ACTIONS:
            return ACTIONS.control_volume(act)
        return self._no_actions_text()
    @Tool("marisa_read_clipboard", description="读取主人在电脑上复制（剪贴板）的内容。")
    async def tool_clipboard(self, **kwargs: Any) -> str:
        if not await self._caller_authorized(str(kwargs.get("platform") or ""), str(kwargs.get("user_id") or "")):
            return OWNER_DENY_TEXT
        del kwargs
        if ACTIONS:
            return ACTIONS.get_clipboard()
        return self._no_actions_text()
    # ---------------- 工具：仅主人 - 信息/系统 ----------------

    @Tool("marisa_favor", description="查询魔理沙与主人的好感度/羁绊。主人询问时使用。")
    async def tool_favor(self, **kwargs: Any) -> str:
        del kwargs
        fav = self._fav_value()
        if fav >= 80:
            rank = "灵魂挚友"
        elif fav >= 60:
            rank = "亲密搭档"
        elif fav >= 40:
            rank = "可靠的搭档"
        elif fav >= 20:
            rank = "熟络的助手"
        else:
            rank = "初识的魔法使"
        return f"当前好感度 {fav}/100（{rank}），桌面与QQ同源计算。"

    @Tool("marisa_uptime", description="查询主人电脑本次开机时长。")
    async def tool_uptime(self, **kwargs: Any) -> str:
        del kwargs
        try:
            import ctypes
            ms = int(ctypes.windll.kernel32.GetTickCount64())
            total_min = int(ms // 60000)
            days, rem = divmod(total_min, 1440)
            hours, mins = divmod(rem, 60)
            return f"电脑已连续开机 {days} 天 {hours} 小时 {mins} 分。"
        except Exception as exc:
            return f"查询失败：{exc}"

    @Tool("marisa_balance", description="查询主人 DeepSeek 模型账户余额。")
    async def tool_balance(self, **kwargs: Any) -> str:
        if not await self._caller_authorized(str(kwargs.get("platform") or ""), str(kwargs.get("user_id") or "")):
            return OWNER_DENY_TEXT
        del kwargs
        try:
            key = ""
            candidate = _PLUGIN_DIR.parent.parent / "config.json"  # 一体化工程根（桌宠 config.json）
            if candidate.exists():
                try:
                    data = json.loads(candidate.read_text(encoding="utf-8"))
                    key = str((data.get("deepseek") or {}).get("api_key") or "")
                except Exception:
                    key = ""
            if not key:
                try:
                    gate_cfg = await self.ctx.config.get_plugin("marisa_gate") or {}
                    key = str(((gate_cfg.get("rules") or {}).get("deepseek_api_key")) or "")
                except Exception:
                    pass
            if not key:
                return "还没配 DeepSeek Key，查不了余额。"
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.get(
                    "https://api.deepseek.com/user/balance",
                    headers={"Authorization": f"Bearer {key}"},
                )
                data = resp.json()
            infos = data.get("balance_infos") or []
            if infos:
                total = sum(float(i.get("total_balance") or 0) for i in infos)
                return f"DeepSeek 余额还剩 {total:.2f} 元。"
            return f"余额接口异常：{str(data)[:120]}"
        except Exception as exc:
            return f"查询余额出错：{exc}"

    @Tool("marisa_lock_screen", description="锁定主人的电脑屏幕。")
    async def tool_lock_screen(self, **kwargs: Any) -> str:
        if not await self._caller_authorized(str(kwargs.get("platform") or ""), str(kwargs.get("user_id") or "")):
            return OWNER_DENY_TEXT
        del kwargs
        try:
            import ctypes
            ok = ctypes.windll.user32.LockWorkStation()
            return "屏幕已锁定。" if ok else "锁屏指令没生效。"
        except Exception as exc:
            return f"锁屏出错：{exc}"

    @Tool(
        "marisa_screenshot",
        description="截取主人电脑当前屏幕并把截图图片发给主人（主人说：截个屏/看看我屏幕/拍一下屏幕 时使用）。",
    )
    async def tool_screenshot(self, **kwargs: Any) -> str:
        if not await self._caller_authorized(str(kwargs.get("platform") or ""), str(kwargs.get("user_id") or "")):
            return OWNER_DENY_TEXT
        stream_id = str(kwargs.get("stream_id") or "")
        if not ACTIONS:
            return "截图需要本机桌面引擎だぜ……请确认桌宠已就绪。"
        try:
            _path, b64 = ACTIONS.capture_screenshot()
        except Exception as exc:
            return f"截屏失败了：{exc}"
        if not b64:
            return "截屏失败了だぜ……"
        text = "这是主人屏幕现在的样子だぜ！📷"
        if stream_id:
            try:
                await self.ctx.send.hybrid(
                    [{"type": "text", "content": text}, {"type": "image", "content": b64}],
                    stream_id,
                )
                return "截图已发给主人。"
            except Exception:
                pass
        return text

    # ---------------- 工具：天气（需明确城市；群友也可问） ----------------

    @Tool(
        "marisa_weather",
        description="查询某个明确城市的实时天气。只有当用户明确说出了具体城市/地点（如：东京天气、北京天气、上海明天下雨吗）时才调用；若用户只笼统说“天气”而没给地点，不要调用本工具，直接反问想去哪座城市。",
        parameters=[ToolParameterInfo(name="city", param_type=ToolParamType.STRING, description="明确的城市名", required=True)],
    )
    async def tool_weather(self, city: str = "", **kwargs: Any) -> str:
        del kwargs
        city = str(city or "").strip()
        if len(city) < 2:
            return "需要具体城市才能查だぜ。"
        try:
            import urllib.parse
            url = "https://wttr.in/" + urllib.parse.quote(city) + "?format=j1"
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.get(url, headers={"User-Agent": "curl/7.68.0"})
                data = resp.json()
            current = (data.get("current_condition") or [{}])[0]
            area = (data.get("nearest_area") or [{}])[0]
            area_name = ((area.get("areaName") or [{}])[0].get("value") if area.get("areaName") else "") or city
            temp_c = current.get("temp_C", "--")
            feels = current.get("FeelsLikeC", temp_c)
            humi = current.get("humidity", "--")
            desc_en = ((current.get("weatherDesc") or [{}])[0].get("value")) or "Clear"
            desc_map = {
                "Sunny": "晴朗", "Clear": "晴天", "Partly cloudy": "多云", "Cloudy": "阴天",
                "Overcast": "阴天/多云", "Mist": "薄雾", "Fog": "大雾", "Light rain": "小雨",
                "Moderate rain": "中雨", "Heavy rain": "大雨", "Patchy rain possible": "局部有雨",
                "Patchy rain nearby": "局部有雨", "Thundery outbreaks possible": "雷阵雨可能",
                "Light snow": "小雪",
            }
            desc = desc_map.get(desc_en, desc_en)
            return f"{area_name} 实时天气：{desc}，气温 {temp_c}°C（体感约 {feels}°C），湿度 {humi}%。"
        except Exception as exc:
            return f"天气服务遇到问题：{exc}"

    # ---------------- 斜杠指令（桌面=主人全量；QQ 群友带〔群友〕标记时由门控拒绝） ----------------

    def _help_text(self) -> str:
        """生成纯文本指令大全（与注册命令同源）。"""

        return f"{render_text(collect_command_entries(self))}\n{HELP_FOOTER_NOTE}"

    async def _help_image(self) -> str:
        """把指令菜单渲染成图片，返回 base64；渲染不可用时返回空串。"""

        try:
            html = render_html(collect_command_entries(self))
            result = await self.ctx.render.html2png(
                html,
                selector="body",
                viewport={"width": 760, "height": 900},
                device_scale_factor=2.0,
                full_page=True,
            )
        except Exception as exc:
            self.ctx.logger.info("[marisa_desktop] 指令菜单渲染失败，退回文本: %s", exc)
            return ""
        if isinstance(result, dict):
            return str(result.get("image_base64") or "")
        return str(getattr(result, "image_base64", "") or "")

    async def _handle_desktop_slash(self, text: str) -> None:
        """桌面通道直收 /指令：先走桌宠自己的确定性处理，不认识的交回主链。

        桌宠只认识与自己直接相关的斜杠；其余（如 /体检、/终端、/项目）交给
        宿主主链里的门控插件处理，两个通道的行为因此保持一致。
        """
        parts = text[1:].split(None, 1)
        cmd = parts[0].lower()
        args = (parts[1] if len(parts) > 1 else "").strip()
        try:
            # 桌面通道由本机 WS 直连，身份即主人
            result = await self._run_slash(cmd, args, "", platform="desktop")
        except Exception as exc:
            result = f"指令执行出错：{str(exc)[:120]}"
        if result is None:
            await self._inject_desktop_message(text, source="qt_pet")
            return
        payload = {"type": "bot_message", "text": result, "message_id": "", "raw_message": []}
        for ws in list(self._clients):
            try:
                await ws.send_str(json.dumps(payload, ensure_ascii=False))
            except Exception:
                self._clients.discard(ws)

    async def _run_slash(
        self,
        cmd: str,
        args: str,
        stream_id: str,
        *,
        platform: str = "",
        user_id: str = "",
    ) -> Optional[str]:
        """斜杠子命令分发；返回回复文本（None=不处理）。

        调用受保护工具时必须带上调用者身份，否则工具侧的鉴权会因为拿不到
        platform/user_id 而拒绝执行（桌面通道传 platform="desktop"）。
        """
        if cmd in ("help", "帮助"):
            return self._help_text()
        if cmd == "天气":
            if len(args) < 2:
                return "想查哪座城？格式：/天气 武汉"
            return await self.tool_weather(city=args)
        if cmd == "好感度":
            fav = self._fav_value()
            return f"【好感度】{fav}/100（{self._favor_rank(fav)}）"
        if cmd == "余额":
            return await self.tool_balance(platform=platform, user_id=user_id)
        if cmd in ("开机时长", "时长"):
            return await self.tool_uptime()
        if cmd in ("锁屏", "锁定"):
            return await self.tool_lock_screen(platform=platform, user_id=user_id)
        if cmd in ("推荐歌曲", "来点音乐", "今日推荐"):
            if ACTIONS:
                return ACTIONS.play_recommend()
            return self._no_actions_text()
        if cmd in ("点歌", "播放歌曲"):
            if len(args) < 1:
                return "格式：/点歌 歌名"
            if ACTIONS:
                return ACTIONS.play_music(args)
            return self._no_actions_text()
        if cmd in ("我的歌单", "我喜欢的音乐"):
            if ACTIONS:
                return ACTIONS.play_my_playlist()
            return self._no_actions_text()
        if cmd in ("播放", "继续"):
            if ACTIONS:
                return ACTIONS.control_music("play")
            return self._no_actions_text()
        if cmd == "暂停":
            if ACTIONS:
                return ACTIONS.control_music("pause")
            return self._no_actions_text()
        if cmd in ("下一首", "下首", "切歌"):
            if ACTIONS:
                return ACTIONS.control_music("next")
            return self._no_actions_text()
        if cmd in ("上一首", "上首"):
            if ACTIONS:
                return ACTIONS.control_music("prev")
            return self._no_actions_text()
        if cmd == "音量":
            if "静音" in args or "无声" in args:
                act = "volume_mute"
            elif any(w in args for w in ["大", "加", "高"]):
                act = "volume_up"
            elif any(w in args for w in ["小", "减", "低", "降"]):
                act = "volume_down"
            else:
                return "格式：/音量 大 /音量 小 /音量 静音"
            if ACTIONS:
                return ACTIONS.control_volume(act)
            return self._no_actions_text()
        if cmd in ("开软件", "打开"):
            if len(args) < 1:
                return "格式：/开软件 VSCode"
            if ACTIONS:
                return ACTIONS.open_app(args)
            return self._no_actions_text()
        if cmd == "剪贴板":
            if ACTIONS:
                return ACTIONS.get_clipboard()
            return self._no_actions_text()
        if cmd in ("网易云登录", "扫码登录"):
            return (await self._ncm_login_send(stream_id)) or "二维码图片已发给主人，用手机网易云 App 扫一扫だぜ♪"
        if cmd in ("截图", "截屏"):
            return "QQ 里用 /截图 能收到图；桌面上可以直接看屏幕だぜ。"
        if cmd in ("终端", "关闭电脑", "关机", "取消关机", "opencode"):
            return "该指令需要在 QQ（@她说）：/终端 · /关闭电脑 · /取消关机 · /opencode 走确认流程だぜ。"
        return None

    # ---------------- QQ 原生命令：桌宠能力（/斜杠 触发，默认仅管理员） ----------------
    # 每条命令即 MaiBot 原生组件：WebUI「命令权限」页可逐条启停/授权，
    # host 对 operator 命令先行拦截，_dispatch_qq_command 内再按名单兜底校验。

    @Command("marisa_help", description="查看魔理沙 QQ 指令大全：/help（所有人可用）", pattern=cmd_pattern(r"/(?:help|帮助|指令)(?:\s+[\s\S]*)?\s*$"))
    async def cmd_help(self, **kwargs: Any) -> tuple:
        stream_id = str((kwargs or {}).get("stream_id") or "")
        image_b64 = await self._help_image()
        if image_b64 and stream_id:
            try:
                await self.ctx.send.hybrid(
                    [
                        {"type": "text", "content": "魔理沙的指令大全だぜ，看图就能用！★"},
                        {"type": "image", "content": image_b64},
                    ],
                    stream_id,
                )
                return True, "", True
            except Exception as exc:
                self.ctx.logger.info("[marisa_desktop] 指令菜单图片发送失败，退回文本: %s", exc)
        await self._try_reply(stream_id, self._help_text())
        return True, "", True

    @Command("marisa_weather", description="查询天气：/天气 <城市>（仅管理员）", pattern=cmd_pattern(r"/天气(?:\s+[\s\S]*)?\s*$"), permission="operator")
    async def cmd_weather(self, **kwargs: Any) -> tuple:
        return await self._dispatch_qq_command(kwargs, ("天气",))

    @Command("marisa_music_play", description="点歌：/点歌 <歌名>（主人电脑播放，群友发送群语音）", pattern=cmd_pattern(r"/(?:点歌|播放歌曲)(?:\s+[\s\S]*)?\s*$"))
    async def cmd_music_play(self, **kwargs: Any) -> tuple:
        if not isinstance(kwargs, dict):
            return False, "", False
        stream_id = str(kwargs.get("stream_id") or "")
        text = self._clean_raw(self._raw(kwargs))
        cmd, args = self._parse_slash_text(text)
        if cmd not in ("点歌", "播放歌曲"):
            return False, "", False

        if not args:
            await self._try_reply(stream_id, "格式：/点歌 歌名")
            return True, "", True

        is_owner = await self._caller_authorized(str(kwargs.get("platform") or ""), str(kwargs.get("user_id") or ""))
        if is_owner:
            # 主人点歌：在电脑本地网易云播放
            result = await self._run_slash(cmd, args, stream_id)
            if result:
                await self._try_reply(stream_id, result)
            return True, "", True
        else:
            # 群友点歌：发送群语音条
            ok, msg = await self._send_group_member_voice_music(args, stream_id)
            if not ok:
                await self._try_reply(stream_id, msg)
            return True, "", True

    @Command("marisa_music_recommend", description="播放今日魔理推荐：/推荐歌曲（仅管理员）", pattern=cmd_pattern(r"/(?:推荐歌曲|今日推荐|来点音乐)(?:\s+[\s\S]*)?\s*$"), permission="operator")
    async def cmd_music_recommend(self, **kwargs: Any) -> tuple:
        return await self._dispatch_qq_command(kwargs, ("推荐歌曲", "今日推荐", "来点音乐"))

    @Command("marisa_music_play_toggle", description="继续播放音乐：/播放（仅管理员）", pattern=cmd_pattern(r"/(?:播放|继续)(?:\s+[\s\S]*)?\s*$"), permission="operator")
    async def cmd_music_play_toggle(self, **kwargs: Any) -> tuple:
        return await self._dispatch_qq_command(kwargs, ("播放", "继续"))

    @Command("marisa_music_pause", description="暂停播放音乐：/暂停（仅管理员）", pattern=cmd_pattern(r"/暂停(?:\s+[\s\S]*)?\s*$"), permission="operator")
    async def cmd_music_pause(self, **kwargs: Any) -> tuple:
        return await self._dispatch_qq_command(kwargs, ("暂停",))

    @Command("marisa_music_next", description="切到下一首：/下一首（仅管理员）", pattern=cmd_pattern(r"/(?:下一首|下首|切歌)(?:\s+[\s\S]*)?\s*$"), permission="operator")
    async def cmd_music_next(self, **kwargs: Any) -> tuple:
        return await self._dispatch_qq_command(kwargs, ("下一首", "下首", "切歌"))

    @Command("marisa_music_prev", description="回到上一首：/上一首（仅管理员）", pattern=cmd_pattern(r"/(?:上一首|上首)(?:\s+[\s\S]*)?\s*$"), permission="operator")
    async def cmd_music_prev(self, **kwargs: Any) -> tuple:
        return await self._dispatch_qq_command(kwargs, ("上一首", "上首"))

    @Command("marisa_music_volume", description="调节电脑音量：/音量 大|小|静音（仅管理员）", pattern=cmd_pattern(r"/音量(?:\s+[\s\S]*)?\s*$"), permission="operator")
    async def cmd_music_volume(self, **kwargs: Any) -> tuple:
        return await self._dispatch_qq_command(kwargs, ("音量",))

    @Command("marisa_playlist_like", description="播放我喜欢的音乐歌单：/我的歌单（仅管理员）", pattern=cmd_pattern(r"/(?:我的歌单|我喜欢的音乐)(?:\s+[\s\S]*)?\s*$"), permission="operator")
    async def cmd_playlist_like(self, **kwargs: Any) -> tuple:
        return await self._dispatch_qq_command(kwargs, ("我的歌单", "我喜欢的音乐"))

    @Command("marisa_open_app", description="打开电脑软件/网址：/开软件 <名称>（仅管理员）", pattern=cmd_pattern(r"/(?:开软件|打开)(?:\s+[\s\S]*)?\s*$"), permission="operator")
    async def cmd_open_app(self, **kwargs: Any) -> tuple:
        return await self._dispatch_qq_command(kwargs, ("开软件", "打开"))

    @Command("marisa_clipboard", description="读取电脑剪贴板：/剪贴板（仅管理员）", pattern=cmd_pattern(r"/剪贴板(?:\s+[\s\S]*)?\s*$"), permission="operator")
    async def cmd_clipboard(self, **kwargs: Any) -> tuple:
        return await self._dispatch_qq_command(kwargs, ("剪贴板",))

    @Command("marisa_favor", description="查看与魔理沙的好感度：/好感度（仅管理员）", pattern=cmd_pattern(r"/好感度(?:\s+[\s\S]*)?\s*$"), permission="operator")
    async def cmd_favor(self, **kwargs: Any) -> tuple:
        return await self._dispatch_qq_command(kwargs, ("好感度",))

    @Command("marisa_balance", description="查询 DeepSeek 账户余额：/余额（仅管理员）", pattern=cmd_pattern(r"/余额(?:\s+[\s\S]*)?\s*$"), permission="operator")
    async def cmd_balance(self, **kwargs: Any) -> tuple:
        return await self._dispatch_qq_command(kwargs, ("余额",))

    @Command("marisa_uptime", description="查询电脑开机时长：/开机时长（仅管理员）", pattern=cmd_pattern(r"/(?:开机时长|时长)(?:\s+[\s\S]*)?\s*$"), permission="operator")
    async def cmd_uptime(self, **kwargs: Any) -> tuple:
        return await self._dispatch_qq_command(kwargs, ("开机时长", "时长"))

    @Command("marisa_lock", description="锁定电脑屏幕：/锁屏（仅管理员）", pattern=cmd_pattern(r"/(?:锁屏|锁定)(?:\s+[\s\S]*)?\s*$"), permission="operator")
    async def cmd_lock(self, **kwargs: Any) -> tuple:
        return await self._dispatch_qq_command(kwargs, ("锁屏", "锁定"))

    # ---------------- 网易云扫码登录（QQ 收图；仅管理员） ----------------

    @Command("d_ncm_login", description="网易云扫码登录：/网易云登录（仅管理员）", pattern=cmd_pattern(r"/(?:网易云登录|扫码登录)(?:\s+[\s\S]*)?\s*$"), permission="operator")
    async def cmd_ncm_login(self, **kwargs: Any) -> tuple:
        # 鉴权由宿主对 operator 命令统一完成，这里只负责发码
        stream_id = str((kwargs or {}).get("stream_id") or "")
        text = await self._ncm_login_send(stream_id)
        if text:
            await self._try_reply(stream_id, text)
        return True, "", True

def create_plugin() -> MarisaDesktopPlugin:
    """创建插件实例。"""
    return MarisaDesktopPlugin()
