"""魔理沙语音插件。

让魔理沙**自己决定**什么时候用语音说话，而不是被插件偷偷把文字换成语音。

实现方式：注册一个对 LLM 可见的 ``marisa_say`` 工具。模型判断该开口时调用它，
插件用本地 GPT-SoVITS（当前音色）合成，再通过 NapCat 发成语音条；工具返回
``stop_after_execution=True`` 结束本轮规划，所以不会再补一条文字——真正做到
"要么打字、要么说话"。

为什么不用"拦截出站消息再改写成语音"：那样模型在历史里只看得到自己调了 reply
发文字（`reply` 工具的结果写着原文），插件在 send_service 层的改写完全在历史之外，
它既不知道自己发过语音，也无从判断何时该开口。
"""

from __future__ import annotations

from errno import EACCES, EDEADLK
from pathlib import Path
from typing import Any, Dict, Literal, Optional

from maibot_sdk import Command, Field, HomeCard, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

import asyncio
import base64
import io
import msvcrt
import os
import re
import subprocess
import time
import unicodedata
import uuid
import wave
import httpx

_PLUGIN_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _PLUGIN_DIR.parent.parent
# 语音服务的启动输出与跨进程启动锁放在工程 data 下，桌宠侧也写同一个日志文件
_VOICE_API_LOG = _PROJECT_ROOT / "data" / "voice_api.log"
_START_LOCK_PATH = _PROJECT_ROOT / "data" / "voice_api.start.lock"


def _open_voice_log() -> Any:
    """打开语音服务输出日志（追加）；文件错误直接暴露，避免丢失失败原因。"""

    _VOICE_API_LOG.parent.mkdir(parents=True, exist_ok=True)
    return open(_VOICE_API_LOG, "ab")


def _try_acquire_start_lock(ttl_sec: float) -> bool:
    """跨进程启动锁：短时间内已有进程在拉起语音服务时返回 False，避免重复加载抢显存。"""

    _START_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    # 系统文件锁保护时间戳的检查与更新，过期锁竞争也不能重复拉起服务。
    with _START_LOCK_PATH.open("a+b") as lock_file:
        lock_file.seek(0)
        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in (EACCES, EDEADLK):
                return False
            raise
        try:
            if lock_file.read() and time.time() - os.fstat(lock_file.fileno()).st_mtime < ttl_sec:
                return False
            lock_file.seek(0)
            lock_file.truncate()
            lock_file.write(str(os.getpid()).encode("ascii"))
            lock_file.flush()
            return True
        finally:
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)

# 日文假名区段（平假名 / 片假名 / 假名扩展 / 半角片假名）。
# 当前音色模型是纯中文模型，没有日文前端，假名会被当中文音素错误朗读，送合成前一律剔除。
_KANA_RANGES = ((0x3040, 0x309F), (0x30A0, 0x30FF), (0x31F0, 0x31FF), (0xFF66, 0xFF9D))
_DROP_CATEGORIES = frozenset({"So", "Sk", "Cs", "Co", "Cn", "Cf"})

# 生成退化判定：正常中文语速约 0.2-0.3 秒/字。服务进程状态劣化时模型不吐结束符，
# 会一路生成到长度上限，输出超长且是咕噜声。
_DEGENERATE_MIN_SEC = 6.0
_DEGENERATE_SEC_PER_CHAR = 0.75

# 同时进行的合成数上限，多群同时触发时不至于把 GPU 打满
_SYNTH_CONCURRENCY = 1

_AT_MARK = r"(?:\s*〔[^〕]*〕\s*)?(?:\s*(?:\[At:[^\]]*\]|@[\w\u4e00-\u9fa5]+)\s*)*"
_TRIGGER = r"(?:魔理沙[，,、\s]*)?"

_DEFAULT_USE_HINT = "当用户要求用语音、或闲聊里想让回复更生动时使用；纯信息性/指令类的长回答不要用。"
_DEFAULT_LENGTH_HINT = "适合 3-60 字的口语短句，太长会合成很久，太短没必要。"


def cmd_pattern(inner: str) -> str:
    """统一命令正则前缀：容忍 @At 与「魔理沙」前缀。"""
    return "^" + _AT_MARK + _TRIGGER + "(?P<raw>" + inner + ")$"


def sanitize_voice_text(text: str) -> str:
    """剔除假名与 emoji，折叠空白，得到中文语音模型能正确朗读的文本。"""
    if not text:
        return ""
    kept = []
    for ch in text:
        code = ord(ch)
        if any(lo <= code <= hi for lo, hi in _KANA_RANGES):
            continue
        if unicodedata.category(ch) in _DROP_CATEGORIES:
            continue
        kept.append(ch)

    cleaned = "".join(kept)
    cleaned = re.sub(r"\s*\n+\s*", "，", cleaned)
    cleaned = re.sub(r"[ \t\u3000]+", " ", cleaned)
    cleaned = re.sub(r"([，。！？、,.!?])\1+", r"\1", cleaned)
    return cleaned.strip()


def _load_tool_hints() -> tuple[str, str]:
    """在导入期读取 config.toml 的提示语，用来拼装工具描述。

    工具的 description 在组件注册（插件加载）阶段就被读取并固定，装饰器又是在
    类定义时求值的，拿不到 self.config，所以这里在导入期读一次文件；改完提示语
    需要在 WebUI 里热重载插件才能生效。
    """
    try:
        import tomllib

        with open(_PLUGIN_DIR / "config.toml", "rb") as fp:
            data = tomllib.load(fp)
        voice = data.get("voice") or {}
        use_hint = str(voice.get("use_hint") or "").strip() or _DEFAULT_USE_HINT
        length_hint = str(voice.get("length_hint") or "").strip() or _DEFAULT_LENGTH_HINT
        return use_hint, length_hint
    except Exception:
        return _DEFAULT_USE_HINT, _DEFAULT_LENGTH_HINT


_USE_HINT, _LENGTH_HINT = _load_tool_hints()

_TOOL_DESCRIPTION = (
    "用魔理沙自己的声音（本地 GPT-SoVITS 音色）把一句话说出去，聊天里会收到一条语音。"
    f"{_USE_HINT}{_LENGTH_HINT}"
    "【重要】调用本工具后本轮回复就此结束，不会再额外发送文字，"
    "所以 text 必须是完整的、可以直接说给对方听的回复内容（口语化，不要 markdown、表情符号或舞台提示）。"
)


# ───────────────────────── 配置 ─────────────────────────


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=False, description="是否启用插件")
    config_version: str = Field(default="1.2.1", description="配置版本")


class VoiceConfig(PluginConfigBase):
    """语音工具：让魔理沙自己决定什么时候开口。"""

    __ui_label__ = "语音工具"
    __ui_icon__ = "volume-2"
    __ui_order__ = 1

    enabled: bool = Field(
        default=True,
        description="是否把 marisa_say 工具暴露给魔理沙（关掉后它只能打字）",
        json_schema_extra={"x-widget": "switch"},
    )
    min_chars: int = Field(
        default=3,
        ge=1,
        le=20,
        description="短于这个字数就让模型改用文字（太短没必要发语音）",
        json_schema_extra={"depends_on": "enabled", "depends_value": True},
    )
    max_chars: int = Field(
        default=60,
        ge=10,
        le=300,
        description="超过这个字数就让模型改用文字（合成太慢，会让人干等）",
        json_schema_extra={"depends_on": "enabled", "depends_value": True},
    )
    cooldown_sec: float = Field(
        default=30.0,
        ge=0.0,
        le=600.0,
        description="同一人两次 /语音 的最小间隔（秒），0=不限制；防止群友刷合成队列",
        json_schema_extra={"depends_on": "enabled", "depends_value": True},
    )
    send_mode: Literal["base64", "file"] = Field(
        default="base64",
        description="base64=内联音频数据（协议端无需访问本机文件，会写进对话历史）；file=只发本地路径，仅当协议端与本机共享文件系统时可用，容器化协议端会报「路径不存在」",
        json_schema_extra={"depends_on": "enabled", "depends_value": True},
    )
    use_hint: str = Field(
        default=_DEFAULT_USE_HINT,
        description="写进工具描述的「什么时候该用语音」引导语（改后需热重载插件才生效）",
        json_schema_extra={"depends_on": "enabled", "depends_value": True, "rows": 3},
    )
    length_hint: str = Field(
        default=_DEFAULT_LENGTH_HINT,
        description="写进工具描述的长度建议（改后需热重载插件才生效）",
        json_schema_extra={"depends_on": "enabled", "depends_value": True, "rows": 2},
    )


class ServiceConfig(PluginConfigBase):
    """本地 GPT-SoVITS 语音服务连接参数。"""

    __ui_label__ = "语音服务"
    __ui_icon__ = "server"
    __ui_order__ = 2

    api_url: str = Field(
        default="http://127.0.0.1:9880",
        description="GPT-SoVITS API 地址",
    )
    ref_audio_path: str = Field(
        default=r"D:\GPT-SoVITS-v4-20250421fix(1)\GPT-SoVITS-v4-20250422fix\output\model.wav",
        description="参考音频路径（3-10 秒），决定音色",
    )
    prompt_text: str = Field(
        default="而且这次不一样，动机和证据我都凑齐了。",
        description="参考音频里说的那句话，必须逐字一致",
    )
    prompt_lang: str = Field(default="zh", description="参考文本语种")
    text_lang: str = Field(default="zh", description="合成文本语种")
    speed_factor: float = Field(default=1.0, ge=0.5, le=2.0, description="语速倍率")
    synthesize_timeout_sec: float = Field(
        default=45.0,
        ge=5.0,
        le=180.0,
        description="单次合成超时（秒）；超时就告诉模型改用文字，不会把回复卡没",
    )
    auto_start_api: bool = Field(
        default=True,
        description="语音服务没启动时自动拉起（首次加载模型约需一分钟）",
    )
    api_start_script: str = Field(
        default=r"D:\GPT-SoVITS-v4-20250421fix(1)\GPT-SoVITS-v4-20250422fix\run_api.bat",
        description="自动拉起用的启动脚本",
    )
    api_start_timeout_sec: float = Field(
        default=180.0,
        ge=30.0,
        le=600.0,
        description="等待服务就绪的最长时间（秒）",
    )
    prewarm: bool = Field(
        default=False,
        description="宿主启动时预热语音服务：首句不用等模型加载，启动失败会写日志并给桌宠提示",
    )


class MarisaVoiceConfig(PluginConfigBase):
    """魔理沙语音插件配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    service: ServiceConfig = Field(default_factory=ServiceConfig)


# ───────────────────────── 插件主体 ─────────────────────────


class MarisaVoicePlugin(MaiBotPlugin):
    """让魔理沙自己决定何时用语音说话。"""

    config_model = MarisaVoiceConfig

    def __init__(self) -> None:
        super().__init__()
        self._synth_sem: asyncio.Semaphore = asyncio.Semaphore(_SYNTH_CONCURRENCY)
        self._prewarm_task: Optional[asyncio.Task] = None
        # 公开 /语音 的按人冷却：key = "platform:user_id"
        self._last_say: Dict[str, float] = {}

    async def on_load(self) -> None:
        voice = self.config.voice
        self.ctx.logger.info(
            "[marisa_voice] 已加载：语音工具 %s，发送方式 %s，长度 %s-%s 字",
            "开启" if voice.enabled else "关闭",
            voice.send_mode,
            voice.min_chars,
            voice.max_chars,
        )
        if bool(self.config.service.prewarm):
            self._prewarm_task = asyncio.create_task(self._prewarm_service())

    async def on_unload(self) -> None:
        """插件卸载时无需清理后台任务。"""
        if self._prewarm_task is not None and not self._prewarm_task.done():
            self._prewarm_task.cancel()

    async def _prewarm_service(self) -> None:
        """宿主启动时预热语音服务：首句不用再等模型加载，失败也留下可见记录。"""

        svc = self.config.service
        base_url = str(svc.api_url or "").rstrip("/")
        if not base_url:
            return
        try:
            ok = await self._ensure_service(base_url, svc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.ctx.logger.warning("[marisa_voice] 语音服务预热异常: %s", exc)
            return
        if ok:
            self.ctx.logger.info("[marisa_voice] 语音服务预热完成")
            return
        self.ctx.logger.warning(
            "[marisa_voice] 语音服务预热失败：启动输出见 %s；桌宠说话与 /语音 会降级为文字",
            _VOICE_API_LOG,
        )
        await self._notify_pet(f"语音服务没起来だぜ……主人看看 {_VOICE_API_LOG} 吧。")

    async def _notify_pet(self, text: str) -> None:
        """通过桌面适配器的公开 API 给桌宠推一条气泡（桌宠不在线则忽略）。"""

        try:
            await self.ctx.api.call(
                "marisa-team.marisa-desktop.push_pet_frame",
                frame={"type": "bot_message", "text": text, "message_id": "", "raw_message": []},
            )
        except Exception:
            pass

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        """配置热更新回调。

        提示语（use_hint / length_hint）是在导入期拼进工具描述的，改这两项需要
        重载插件才会生效；其余配置每次调用时现取，立即生效。
        """
        del config_data, version
        try:
            voice = self.config.voice
            state = "开启" if voice.enabled else "关闭"
            send_mode = voice.send_mode
        except Exception:
            self.ctx.logger.info("[marisa_voice] 配置已更新（scope=%s），强类型配置暂不可读", scope)
            return
        self.ctx.logger.info(
            "[marisa_voice] 配置已更新（scope=%s）：语音工具 %s，发送方式 %s", scope, state, send_mode
        )

    # ───────── WebUI 首页入口 ─────────

    @HomeCard(
        "marisa_voice_entry",
        title="魔理沙语音",
        description="魔理沙自己决定什么时候用当前音色发语音条。",
        content=[
            {
                "type": "markdown",
                "content": "语音由魔理沙主动选择：它想让回复更生动、或你要求用语音时，会调用语音工具说话，"
                "这一轮就不会再发文字。合成失败或内容长度不合适时，它会自动改用文字。",
            },
            {
                "type": "list",
                "items": [
                    "/语音 <文本>：立刻用当前音色念一段话（用来验证链路）",
                    "工具描述里的「什么时候用语音」「多长合适」可在插件配置里改",
                ],
            },
            {
                "type": "actions",
                "actions": [
                    {"label": "打开插件配置", "url": "/plugin-config?plugin=marisa.voice"},
                ],
            },
        ],
        link_url="/plugin-config?plugin=marisa.voice",
        link_label="配置魔理沙语音",
        width="medium",
        order=130,
    )
    async def home_entry(self) -> None:
        """声明 WebUI 首页入口卡片。"""

        return None

    # ───────── 核心：让模型主动开口的工具 ─────────

    @Tool(
        "marisa_say",
        description=_TOOL_DESCRIPTION,
        parameters=[
            ToolParameterInfo(
                name="text",
                param_type=ToolParamType.STRING,
                description="要念出来的完整回复内容（口语化短句，不要 markdown、表情符号或括号动作描写）",
                required=True,
            )
        ],
        visibility="visible",
    )
    async def tool_say(self, text: str = "", **kwargs: Any) -> Dict[str, Any]:
        """把一句话用当前音色说出去；成功时终止本轮规划，不再补文字。"""
        cfg = self.config.voice
        if not cfg.enabled:
            return {"success": False, "content": "语音功能当前已关闭，请直接用文字回复。"}

        spoken = sanitize_voice_text(str(text or ""))
        if not spoken:
            return {"success": False, "content": "没有可朗读的内容，请直接用文字回复。"}
        if len(spoken) < int(cfg.min_chars):
            return {"success": False, "content": f"「{spoken}」太短了，不适合发语音，请直接用文字回复。"}
        if len(spoken) > int(cfg.max_chars):
            return {
                "success": False,
                "content": (
                    f"这段话 {len(spoken)} 字，超过 {int(cfg.max_chars)} 字上限，合成语音太慢。"
                    "请精简后重试，或直接用文字回复。"
                ),
            }

        stream_id = str((kwargs or {}).get("stream_id") or "")
        if not stream_id:
            return {"success": False, "content": "没有拿到会话上下文，无法发送语音，请直接用文字回复。"}

        voice_path = await self._synthesize(spoken, float(self.config.service.synthesize_timeout_sec))
        if not voice_path:
            return {
                "success": False,
                "content": "语音合成失败（服务可能未启动、或参考音频配置有误），请直接用文字回复。",
            }

        if not await self._send_voice(stream_id, voice_path):
            return {"success": False, "content": "语音发送失败，请直接用文字回复。"}

        self.ctx.logger.info("[marisa_voice] 已发送语音: %s", spoken[:40])
        # content 会写进 tool 历史——模型由此知道自己在后续对话里说过什么
        return {
            "success": True,
            "content": f"已用魔理沙的声音说出：「{spoken}」",
            "stop_after_execution": True,
        }

    # ───────── 命令：手动验证链路 ─────────

    @Command(
        "marisa_voice_say",
        description="让魔理沙用当前音色念一段话：/语音 <文本>（所有人可用）",
        pattern=cmd_pattern(r"/(?:语音|说话|朗读)(?:\s+[\s\S]+)?\s*$"),
    )
    async def cmd_say(self, **kwargs: Any) -> tuple:
        if not isinstance(kwargs, dict):
            return False, "", False

        stream_id = str(kwargs.get("stream_id") or "")
        matched = kwargs.get("matched_groups")
        raw = str((matched or {}).get("raw") or kwargs.get("text") or "") if isinstance(matched, dict) else ""
        m = re.match(r"^/(?:语音|说话|朗读)\s*([\s\S]*)$", raw.strip())
        text = (m.group(1) if m else "").strip()

        if not text:
            if stream_id:
                await self.ctx.send.text("想让我说什么？用法：/语音 今天也要元气满满！", stream_id)
            return True, "", True

        spoken = sanitize_voice_text(text)
        if not spoken:
            if stream_id:
                await self.ctx.send.text("这段话没有可朗读的内容呢……", stream_id)
            return True, "", True

        # 公开指令：按调用者做冷却，防止群友连续刷合成队列
        cooldown = float(self.config.voice.cooldown_sec)
        caller_key = f"{kwargs.get('platform') or '?'}:{kwargs.get('user_id') or '?'}"
        if cooldown > 0 and caller_key != "?:?":
            now = time.monotonic()
            wait = cooldown - (now - self._last_say.get(caller_key, 0.0))
            if wait > 0:
                if stream_id:
                    await self.ctx.send.text(f"别急だぜ，等 {wait:.0f} 秒再让我说～", stream_id)
                return True, "", True
            self._last_say[caller_key] = now

        voice_path = await self._synthesize(spoken, float(self.config.service.synthesize_timeout_sec))
        if not voice_path:
            if stream_id:
                await self.ctx.send.text("语音没合成出来……检查下语音服务是否启动、参考音频配置对不对。", stream_id)
            return True, "", True

        if not await self._send_voice(stream_id, voice_path) and stream_id:
            await self.ctx.send.text("语音发送失败了……", stream_id)
        return True, "", True

    # ───────── 发送 ─────────

    async def _send_voice(self, stream_id: str, voice_path: str) -> bool:
        """按配置的方式把语音发到会话里。"""
        if str(self.config.voice.send_mode).lower() == "base64":
            try:
                encoded = base64.b64encode(Path(voice_path).read_bytes()).decode("ascii")
            except Exception as exc:
                self.ctx.logger.warning("[marisa_voice] 读取音频失败: %s", exc)
                return False
            # voice 是消息一等公民，历史里渲染成干净的 [语音消息]，所以这里同步进历史
            return bool(
                await self.ctx.send.custom(
                    "voice",
                    encoded,
                    stream_id,
                    sync_to_maisaka_history=True,
                    maisaka_source_kind="guided_reply",
                )
            )
        # 非 base64 走 voiceurl + 本地路径：这条只有在协议端与本机共享文件系统时才成立
        # （历史上 NapCat 与宿主同机，所以当时"最稳"）。协议端一旦容器化，它拿到的是
        # file://D:\... 这样的宿主机路径，会直接报「路径不存在」——所以默认已是 base64。
        # voiceurl 组件在历史里会被渲染成 [voiceurl消息]，因此不同步进历史：
        # 模型对"我说过什么"的认知由工具结果提供。
        return bool(await self.ctx.send.custom("voiceurl", {"url": voice_path}, stream_id))

    # ───────── 语音合成 ─────────

    async def _synthesize(self, text: str, timeout_sec: float) -> Optional[str]:
        """工具和公开指令共用合成队列，避免同时推理挤占内存与显存。"""
        async with self._synth_sem:
            return await self._synthesize_serial(text, timeout_sec)

    async def _synthesize_serial(self, text: str, timeout_sec: float) -> Optional[str]:
        """调用本地 GPT-SoVITS 合成语音，落盘后返回绝对路径（失败返回 None）。"""
        svc = self.config.service
        ref_audio = str(svc.ref_audio_path or "").strip().replace("\\", "/")
        prompt_text = str(svc.prompt_text or "").strip()
        if not ref_audio or not prompt_text:
            self.ctx.logger.warning("[marisa_voice] 参考音频或参考文本未填写，无法合成")
            return None
        if not Path(ref_audio).exists():
            self.ctx.logger.warning("[marisa_voice] 参考音频不存在: %s", ref_audio)
            return None

        base_url = str(svc.api_url or "").rstrip("/")
        if not base_url:
            self.ctx.logger.warning("[marisa_voice] 未配置语音服务地址")
            return None
        if not await self._ensure_service(base_url, svc):
            return None

        payload = {
            "text": text,
            "text_lang": str(svc.text_lang or "zh"),
            "ref_audio_path": ref_audio,
            "prompt_text": prompt_text,
            "prompt_lang": str(svc.prompt_lang or "zh"),
            "text_split_method": "cut5",
            "batch_size": 1,
            "media_type": "wav",
            "streaming_mode": False,
            "speed_factor": float(svc.speed_factor),
        }

        timeout = max(float(svc.synthesize_timeout_sec), timeout_sec)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(f"{base_url}/tts", json=payload)
        except Exception as exc:
            self.ctx.logger.warning("[marisa_voice] 合成请求失败: %s", exc)
            return None

        if resp.status_code != 200 or len(resp.content) < 100:
            self.ctx.logger.warning(
                "[marisa_voice] 合成返回异常: HTTP %s %s", resp.status_code, resp.text[:120]
            )
            return None

        try:
            with wave.open(io.BytesIO(resp.content), "rb") as wf:
                seconds = wf.getnframes() / float(wf.getframerate() or 1)
        except Exception as exc:
            self.ctx.logger.warning("[marisa_voice] 返回内容不是有效音频: %s", exc)
            return None

        limit = max(_DEGENERATE_MIN_SEC, len(text) * _DEGENERATE_SEC_PER_CHAR)
        if seconds > limit:
            self.ctx.logger.warning(
                "[marisa_voice] 合成退化：%s 字得到 %.1f 秒（上限 %.1f），改为让模型用文字",
                len(text),
                seconds,
                limit,
            )
            return None

        out_dir = _PLUGIN_DIR / "data" / "tts"
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            _prune_cache(out_dir)
            out_path = (out_dir / f"tts_{uuid.uuid4().hex[:12]}.wav").resolve()
            out_path.write_bytes(resp.content)
        except Exception as exc:
            self.ctx.logger.warning("[marisa_voice] 语音落盘失败: %s", exc)
            return None
        return out_path.as_posix()

    async def _service_alive(self, base_url: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                await client.get(f"{base_url}/")
            return True
        except Exception:
            return False

    async def _ensure_service(self, base_url: str, svc: ServiceConfig) -> bool:
        """服务没起来时按配置自动拉起并等待就绪（启动输出写入 data/voice_api.log）。"""
        if await self._service_alive(base_url):
            return True
        if not svc.auto_start_api:
            self.ctx.logger.warning("[marisa_voice] 语音服务未启动，且未开启自动拉起")
            return False

        script = str(svc.api_start_script or "").strip()
        if not script or not Path(script).exists():
            self.ctx.logger.warning("[marisa_voice] 找不到启动脚本: %s", script or "(未配置)")
            return False

        timeout_sec = float(svc.api_start_timeout_sec)
        # 桌宠与宿主可能同时发现服务掉线：拿不到锁说明已有进程在拉，等它就好
        if _try_acquire_start_lock(timeout_sec):
            self._spawn_voice_api(script)
        else:
            self.ctx.logger.info("[marisa_voice] 语音服务已在拉起中，等待就绪…")

        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            await asyncio.sleep(3)
            if await self._service_alive(base_url):
                self.ctx.logger.info("[marisa_voice] 语音服务已就绪")
                return True
        self.ctx.logger.warning(
            "[marisa_voice] 语音服务未在 %.0f 秒内就绪，启动输出见 %s",
            timeout_sec,
            _VOICE_API_LOG,
        )
        return False

    def _spawn_voice_api(self, script: str) -> None:
        """拉起 GPT-SoVITS。

        必须用 ``cmd /d /s /c ""脚本""`` 的双引号写法：路径里只要带 ``(1)`` 这类括号，
        普通 ``cmd /c 脚本`` 会被 cmd 当成命令分隔符截断，进程秒退且没有任何输出。
        """

        try:
            with _open_voice_log() as log_file:
                subprocess.Popen(
                    f'cmd /d /s /c ""{script}""',
                    cwd=str(Path(script).parent),
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    close_fds=True,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                )
            self.ctx.logger.info("[marisa_voice] 正在拉起语音服务: %s", script)
        except Exception as exc:
            self.ctx.logger.warning("[marisa_voice] 拉起语音服务失败: %s", exc)


def _prune_cache(out_dir: Path, keep_hours: float = 24.0) -> None:
    """清理过期的语音缓存，避免无限堆积。"""
    deadline = time.time() - keep_hours * 3600
    for old in out_dir.glob("tts_*.wav"):
        try:
            if old.stat().st_mtime < deadline:
                old.unlink()
        except Exception:
            continue


def create_plugin() -> MarisaVoicePlugin:
    """创建插件实例。"""
    return MarisaVoicePlugin()
