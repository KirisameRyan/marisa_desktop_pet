# -*- coding: utf-8 -*-
"""魔理沙识图插件。

两条入口：

1. **工具** ``marisa_identify_character``：让魔理沙在聊天里主动看图认人。
   不带参数时自动取当前会话最近一张图，也可以指定本地图片路径。
2. **指令** ``/识图``：先发指令进入"待图"状态，随后发来的下一张图会被
   确定性拦截识别并直接回复（不走主链）。

识别在本地分类服务（`service/serve.py`，端口 9881）完成：WD-ConvNeXt v3
特征 + 数据集自带的 134 个东方角色逻辑回归头，只回答"图里有谁"，不给坐标。
"""

from __future__ import annotations

import asyncio
import base64
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from maibot_sdk import Command, Field, HookHandler, HomeCard, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import HookMode, HookOrder, ToolParameterInfo, ToolParamType

_PLUGIN_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _PLUGIN_DIR.parent.parent
# 识图服务的启动输出与跨进程启动锁放在工程 data 下，便于出问题时统一排查
_API_LOG = _PROJECT_ROOT / "data" / "anime_cv.log"
_START_LOCK_PATH = _PROJECT_ROOT / "data" / "anime_cv.start.lock"
_DEFAULT_START_SCRIPT = _PLUGIN_DIR / "service" / "start_cls_api.bat"

_AT_MARK = r"(?:\s*〔[^〕]*〕\s*)?(?:\s*(?:\[At:[^\]]*\]|@[\w\u4e00-\u9fa5]+)\s*)*"
_TRIGGER = r"(?:魔理沙[，,、\s]*)?"

_TOOL_DESCRIPTION = (
    "识别一张图片里出现的东方 Project 角色，返回角色中文名与置信度（可能同时有多个角色）。"
    "适用场景：聊天里有人发了图、有人问你「图里是谁」、或你想知道刚收到的图里有什么角色时。"
    "不带参数时自动取当前会话最近一张图；也可以传 image_path 指定本地图片路径。"
    "识别不到已知角色时会返回空结果，不要编造角色名。"
)


def cmd_pattern(inner: str) -> str:
    """统一命令正则前缀：容忍 @At 与「魔理沙」前缀。"""
    return "^" + _AT_MARK + _TRIGGER + "(?P<raw>" + inner + ")$"


def _open_api_log() -> Any:
    """打开识图服务输出日志（追加）；打不开时退回丢弃输出。"""

    try:
        _API_LOG.parent.mkdir(parents=True, exist_ok=True)
        return open(_API_LOG, "ab")
    except OSError:
        return subprocess.DEVNULL


def _try_acquire_start_lock(ttl_sec: float) -> bool:
    """跨进程启动锁：短时间内已有进程在拉起识图服务时返回 False，避免重复加载。"""

    try:
        _START_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        if _START_LOCK_PATH.exists() and time.time() - _START_LOCK_PATH.stat().st_mtime < ttl_sec:
            return False
        _START_LOCK_PATH.write_text(str(os.getpid()), encoding="utf-8")
        return True
    except OSError:
        return True


# ───────────────────────── 配置 ─────────────────────────


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.0.0", description="配置版本")


class CvConfig(PluginConfigBase):
    """识图工具与 /识图 指令。"""

    __ui_label__ = "识图"
    __ui_icon__ = "image"
    __ui_order__ = 1

    enabled: bool = Field(
        default=True,
        description="是否打开识图工具和 /识图 指令",
        json_schema_extra={"x-widget": "switch"},
    )
    threshold: float = Field(
        default=0.2,
        ge=0.05,
        le=0.99,
        description="置信度阈值，低于该值的结果不展示（数据集推荐 0.2）",
        json_schema_extra={"depends_on": "enabled", "depends_value": True},
    )
    top_k: int = Field(
        default=3,
        ge=1,
        le=10,
        description="一次最多展示几个角色",
        json_schema_extra={"depends_on": "enabled", "depends_value": True},
    )
    cooldown_sec: float = Field(
        default=30.0,
        ge=0.0,
        le=600.0,
        description="同一人两次 /识图 的最小间隔（秒），0=不限制",
        json_schema_extra={"depends_on": "enabled", "depends_value": True},
    )
    pending_ttl_sec: float = Field(
        default=120.0,
        ge=10.0,
        le=600.0,
        description="/识图 之后等待图片的秒数，超时则取消待图",
        json_schema_extra={"depends_on": "enabled", "depends_value": True},
    )


class CvServiceConfig(PluginConfigBase):
    """本地识图分类服务连接参数。"""

    __ui_label__ = "识图服务"
    __ui_icon__ = "server"
    __ui_order__ = 2

    api_url: str = Field(
        default="http://127.0.0.1:9881",
        description="识图分类服务地址",
    )
    request_timeout_sec: float = Field(
        default=30.0,
        ge=5.0,
        le=180.0,
        description="单次识别请求超时（秒）",
    )
    auto_start_api: bool = Field(
        default=True,
        description="识图服务没启动时自动拉起",
    )
    api_start_script: str = Field(
        default="",
        description="自动拉起用的启动脚本；留空使用插件内置 service/start_cls_api.bat",
    )
    api_start_timeout_sec: float = Field(
        default=120.0,
        ge=30.0,
        le=600.0,
        description="等待服务就绪的最长时间（秒）",
    )
    prewarm: bool = Field(
        default=True,
        description="宿主启动时预热识图服务：首次识图不用等冷启动，失败写日志",
    )


class MarisaCvConfig(PluginConfigBase):
    """魔理沙识图插件配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    cv: CvConfig = Field(default_factory=CvConfig)
    service: CvServiceConfig = Field(default_factory=CvServiceConfig)


# ───────────────────────── 插件主体 ─────────────────────────


class MarisaCvPlugin(MaiBotPlugin):
    """本地东方角色识图：工具 + /识图 待图流程。"""

    config_model = MarisaCvConfig

    def __init__(self) -> None:
        super().__init__()
        self._prewarm_task: Optional[asyncio.Task] = None
        # 待图状态：key = (session_id, platform:user_id)，值 = 过期时刻（monotonic）
        self._pending: Dict[str, float] = {}
        # 公开 /识图 的按人冷却：key 同上
        self._last_identify: Dict[str, float] = {}
        self._classify_sem: asyncio.Semaphore = asyncio.Semaphore(1)

    async def on_load(self) -> None:
        """插件加载：记录配置并预热服务。"""

        cv = self.config.cv
        self.ctx.logger.info(
            "[marisa_cv] 已加载：识图 %s，阈值 %.2f，最多 %s 个角色",
            "开启" if cv.enabled else "关闭",
            float(cv.threshold),
            int(cv.top_k),
        )
        if bool(self.config.service.prewarm):
            self._prewarm_task = asyncio.create_task(self._prewarm_service())

    async def on_unload(self) -> None:
        """插件卸载时取消后台预热任务。"""

        if self._prewarm_task is not None and not self._prewarm_task.done():
            self._prewarm_task.cancel()

    async def _prewarm_service(self) -> None:
        """宿主启动时预热识图服务，失败留下可见记录。"""

        svc = self.config.service
        base_url = str(svc.api_url or "").rstrip("/")
        if not base_url:
            return
        try:
            ok = await self._ensure_service(base_url, svc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.ctx.logger.warning("[marisa_cv] 识图服务预热异常: %s", exc)
            return
        if ok:
            self.ctx.logger.info("[marisa_cv] 识图服务预热完成")
        else:
            self.ctx.logger.warning(
                "[marisa_cv] 识图服务预热失败：启动输出见 %s；识图会暂时降级不可用", _API_LOG
            )

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        """配置热更新回调。"""

        del config_data, version
        try:
            cv = self.config.cv
            state = "开启" if cv.enabled else "关闭"
            threshold = float(cv.threshold)
        except Exception:
            self.ctx.logger.info("[marisa_cv] 配置已更新（scope=%s），强类型配置暂不可读", scope)
            return
        self.ctx.logger.info("[marisa_cv] 配置已更新（scope=%s）：识图 %s，阈值 %.2f", scope, state, threshold)

    # ───────── WebUI 首页入口 ─────────

    @HomeCard(
        "marisa_cv_entry",
        title="魔理沙识图",
        description="本地识别图片里的东方角色：认识 134 位角色，只回答「有谁」。",
        content=[
            {
                "type": "markdown",
                "content": "识图完全在本地跑：WD-ConvNeXt v3 骨干 + 数据集自带的 134 位东方角色"
                "分类头。魔理沙在聊天里可以主动调用识图工具；你也可以用 /识图 指令走确定性的"
                "「发指令→再发图」流程。",
            },
            {
                "type": "list",
                "items": [
                    "/识图：回到会话后发一张图，她会直接告诉你图里是谁",
                    "聊天里直接发图并问她时，她也可以自己调用识图工具",
                    "识别不到已知角色时会明说「没认出来」，不会瞎编",
                ],
            },
            {
                "type": "actions",
                "actions": [
                    {"label": "打开插件配置", "url": "/plugin-config?plugin=marisa-team.marisa-cv"},
                ],
            },
        ],
        link_url="/plugin-config?plugin=marisa-team.marisa-cv",
        link_label="配置魔理沙识图",
        width="medium",
        order=140,
    )
    async def home_entry(self) -> None:
        """声明 WebUI 首页入口卡片。"""

        return None

    # ───────── 核心：识图工具 ─────────

    @Tool(
        "marisa_identify_character",
        description=_TOOL_DESCRIPTION,
        parameters=[
            ToolParameterInfo(
                name="image_path",
                param_type=ToolParamType.STRING,
                description="本地图片路径；留空则自动取当前会话最近一张图",
                required=False,
            )
        ],
        visibility="visible",
    )
    async def tool_identify(self, image_path: str = "", **kwargs: Any) -> Dict[str, Any]:
        """识别图片里的东方角色。"""

        cfg = self.config.cv
        if not cfg.enabled:
            return {"success": False, "content": "识图功能当前已关闭，请直接用文字回复。"}

        path_text = str(image_path or "").strip()
        if path_text:
            image_base64 = self._image_base64_from_path(path_text)
            if not image_base64:
                return {"success": False, "content": f"读取不到图片文件：{path_text}"}
        else:
            stream_id = str((kwargs or {}).get("stream_id") or "")
            if not stream_id:
                return {
                    "success": False,
                    "content": "没有拿到会话上下文，无法自动取图；请在调用时传入 image_path。",
                }
            try:
                messages = await self.ctx.call_capability(
                    "message.get_recent",
                    chat_id=stream_id,
                    limit=10,
                    include_binary_data=True,
                )
            except Exception as exc:
                self.ctx.logger.warning("[marisa_cv] 获取最近消息失败: %s", exc)
                return {"success": False, "content": "读取聊天记录失败，请改用文字回复。"}
            image_base64 = self._latest_image_from_messages(messages)
            if not image_base64:
                return {
                    "success": False,
                    "content": "当前会话最近没有找到图片，请让用户先发一张图，或改用文字回复。",
                }

        async with self._classify_sem:
            result = await self._classify(image_base64)
        if result is None:
            return {"success": False, "content": "识图服务暂时不可用，请改用文字回复。"}

        characters = result.get("characters") or []
        if not characters:
            return {
                "success": True,
                "content": "识别完成：图里没有检测到数据集覆盖的东方角色（不认识的不要编造）。",
            }
        detail = "；".join(
            f"{item['name_zh']}({item['tag']}) 置信度 {item['confidence'] * 100:.0f}%" for item in characters
        )
        return {"success": True, "content": f"识别结果（可能同时有多个角色）：{detail}"}

    # ───────── 指令：/识图 待图流程 ─────────

    @Command(
        "marisa_cv_identify",
        description="让魔理沙看图认人：先发 /识图，再发图片（所有人可用）",
        pattern=cmd_pattern(r"/(?:识图|认图|认人|看图)\s*$"),
    )
    async def cmd_identify(self, **kwargs: Any) -> tuple:
        if not isinstance(kwargs, dict):
            return False, "", False

        cfg = self.config.cv
        session_id = str(kwargs.get("stream_id") or "")
        if not cfg.enabled:
            if session_id:
                await self.ctx.send.text("识图功能现在关着だぜ，让主人去插件配置里打开吧。", session_id)
            return True, "", True

        platform = str(kwargs.get("platform") or "?")
        user_id = str(kwargs.get("user_id") or "?")
        key = self._pending_key(session_id, platform, user_id)

        # 公开指令：按调用者冷却，防止群友连续刷识别
        cooldown = float(cfg.cooldown_sec)
        if cooldown > 0 and user_id != "?":
            now = time.monotonic()
            wait = cooldown - (now - self._last_identify.get(key, 0.0))
            if wait > 0:
                if session_id:
                    await self.ctx.send.text(f"别急だぜ，等 {wait:.0f} 秒再让我看图～", session_id)
                return True, "", True
            self._last_identify[key] = now

        self._pending[key] = time.monotonic() + float(cfg.pending_ttl_sec)
        if session_id:
            await self.ctx.send.text("发图来吧だぜ，我帮你看看图里是谁～", session_id)
        return True, "", True

    # ───────── 钩子：待图消息确定性拦截 ─────────

    @HookHandler(
        "chat.receive.before_process",
        name="marisa_cv_pending_image",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
    )
    async def handle_pending_image(self, **kwargs: Any) -> Dict[str, Any]:
        """处于待图状态时，截获随后的图片消息并直接识别回复。"""

        cont = {"action": "continue", "modified_kwargs": kwargs}
        try:
            cfg = self.config.cv
            if not cfg.enabled:
                return cont
            message = kwargs.get("message")
            if not isinstance(message, dict) or bool(message.get("is_notify")):
                return cont

            session_id = str(message.get("session_id") or "")
            platform = str(message.get("platform") or "")
            user_info = ((message.get("message_info") or {}).get("user_info") or {})
            user_id = str(user_info.get("user_id") or "")
            if not session_id or not user_id:
                return cont

            key = self._pending_key(session_id, platform, user_id)
            expire_at = self._pending.get(key, 0.0)
            if expire_at <= time.monotonic():
                if key in self._pending:
                    self._pending.pop(key, None)
                return cont

            image_base64 = self._image_from_components(message.get("raw_message"))
            if not image_base64:
                # 可能是紧接着的纯文字消息：保持待图状态，交给主链正常处理
                return cont

            self._pending.pop(key, None)
            self._last_identify[key] = time.monotonic()
            async with self._classify_sem:
                result = await self._classify(image_base64)
            await self.ctx.send.text(self._format_reply(result), session_id)
            return {"action": "abort"}
        except Exception as exc:
            self.ctx.logger.warning("[marisa_cv] 待图识别失败: %s", exc)
            return cont

    # ───────── 图片来源 ─────────

    @staticmethod
    def _pending_key(session_id: str, platform: str, user_id: str) -> str:
        """待图/冷却状态键：同一会话 + 同一发送人。"""

        return f"{session_id}::{platform}:{user_id}"

    @staticmethod
    def _image_from_components(components: Any) -> str:
        """从消息组件列表里取最后一张图片的 base64。"""

        if not isinstance(components, list):
            return ""
        for component in reversed(components):
            if not isinstance(component, dict):
                continue
            if str(component.get("type") or "") != "image":
                continue
            data = str(component.get("binary_data_base64") or "").strip()
            if data:
                return data
        return ""

    def _latest_image_from_messages(self, messages: Any) -> str:
        """从消息列表里找最近一条带图消息的 base64。"""

        if not isinstance(messages, list):
            return ""
        for message in reversed(messages):
            if not isinstance(message, dict):
                continue
            data = self._image_from_components(message.get("raw_message"))
            if data:
                return data
        return ""

    @staticmethod
    def _image_base64_from_path(path_text: str) -> str:
        """读取本地图片文件并编码为 base64（失败返回空串）。"""

        path = Path(path_text)
        if not path.is_file():
            return ""
        try:
            return base64.b64encode(path.read_bytes()).decode("ascii")
        except OSError:
            return ""

    # ───────── 识别与回复 ─────────

    async def _classify(self, image_base64: str) -> Optional[Dict[str, Any]]:
        """调用本地识图服务，失败返回 None。"""

        svc = self.config.service
        cfg = self.config.cv
        base_url = str(svc.api_url or "").rstrip("/")
        if not base_url:
            self.ctx.logger.warning("[marisa_cv] 未配置识图服务地址")
            return None
        if not await self._ensure_service(base_url, svc):
            return None

        payload = {
            "image_base64": image_base64,
            "threshold": float(cfg.threshold),
            "top_k": int(cfg.top_k),
        }
        try:
            async with httpx.AsyncClient(timeout=float(svc.request_timeout_sec)) as client:
                resp = await client.post(f"{base_url}/classify", json=payload)
        except Exception as exc:
            self.ctx.logger.warning("[marisa_cv] 识图请求失败: %s", exc)
            return None

        if resp.status_code != 200:
            self.ctx.logger.warning(
                "[marisa_cv] 识图返回异常: HTTP %s %s", resp.status_code, resp.text[:160]
            )
            return None
        try:
            return resp.json()
        except Exception as exc:
            self.ctx.logger.warning("[marisa_cv] 识图返回不是 JSON: %s", exc)
            return None

    @staticmethod
    def _format_characters(characters: Any) -> str:
        """把角色列表拼成「名字（xx%）」样式的文本。"""

        parts = []
        for item in characters or []:
            if not isinstance(item, dict):
                continue
            parts.append(f"{item.get('name_zh') or item.get('tag')}（{float(item.get('confidence', 0.0)) * 100:.0f}%）")
        return "、".join(parts)

    def _format_reply(self, result: Optional[Dict[str, Any]]) -> str:
        """把识别结果拼成给聊天用的回复。"""

        if result is None:
            return "识图服务没起来だぜ……主人看看 data/anime_cv.log 吧。"
        characters = (result or {}).get("characters") or []
        if not characters:
            return "看了一圈，没认出图里有啥东方角色呢……"
        return f"我看看……图里是{self._format_characters(characters)}だぜ！"

    # ───────── 服务管理 ─────────

    async def _service_alive(self, base_url: str) -> bool:
        """探测服务是否存活。"""

        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                await client.get(f"{base_url}/")
            return True
        except Exception:
            return False

    async def _ensure_service(self, base_url: str, svc: CvServiceConfig) -> bool:
        """服务没起来时按配置自动拉起并等待就绪。"""

        if await self._service_alive(base_url):
            return True
        if not svc.auto_start_api:
            self.ctx.logger.warning("[marisa_cv] 识图服务未启动，且未开启自动拉起")
            return False

        script = str(svc.api_start_script or "").strip()
        script_path = Path(script) if script else _DEFAULT_START_SCRIPT
        if not script_path.is_file():
            self.ctx.logger.warning("[marisa_cv] 找不到启动脚本: %s", script_path)
            return False

        timeout_sec = float(svc.api_start_timeout_sec)
        # 桌宠与宿主可能同时发现服务掉线：拿不到锁说明已有进程在拉，等它就好
        if _try_acquire_start_lock(timeout_sec):
            self._spawn_api(script_path)
        else:
            self.ctx.logger.info("[marisa_cv] 识图服务已在拉起中，等待就绪…")

        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            await asyncio.sleep(2)
            if await self._service_alive(base_url):
                self.ctx.logger.info("[marisa_cv] 识图服务已就绪")
                return True
        self.ctx.logger.warning(
            "[marisa_cv] 识图服务未在 %.0f 秒内就绪，启动输出见 %s", timeout_sec, _API_LOG
        )
        return False

    def _spawn_api(self, script_path: Path) -> None:
        """拉起识图服务。

        与语音服务相同的引号写法：路径里带 ``(1)`` 这类括号时，普通
        ``cmd /c 脚本`` 会被 cmd 当成命令分隔符截断，进程秒退且没有任何输出。
        """

        try:
            subprocess.Popen(
                f'cmd /d /s /c ""{script_path}""',
                cwd=str(script_path.parent),
                creationflags=0x08000000,
                close_fds=True,
                stdout=_open_api_log(),
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )
            self.ctx.logger.info("[marisa_cv] 正在拉起识图服务: %s", script_path)
        except Exception as exc:
            self.ctx.logger.warning("[marisa_cv] 拉起识图服务失败: %s", exc)


def create_plugin() -> MarisaCvPlugin:
    """创建插件实例。"""

    return MarisaCvPlugin()
