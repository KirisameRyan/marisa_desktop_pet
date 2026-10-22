# -*- coding: utf-8 -*-
"""魔理沙守门人：MaiBot 中枢插件

职责：
1. chat.receive.before_process 入口门控（确定性规则，主链安静）：
   - 私聊：仅主人(owners)放行；他人直接吞掉
   - 群：所有群都开放，但消息必须 @ 提到 bot 或明确说出「魔理沙/Marisa」才放行
     （group_mode=whitelist 时可退回仅白名单群）；其余吞掉
   - 非 QQ 平台默认放行（不影响其它平台）
2. 给放行的消息打身份隐形标记：主人消息=〔主人〕，群友消息=〔群友〕，供 AI 识别说话者
3. 跨端好感度服务：单一 json 事实源（与桌面/QQ 同一颗大脑共用）
4. 显式回图指令：/截图（QQ 收图需要显式触发，permission=operator 由宿主鉴权）
5. 远程终端家族（/终端 · /关闭电脑 · /取消关机）确定性执行与提案-确认

命令级门控已移交 MaiBot 原生体系：桌宠 /指令由 marisa_desktop 注册为
permission="operator" 的 @Command，宿主按 plugin.permission 名单先行拦截；
工具层另有运行时身份校验兜底。本插件不再维护敏感词黑名单。
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_PLUGIN_DIR = str(Path(__file__).resolve().parent)
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)
import cleanup_plan as CP  # noqa: E402
import disk_scan as DS  # noqa: E402
import plan_card as PC  # noqa: E402
import shell_exec as SE  # noqa: E402

from maibot_sdk import API, Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool  # noqa: E402
from maibot_sdk.types import HookMode, HookOrder, ToolParameterInfo, ToolParamType  # noqa: E402
from PIL import ImageGrab  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
MARISA_DATA_DIR = PROJECT_ROOT / "data" / "marisa"

PREFIX_WORDS = ("魔理沙", "雾雨魔理沙", "marisa", "Marisa", "MARISA", "mrs")

# 群友/主人敏感词黑名单与斜杠白名单已退役：命令级门控移交宿主 plugin.permission
# （permission="operator" 原生拦截），工具层另有运行时身份校验兜底。

class FavorabilityStore:
    """跨端好感度单源存储（QQ 与桌面端共用）。"""

    def __init__(self, seed: int = 51) -> None:
        self._path = MARISA_DATA_DIR / "favorability.json"
        self._seed = int(seed)
        self._lock = threading.RLock()
        MARISA_DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._data = self._load_or_seed()

    def _load_or_seed(self) -> Dict[str, Any]:
        if self._path.exists():
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        data = {
            "favorability": self._seed,
            "total_interactions": 0,
            "history": {},
            "updated_at": time.time(),
        }
        self._save(data)
        return data

    def _save(self, data: Dict[str, Any]) -> None:
        try:
            with self._lock:
                with open(self._path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def get(self) -> int:
        return int(self._data.get("favorability", self._seed))

    def add(self, delta: int, reason: str = "", hourly_cap: int = 0) -> int:
        """增加好感度（可带每小时次数上限，用于防刷）。"""
        now = time.time()
        hour_key = int(now // 3600)
        history = self._data.setdefault("history", {})
        stats = history.setdefault(str(hour_key), {"count": 0, "sum": 0})
        if hourly_cap and int(stats.get("count", 0)) >= hourly_cap:
            return self.get()
        stats["count"] = int(stats.get("count", 0)) + 1
        stats["sum"] = int(stats.get("sum", 0)) + max(0, delta)
        old = int(self._data.get("favorability", self._seed))
        new_val = max(0, min(100, old + delta))
        self._data["favorability"] = new_val
        self._data["total_interactions"] = int(self._data.get("total_interactions", 0)) + 1
        self._data["updated_at"] = now
        if len(history) > 48:
            for k in list(history)[:-48]:
                history.pop(k, None)
        self._save(self._data)
        return new_val


# 亲疏称呼：把数值翻译成魔理沙能直接用的口吻档位
RELATION_TIERS: List[Tuple[int, str]] = [
    (90, "老友"),
    (60, "好友"),
    (30, "熟络"),
    (10, "眼熟"),
    (0, "陌生"),
]


class RelationStore:
    """群友关系：每个群友各自的亲疏与称呼（主人不在此表，走单源好感度）。"""

    def __init__(self) -> None:
        self._path = MARISA_DATA_DIR / "relations.json"
        self._lock = threading.RLock()
        MARISA_DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._data = self._load()

    def _load(self) -> Dict[str, Any]:
        if self._path.exists():
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        return {}

    def _save(self) -> None:
        try:
            with self._lock:
                with open(self._path, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def touch(
        self,
        platform: str,
        user_id: str,
        nickname: str = "",
        group_id: str = "",
        hourly_cap: int = 4,
    ) -> Dict[str, Any]:
        """记录一次互动：更新称呼、互动次数与亲疏（带每小时上限防刷）。"""

        key = f"{platform}:{user_id}"
        if not user_id:
            return {}
        with self._lock:
            record = self._data.setdefault(
                key,
                {
                    "user_id": user_id,
                    "platform": platform,
                    "nickname": nickname,
                    "affinity": 0,
                    "interactions": 0,
                    "last_seen": 0.0,
                    "last_group": "",
                    "hour_key": 0,
                    "hour_count": 0,
                },
            )
            now = time.time()
            hour_key = int(now // 3600)
            if int(record.get("hour_key", 0)) != hour_key:
                record["hour_key"] = hour_key
                record["hour_count"] = 0
            record["interactions"] = int(record.get("interactions", 0)) + 1
            record["last_seen"] = now
            if nickname:
                record["nickname"] = nickname
            if group_id:
                record["last_group"] = group_id
            if int(record.get("hour_count", 0)) < max(0, hourly_cap):
                record["hour_count"] = int(record.get("hour_count", 0)) + 1
                record["affinity"] = min(100, int(record.get("affinity", 0)) + 1)
            self._save()
            return dict(record)

    def describe(self, platform: str, user_id: str) -> str:
        """返回给 AI 看的称呼标签，如「小明·熟络」；没有记录时返回空串。"""

        record = self._data.get(f"{platform}:{user_id}")
        if not record:
            return ""
        nickname = str(record.get("nickname") or "").strip()
        affinity = int(record.get("affinity", 0))
        tier = next(label for threshold, label in RELATION_TIERS if affinity >= threshold)
        return f"{nickname}·{tier}" if nickname else tier


class GateMainConfig(PluginConfigBase):
    """插件开关。"""

    __ui_label__ = "插件"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用魔理沙守门人")
    config_version: str = Field(default="0.2.0", description="配置版本")

class GateRuleConfig(PluginConfigBase):
    """门控规则。"""

    __ui_label__ = "门控规则"
    __ui_order__ = 1

    group_mode: str = Field(default="open", description="群模式：open=所有群都可聊(须@/提到魔理沙)；whitelist=仅 groups 白名单群")
    owners: List[str] = Field(default_factory=lambda: ["3895418161"], description="主人 QQ 号列表（运行时优先取宿主 plugin.permission 的 qq 平台名单，此项仅作回退）")
    groups: List[str] = Field(default_factory=lambda: ["1058958004"], description="群白名单（group_mode=whitelist 时生效）")
    cooldown_sec: float = Field(default=3.0, description="同一群同一人触发后的最小间隔（秒）")
    seed_favorability: int = Field(default=51, description="好感度种子值（旧数据迁移）")
    fav_chat_cap_per_hour: int = Field(default=6, description="聊天好感每小时上限")
    fav_tool_cap_per_hour: int = Field(default=6, description="命令好感每小时上限")
    relations_enabled: bool = Field(default=True, description="记录群友关系（各自的亲疏与称呼，让魔理沙对群里不同人有不同态度）")
    relation_cap_per_hour: int = Field(default=4, description="单个群友每小时最多的关系亲疏增长次数")
    deepseek_api_key: str = Field(default="", description="DeepSeek API Key（查余额用；留空则读 D:\\MarisaBOT\\config.json）")

class GateTerminalConfig(PluginConfigBase):
    """远程终端（PowerShell/git/关机）。"""

    __ui_label__ = "远程终端"
    __ui_order__ = 3

    enabled: bool = Field(default=True, description="总开关：启用远程终端能力")
    confirm_required: bool = Field(default=True, description="非只读命令必须先回复『执行』确认")
    shutdown_confirm: bool = Field(
        default=False,
        description="关机是否要提案-确认：**默认 false=说了就关**（会先启动倒计时，"
                    "期间说「取消关机」可中止）。true 则回到「先回复执行 P1」的老流程",
    )
    workdir_default: str = Field(default=r"D:\MarisaBOT", description="默认工作目录")
    timeout_shell_sec: float = Field(default=60.0, description="普通命令超时（秒）")
    output_cap_chars: int = Field(default=1500, description="回传输出上限字符数")


class GateCleanupConfig(PluginConfigBase):
    """电脑管家（只读体检 → 计划 → 确认 → 执行）。"""

    __ui_label__ = "电脑管家"
    __ui_order__ = 3

    roots: List[str] = Field(
        default_factory=list,
        description="清理扫描根白名单；留空时回退桌面/下载/文档。任何文件操作都必须落在这里面",
    )
    project_roots: List[str] = Field(
        default_factory=list,
        description="代码仓库所在目录（只用于 /项目 审查，不会扩大清理白名单）",
    )
    large_file_mb: int = Field(default=200, description="大文件阈值（MB）")
    duplicate_mb: int = Field(default=10, description="参与重复检测的最小文件大小（MB）")
    max_plan_items: int = Field(default=200, description="单个清理计划的最大条目数，超过则要求缩小范围")
    allow_permanent_delete: bool = Field(
        default=False,
        description="是否允许对「可再生产物」彻底删除（默认只进回收站；回收站不释放磁盘空间，需要腾空间时才开）",
    )
    plan_ttl_sec: int = Field(default=600, description="清理方案有效期（秒）；过期后需要重新生成")
    desktop_organize: bool = Field(
        default=True,
        description="是否把桌面散落的文件纳入清理方案（只移动不删除，可 /撤销）",
    )
    desktop_min_age_hours: int = Field(
        default=24,
        description="桌面整理只处理超过该小时数没动过的文件，避免挪走主人正在用的东西",
    )


class MarisaGateConfig(PluginConfigBase):
    """魔理沙守门人配置。"""

    plugin: GateMainConfig = Field(default_factory=GateMainConfig)
    rules: GateRuleConfig = Field(default_factory=GateRuleConfig)
    terminal: GateTerminalConfig = Field(default_factory=GateTerminalConfig)
    cleanup: GateCleanupConfig = Field(default_factory=GateCleanupConfig)

def _deep_get(d: Any, keys: List[str], default: Any = None) -> Any:
    """对序列化消息 dict 做防御式取值。"""
    cur = d
    for k in keys:
        if isinstance(cur, dict):
            cur = cur.get(k)
        else:
            return default
    return cur if cur is not None else default

class MarisaGatePlugin(MaiBotPlugin):
    """魔理沙守门人插件。"""

    config_model = MarisaGateConfig

    def __init__(self) -> None:
        super().__init__()
        self._last_group_reply: Dict[Tuple[str, str], float] = {}
        self._fav = FavorabilityStore(seed=51)
        self._relations = RelationStore()

    async def on_load(self) -> None:
        """插件加载：初始化好感度存储。"""
        try:
            seed = int(self.config.rules.seed_favorability)
        except Exception:
            seed = 51
        self._fav = FavorabilityStore(seed=seed)
        print("[marisa_gate] loaded, favorability=", self._fav.get(), flush=True)

    async def on_unload(self) -> None:
        """插件卸载。"""

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        """配置热更新（保持内置状态同步）。"""
        try:
            seed = int(config_data.get("rules", {}).get("seed_favorability", 51))
        except Exception:
            seed = 51
        self._fav = FavorabilityStore(seed=seed)

    async def _tool_owner_ok(self, kwargs: Any) -> bool:
        """工具调用者鉴权：本地控制台/桌宠通道恒放行；否则按 user_id 匹配宿主
        plugin.permission（兼容 qq:123 与裸号 123），再回退自身 rules.owners。"""
        if not isinstance(kwargs, dict):
            return False
        platform = str(kwargs.get("platform") or "").strip()
        if platform in ("desktop", "bot_console", "maisaka_cli"):
            return True
        user_id = str(kwargs.get("user_id") or "").strip()
        if not user_id:
            return False
        try:
            permission = await self.ctx.config.get("plugin.permission", []) or []
        except Exception:
            permission = []
        allowed = set()
        for item in list(permission) + list(self.config.rules.owners or []):
            item = str(item or "").strip()
            if not item:
                continue
            allowed.add(item.split(":", 1)[1] if ":" in item else item)
        if user_id in allowed:
            return True
        print(f"[marisa_gate] 工具权限拦截 platform={platform} user_id={user_id}", flush=True)
        return False

    # ───────────────────────── 触发门控 ─────────────────────────

    @HookHandler(
        "chat.receive.before_process",
        name="marisa_trigger_gate",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
    )
    async def handle_trigger_gate(self, **kwargs: Any) -> Dict[str, Any]:
        """消息进入主链前的门控：不满足触发规则的消息直接 abort（静默）。"""
        cont = {"action": "continue", "modified_kwargs": kwargs}
        try:
            cfg = self.config
            if not cfg.plugin.enabled:
                return cont
            message = kwargs.get("message")
            if not isinstance(message, dict):
                return cont
            platform = str(_deep_get(message, ["platform"], "qq") or "qq")
            text = str(_deep_get(message, ["processed_plain_text"], "") or "").strip()
            if platform and platform != "qq":
                # 桌面通道只对「带方案编号的清理回执」做确定性拦截，其余照旧交主链
                if platform == "desktop" and text and not bool(_deep_get(message, ["is_notify"], False)):
                    session_id = str(_deep_get(message, ["session_id"], "") or "")
                    if await self._maybe_handle_cleanup_confirm(text, session_id):
                        return {"action": "abort"}
                return cont
            if not text or bool(_deep_get(message, ["is_notify"], False)):
                return cont

            user_info = _deep_get(message, ["message_info", "user_info"], {}) or {}
            group_info = _deep_get(message, ["message_info", "group_info"], None) or {}
            sender = str(_deep_get(user_info, ["user_id"], "") or "")
            group_id = str(_deep_get(group_info, ["group_id"], "") or "")
            session_id = str(_deep_get(message, ["session_id"], "") or "")
            is_group = bool(group_id)
            is_mentioned = bool(_deep_get(message, ["is_mentioned"], False))

            # 主人名单单源：宿主全局 plugin.permission（qq: 平台号）优先，空则回退自身配置
            fallback_owners = [str(x).strip() for x in cfg.rules.owners if str(x).strip()]
            try:
                permission = await self.ctx.config.get("plugin.permission", []) or []
                owners = [str(x).split(":", 1)[1] for x in permission if str(x).startswith("qq:")]
            except Exception:
                owners = []
            if not owners:
                owners = fallback_owners
            groups = [str(x).strip() for x in cfg.rules.groups if str(x).strip()]
            is_owner = sender in owners

            prefix_hit = text.startswith(PREFIX_WORDS)
            mentioned_hit = is_mentioned or any(w in text for w in PREFIX_WORDS)

            # 斜杠指令路由（确定性；群内仍需 @/提到；陌生人私聊不理会）
            slash_cmd = self._extract_slash(text)
            if slash_cmd is not None and (is_group or is_owner):
                if not is_group or (prefix_hit or mentioned_hit):
                    reply = await self._handle_slash(slash_cmd, is_owner, session_id)
                    if reply is not None:
                        await self._try_reply(session_id, reply)
                        return {"action": "abort"}

            if not is_group:
                # 私聊：仅主人（标记身份，供 AI 识别）
                if is_owner:
                    self._tag_as(message, text, "主人", self._pending_hint())
                    if self._fav.get() < 100:
                        self._fav.add(1, "QQ私聊互动", hourly_cap=int(cfg.rules.fav_chat_cap_per_hour))
                    return cont
                return {"action": "abort"}

            # 群白名单（仅 group_mode=whitelist 时生效）
            mode = str(getattr(cfg.rules, "group_mode", "open") or "open").strip().lower()
            if mode != "open" and group_id not in groups:
                return {"action": "abort"}

            # 所有群统一门槛：必须 @ 或明确提到魔理沙（含主人，防止无差别抢话）
            if not (prefix_hit or mentioned_hit):
                return {"action": "abort"}

            if is_owner:
                self._tag_as(message, text, "主人", self._pending_hint())
                return cont

            # 群友：冷却控制（敏感操作由宿主 operator 权限与工具运行时校验兜底）
            key = (group_id, sender)
            now = time.monotonic()
            last = self._last_group_reply.get(key, 0.0)
            if now - last < float(cfg.rules.cooldown_sec):
                return {"action": "abort"}
            self._last_group_reply[key] = now
            relation_note = ""
            if bool(cfg.rules.relations_enabled):
                nickname = str(
                    _deep_get(user_info, ["user_cardname"], "")
                    or _deep_get(user_info, ["user_nickname"], "")
                    or ""
                ).strip()
                record = self._relations.touch(
                    platform="qq",
                    user_id=sender,
                    nickname=nickname,
                    group_id=group_id,
                    hourly_cap=int(cfg.rules.relation_cap_per_hour),
                )
                # 带上「称呼·亲疏」，让魔理沙对群里不同人说话有区别
                relation_note = self._relations.describe("qq", sender) or record.get("nickname", "")
            self._tag_as(message, text, "群友", relation_note)
            return cont
        except Exception as exc:
            print("[marisa_gate] hook error:", exc, flush=True)
            return cont

    @staticmethod
    def _extract_slash(text: str) -> Optional[Tuple[str, str]]:
        """若消息以 / 开头（容忍 @/魔理沙 前缀），返回 (命令, 参数)；否则 None。"""
        t = re.sub(r"^\s*(?:\[At:[^\]]*\]|@[\w\u4e00-\u9fa5]+)\s*", "", text or "")
        for w in ("魔理沙", "雾雨魔理沙", "Marisa", "marisa", "MARISA"):
            if t.startswith(w):
                t = t[len(w):].lstrip("，,、 　")
                break
        t = t.strip()
        if not t.startswith("/"):
            return None
        m = re.match(r"/([^\s/，。！？!?]+)([\s\S]*)", t)
        if not m:
            return None
        return m.group(1).lower(), (m.group(2) or "").strip()

    async def _handle_slash(self, slash: Tuple[str, str], is_owner: bool, session_id: str) -> Optional[str]:
        """处理斜杠指令（仅终端家族确定性执行；其余放行给原生命令链/LLM）。

        非主人斜杠一律放行：已注册的桌宠 /指令由宿主 operator 权限拦截，
        未注册斜杠落入 LLM（其工具层有运行时身份校验）。
        """
        cmd, args = slash
        if not is_owner:
            return None

        tc = self._term_cfg()

        if cmd == "终端":
            if not args:
                return "用法：/终端 <PowerShell/git 命令>。例：/终端 git status"
            kind = SE.classify(args)
            if kind == "refuse":
                return "这个命令在禁止清单里だぜ……换一个吧。"
            if kind == "safe":
                asyncio.create_task(self._bg_shell(args, str(tc.workdir_default), session_id))
                return f"收到，正在执行：{args}（完成后回报结果）"
            return SE.shell_proposal(args, "", str(tc.workdir_default))
        if cmd in ("关闭电脑", "关机", "shutdown"):
            m = re.search(r"(\d{1,4})", args or "")
            sec = int(m.group(1)) if m else 30
            return SE.shutdown_action(
                sec, cancel=False,
                default_workdir=str(tc.workdir_default),
                require_confirm=bool(tc.shutdown_confirm),
            )
        if cmd in ("取消关机", "取消"):
            return SE.shutdown_action(0, cancel=True, default_workdir=str(tc.workdir_default))
        return None

    # ---- 电脑管家：只读体检（第一阶段只报告，不执行任何清理） ----

    async def _bg_disk_report(self, args: str, session_id: str) -> None:
        """后台跑磁盘体检并按需出专项结果。"""

        topic = (args or "").strip()
        try:
            if topic in ("大文件", "大文件扫描"):
                text = await asyncio.to_thread(self._report_large_files)
            elif topic in ("重复", "重复文件"):
                text = await asyncio.to_thread(self._report_duplicates)
            elif topic in ("临时", "临时文件", "缓存"):
                text = await asyncio.to_thread(self._report_temp)
            else:
                text = await asyncio.to_thread(self._report_overview)
        except Exception as exc:
            text = f"体检没做成：{str(exc)[:160]}"
        await self._try_reply(session_id, text)

    async def _bg_project_audit(self, session_id: str) -> None:
        """后台跑项目审查。"""

        try:
            text = await asyncio.to_thread(self._report_projects)
        except Exception as exc:
            text = f"项目审查没做成：{str(exc)[:160]}"
        await self._try_reply(session_id, text)

    def _report_roots(self) -> List[Path]:
        """扫描根：优先用配置里的目录，缺省回退桌面/下载/文档。"""

        configured = [str(item).strip() for item in list(self.config.cleanup.roots or []) if str(item).strip()]
        roots = [Path(item) for item in configured if Path(item).is_dir()]
        return roots or DS.default_roots()

    def _report_overview(self) -> str:
        """总览：磁盘容量、回收站、临时项。"""

        data = DS.scan_overview()
        lines = ["🧹 电脑体检（只读扫描）", ""]
        for drive in data["drives"]:
            free_ratio = drive["free"] / drive["total"] * 100 if drive["total"] else 0
            warn = " ⚠️ 空间偏紧" if free_ratio < 10 else ""
            lines.append(
                f"{drive['path']} 可用 {DS.human_size(drive['free'])}"
                f" / 共 {DS.human_size(drive['total'])}（{free_ratio:.0f}% 空闲）{warn}"
            )
        bin_size = data["recycle_bin"]["size"]
        lines.append(
            f"回收站 {DS.human_size(bin_size)}（{data['recycle_bin']['items']} 项）"
            "——注意：回收站里的文件仍占磁盘，清空才真正释放"
        )
        if data["temp_candidates"]:
            lines.append("")
            lines.append("可清理的缓存/临时目录：")
            for item in data["temp_candidates"]:
                prefix = "至少 " if not item.get("complete", True) else ""
                lines.append(f"· {item['label']}：{prefix}{DS.human_size(item['size'])}")
        if data["truncated"]:
            lines.append("")
            lines.append("（本次扫描未跑完，数字是下限；再发一次 /体检 可重扫）")
        return "\n".join(lines)

    def _report_temp(self) -> str:
        """只报临时项明细。"""

        data = DS.scan_overview()
        if not data["temp_candidates"]:
            return "没找到可清理的缓存目录だぜ。"
        lines = ["🧹 缓存与临时目录（只读）"]
        for item in data["temp_candidates"]:
            prefix = "至少 " if not item.get("complete", True) else ""
            lines.append(f"· {item['label']}：{prefix}{DS.human_size(item['size'])}\n  {item['path']}")
        lines.append("")
        lines.append("清理计划还没开放执行——这一步只报告，确认机制做完后再动文件。")
        return "\n".join(lines)

    def _report_large_files(self) -> str:
        """列出大文件。"""

        data = DS.scan_large_files(self._report_roots())
        if not data["files"]:
            return "扫描范围内没找到大文件だぜ。"
        lines = [f"📦 大文件 Top {len(data['files'])}（共找到 {data['total_found']} 个）"]
        for item in data["files"]:
            lines.append(f"· {DS.human_size(item['size'])}  {item['path']}")
        if data["truncated"]:
            lines.append("（扫描超时，结果不完整）")
        return "\n".join(lines)

    def _report_duplicates(self) -> str:
        """列出重复文件分组。"""

        data = DS.scan_duplicates(self._report_roots())
        if not data["groups"]:
            return "没找到重复文件だぜ。"
        lines = [
            f"👯 重复文件：{data['total_groups']} 组，最多可省 {DS.human_size(data['wasted_bytes'])}",
            "",
        ]
        for index, group in enumerate(data["groups"], start=1):
            lines.append(f"{index}. {DS.human_size(group['size'])} × {group['count']} 份")
            for path in group["paths"][:4]:
                lines.append(f"   {path}")
        if data["truncated"]:
            lines.append("（扫描超时，结果不完整）")
        return "\n".join(lines)

    def _project_roots(self) -> List[Path]:
        """项目审查的扫描目录：与清理白名单分开，避免把仓库纳入可清理范围。"""

        configured = [str(item).strip() for item in list(self.config.cleanup.project_roots or []) if str(item).strip()]
        return [Path(item) for item in configured if Path(item).is_dir()]

    def _report_projects(self) -> str:
        """项目审查报告。"""

        roots = self._project_roots()
        if not roots:
            return (
                "还没配置项目目录だぜ。把仓库上层目录写进插件配置的 cleanup.project_roots，"
                "我就能审查各项目的占用与 Git 状态了。"
            )
        data = DS.scan_projects(roots)
        if not data["projects"]:
            return "扫描范围里没找到 git 仓库だぜ。可以在配置里把项目目录加进 cleanup.roots。"
        lines = ["🗂 项目审查（只读）", ""]
        for repo in data["projects"]:
            lines.append(f"【{repo['name']}】{DS.human_size(repo['total_bytes'])}")
            repro = repo["reproducible"]
            if repro:
                detail = "、".join(f"{name} {DS.human_size(size)}" for name, size in sorted(repro.items(), key=lambda kv: -kv[1]))
                lines.append(f"  可再生产物 {DS.human_size(repo['reproducible_bytes'])}：{detail}")
                lines.append("  （删掉能重新生成，但需要重新下载/构建，耗的是时间不是数据）")
            flags = []
            if repo["dirty_files"]:
                flags.append(f"未提交改动 {repo['dirty_files']} 个文件")
            if repo["unpushed_commits"]:
                flags.append(f"未推送提交 {repo['unpushed_commits']} 个")
            if repo["branch_count"] > 1:
                flags.append(f"分支 {repo['branch_count']} 个")
            lines.append("  " + ("；".join(flags) if flags else "工作区干净"))
        if data["truncated"]:
            lines.append("")
            lines.append("（扫描超时，结果不完整）")
        lines.append("")
        lines.append("目前只报告不执行；清理计划与确认流程在下一步开放。")
        return "\n".join(lines)

    # ---- 电脑管家：清理方案（计划 → 确认 → 执行 → 撤销） ----

    async def _maybe_handle_cleanup_confirm(self, text: str, session_id: str) -> bool:
        """处理主人对清理方案的回执；命中返回 True（调用方应中止主链）。

        只认带方案编号的显式口令（执行 P3 / 执行 P3 1,3 彻底 / 取消 P3），
        裸「执行/取消」继续留给终端待办，避免两条确认链互相抢话。
        """

        plan = CP.get_pending_plan()
        parsed = CP.parse_confirm(text, plan)
        if plan is None or parsed is None:
            return False

        if parsed["action"] == "cancel":
            CP.mark_plan_status(plan, "cancelled", cancelled_at=time.time())
            await self._notify_pet_close(plan)
            await self._try_reply(session_id, f"清理方案 P{plan.get('code')} 已取消だぜ。")
            return True

        await self._try_reply(session_id, f"收到，开始执行清理方案 P{plan.get('code')}…（完成后回报）")
        asyncio.create_task(
            self._bg_execute_cleanup(plan, parsed["selected"], bool(parsed["permanent"]), session_id)
        )
        return True

    def _desktop_root(self) -> Optional[Path]:
        """桌面整理根目录；开关关闭或不在清理白名单内时返回 None。"""

        if not bool(self.config.cleanup.desktop_organize):
            return None
        desktop = Path.home() / "Desktop"
        if not desktop.is_dir():
            return None
        configured = [str(item).strip() for item in list(self.config.cleanup.roots or []) if str(item).strip()]
        if configured:
            allowed = [Path(item) for item in configured if Path(item).is_dir()]
            if not any(CP.is_within(desktop, root) for root in allowed):
                return None
        return desktop

    def _build_cleanup_plan(self, session_id: str, platform: str) -> Dict[str, Any]:
        """扫描并生成清理方案（同步，调用方放线程里跑）。"""

        overview = DS.scan_overview()
        temp_candidates = list(overview.get("temp_candidates") or [])
        desktop_root = self._desktop_root()
        desktop_files: List[Dict[str, Any]] = []
        if desktop_root is not None:
            remaining = max(0, int(self.config.cleanup.max_plan_items) - len(temp_candidates))
            if remaining > 0:
                clutter = DS.scan_desktop_clutter(
                    desktop_root,
                    min_age_hours=int(self.config.cleanup.desktop_min_age_hours),
                    limit=remaining,
                )
                desktop_files = list(clutter.get("files") or [])
        return CP.build_plan(
            temp_candidates=temp_candidates,
            desktop_files=desktop_files,
            desktop_root=desktop_root,
            max_items=int(self.config.cleanup.max_plan_items),
            ttl_sec=int(self.config.cleanup.plan_ttl_sec),
            source={"platform": platform, "stream_id": session_id},
        )

    async def _render_plan_card(self, plan: Dict[str, Any]) -> str:
        """把清理方案渲染成图片，返回 base64；渲染不可用时返回空串。"""

        try:
            html = PC.render_html(plan, allow_permanent=bool(self.config.cleanup.allow_permanent_delete))
            result = await self.ctx.render.html2png(
                html,
                selector="body",
                viewport={"width": 760, "height": 900},
                device_scale_factor=2.0,
                full_page=True,
            )
        except Exception as exc:
            self.ctx.logger.info("[marisa_gate] 清理方案卡片渲染失败，退回文本: %s", exc)
            return ""
        if isinstance(result, dict):
            return str(result.get("image_base64") or "")
        return str(getattr(result, "image_base64", "") or "")

    async def _present_cleanup_plan(self, plan: Dict[str, Any], session_id: str, platform: str) -> None:
        """把方案送到来源会话（QQ 卡片 / 桌面提示），并尝试弹桌宠确认框。"""

        allow_permanent = bool(self.config.cleanup.allow_permanent_delete)
        if platform == "desktop":
            await self._try_reply(
                session_id,
                f"清理方案 P{plan.get('code')} 已生成：共 {len(plan.get('items') or [])} 项，弹窗里勾选确认だぜ。",
            )
        else:
            image = await self._render_plan_card(plan)
            sent = False
            if image and session_id:
                try:
                    await self.ctx.send.hybrid(
                        [
                            {"type": "text", "content": f"清理方案 P{plan.get('code')} 来了だぜ，看图确认！★"},
                            {"type": "image", "content": image},
                        ],
                        session_id,
                    )
                    sent = True
                except Exception as exc:
                    self.ctx.logger.info("[marisa_gate] 清理方案卡片发送失败，退回文本: %s", exc)
            if not sent and session_id:
                await self._try_reply(session_id, PC.render_text(plan, allow_permanent=allow_permanent))

        delivered = await self._push_confirm_to_pet(plan)
        if delivered:
            self.ctx.logger.info("[marisa_gate] 清理方案 P%s 已推送桌宠弹窗", plan.get("code"))

    async def _push_pet_frame(self, frame: Dict[str, Any]) -> int:
        """把一帧 JSON 推给桌宠；返回送达的客户端数量（失败返回 0）。"""

        try:
            result = await self.ctx.api.call(
                "marisa-team.marisa-desktop.push_pet_frame",
                frame=frame,
            )
        except Exception as exc:
            self.ctx.logger.info("[marisa_gate] 推送桌宠帧失败: %s", exc)
            return 0
        if isinstance(result, dict):
            try:
                return int(result.get("delivered") or 0)
            except (TypeError, ValueError):
                return 0
        return 0

    async def _push_confirm_to_pet(self, plan: Dict[str, Any]) -> bool:
        """把清理方案作为确认弹窗推给桌宠（离线则静默跳过）。"""

        frame = {
            "type": "confirm_request",
            "req_id": plan.get("req_id"),
            "plan_code": plan.get("code"),
            "title": f"清理方案 P{plan.get('code')}",
            "expires_in": max(0, int(float(plan.get("expires_at") or 0) - time.time())),
            "allow_permanent": bool(self.config.cleanup.allow_permanent_delete),
            "items": [
                {
                    "id": item.get("id"),
                    "label": item.get("label"),
                    "size_text": DS.human_size(int(item.get("size") or 0)),
                    "reason": item.get("reason"),
                    "kind": item.get("kind"),
                    "undoable": bool(item.get("undoable", True)),
                }
                for item in plan.get("items") or []
            ],
        }
        return await self._push_pet_frame(frame) > 0

    async def _notify_pet_close(self, plan: Dict[str, Any]) -> None:
        """通知桌宠关闭指定方案的弹窗。"""

        req_id = str(plan.get("req_id") or "")
        if req_id:
            await self._push_pet_frame({"type": "confirm_closed", "req_id": req_id})

    async def _bg_build_cleanup_plan(self, session_id: str, platform: str) -> None:
        """后台扫描并生成清理方案。"""

        try:
            plan = await asyncio.to_thread(self._build_cleanup_plan, session_id, platform)
        except Exception as exc:
            await self._try_reply(session_id, f"清理方案没做成：{str(exc)[:160]}")
            return
        if not plan.get("ok"):
            await self._try_reply(session_id, f"没能生成清理方案：{plan.get('error')}")
            return

        old = CP.get_pending_plan()
        if old is not None:
            CP.mark_plan_status(old, "replaced", replaced_at=time.time())
            await self._notify_pet_close(old)

        plan = CP.save_plan(plan)
        self.ctx.logger.info(
            "[marisa_gate] 清理方案 P%s 生成：%s 项（platform=%s）",
            plan.get("code"), len(plan.get("items") or []), platform or "?",
        )
        await self._present_cleanup_plan(plan, session_id, platform)

    async def _bg_execute_cleanup(
        self,
        plan: Dict[str, Any],
        selected: Optional[List[int]],
        permanent: bool,
        reply_stream: str,
    ) -> None:
        """后台执行清理方案，并把结果回报给来源会话与桌宠。"""

        try:
            result = await asyncio.to_thread(
                CP.execute_plan,
                plan,
                selected=selected,
                permanent=permanent,
                allow_permanent=bool(self.config.cleanup.allow_permanent_delete),
                desktop_root=self._desktop_root(),
            )
        except Exception as exc:
            result = {"ok": False, "error": f"执行时出错：{str(exc)[:160]}"}
        text = self._format_execute_result(result)
        if reply_stream:
            await self._try_reply(reply_stream, text)
        await self._notify_pet_close(plan)
        await self._push_pet_frame({"type": "bot_message", "text": text, "message_id": "", "raw_message": []})

    async def _bg_undo_cleanup(self, session_id: str) -> None:
        """后台按 manifest 撤销上一次清理。"""

        try:
            result = await asyncio.to_thread(CP.undo_plan)
        except Exception as exc:
            result = {"ok": False, "error": f"撤销时出错：{str(exc)[:160]}"}
        text = self._format_undo_result(result)
        if session_id:
            await self._try_reply(session_id, text)
        await self._push_pet_frame({"type": "bot_message", "text": text, "message_id": "", "raw_message": []})

    @staticmethod
    def _format_execute_result(result: Dict[str, Any]) -> str:
        """把执行结果整理成主人可读的回执。"""

        if not result.get("ok"):
            lines = [f"清理没有执行：{result.get('error')}"]
            for problem in list(result.get("problems") or [])[:8]:
                lines.append(f"· {problem}")
            return "\n".join(lines)

        summary = result.get("summary") or {}
        lines = [f"🧹 清理方案 P{result.get('plan_code')} 执行完毕：完成 {summary.get('done')}/{summary.get('total')} 项"]
        if summary.get("trashed_bytes"):
            lines.append(
                f"· 移入回收站：{DS.human_size(summary['trashed_bytes'])}"
                "（清空回收站后才真正释放空间）"
            )
        if summary.get("permanent_bytes"):
            lines.append(f"· 彻底删除释放：{DS.human_size(summary['permanent_bytes'])}")
        if summary.get("moved_bytes"):
            lines.append(f"· 桌面整理移动：{DS.human_size(summary['moved_bytes'])}（可 /撤销）")
        failures = [item for item in result.get("items", []) if item.get("status") == "failed"]
        if failures:
            lines.append("没做成的：")
            for item in failures[:5]:
                lines.append(f"· {item.get('label')}：{item.get('error')}")
        return "\n".join(lines)

    @staticmethod
    def _format_undo_result(result: Dict[str, Any]) -> str:
        """把撤销结果整理成主人可读的回执。"""

        if not result.get("ok"):
            return f"撤销没做成：{result.get('error')}"
        lines = [f"↩️ 撤销完成：还原 {result.get('restored')} 项，跳过 {result.get('skipped')} 项"]
        for item in list(result.get("items") or [])[:8]:
            if item.get("status") != "restored":
                lines.append(f"· {item.get('label')}：{item.get('error') or item.get('status')}")
        lines.append("（回收站里没找到的可以打开回收站手动还原）")
        return "\n".join(lines)

    # ---- 斜杠指令的私有实现（远程终端用） ----

    async def _bg_shell(self, cmd: str, cwd: str, session_id: str) -> None:
        try:
            result = await asyncio.to_thread(SE.execute_shell, cmd, cwd, 60.0, cwd)
        except Exception as exc:
            result = f"执行失败：{exc}"
        try:
            await self.ctx.send.text(result, session_id)
        except Exception:
            pass

    @staticmethod
    def _tag_as(message: dict, text: str, tag: str, detail: str = "") -> None:
        """给放行的消息加身份隐形标记（〔主人〕/〔群友·小明·熟络〕），供 AI 识别说话者。"""
        if text.startswith("〔"):
            return
        label = f"{tag}·{detail}" if detail else tag
        message["processed_plain_text"] = f"〔{label}〕 {text}"

    async def _try_reply(self, session_id: str, text: str) -> None:
        """尽力回复（无有效 stream/session 时静默失败）。"""
        if not session_id:
            return
        try:
            await self.ctx.send.text(text, session_id)
        except Exception:
            pass

    async def _send_hybrid(self, stream_id: str, segments: List[Dict[str, str]]) -> None:
        try:
            await self.ctx.send.hybrid(segments, stream_id)
        except Exception as exc:
            print("[marisa_gate] hybrid send error:", exc, flush=True)

    def _raw(self, kwargs: Any) -> str:
        mg = kwargs.get("matched_groups") if isinstance(kwargs, dict) else None
        if isinstance(mg, dict) and mg.get("raw"):
            return str(mg["raw"]).strip()
        txt = kwargs.get("text") if isinstance(kwargs, dict) else None
        return str(txt or "").strip()

    # ───────────────────────── 远程终端工具（仅主人；只读直跑，其余提案-确认） ─────────────────────────

    def _term_cfg(self) -> "GateTerminalConfig":
        return self.config.terminal

    @Tool(
        "marisa_shell",
        description="在主人电脑上执行 PowerShell/git 等命令。只读类命令（git status/log/diff、dir、版本查询等）直接执行；其他命令会先提议、等主人回复『执行』后才真正执行；危险命令直接拒绝。",
        parameters=[
            ToolParameterInfo(name="command", param_type=ToolParamType.STRING, description="要执行的命令", required=True),
            ToolParameterInfo(name="workdir", param_type=ToolParamType.STRING, description="工作目录（默认 D:\\MarisaBOT）", required=False),
        ],
    )
    async def tool_shell(self, command: str = "", workdir: str = "", **kwargs: Any) -> str:
        if not await self._tool_owner_ok(kwargs):
            return "这个操作得让主人来だぜ～"
        del kwargs
        tc = self._term_cfg()
        if not tc.enabled:
            return "远程终端总开关没开だぜ。"
        cmd = str(command or "").strip()
        if not cmd:
            return "命令是空的だぜ。"
        return SE.shell_proposal(cmd, str(workdir or ""), str(tc.workdir_default))

    @Tool(
        "marisa_shutdown",
        description="关机专用工具（带倒计时可取消）。仅当主人明确说出『关机/重启』字样时才能调用；寒暄（如晚安/明天见/去休息吧）不要调用，避免误触发。默认直接启动倒计时（主人回复『取消关机』可中止）；若配置开回了确认模式，则会先提议、等主人回复『执行 P编号』后生效。",
        parameters=[
            ToolParameterInfo(name="seconds", param_type=ToolParamType.INTEGER, description="倒计时秒数，默认 30", required=False),
            ToolParameterInfo(name="cancel", param_type=ToolParamType.BOOLEAN, description="true=取消已排定的关机", required=False),
        ],
    )
    async def tool_shutdown(self, seconds: int = 30, cancel: bool = False, **kwargs: Any) -> str:
        if not await self._tool_owner_ok(kwargs):
            return "这个操作得让主人来だぜ～"
        del kwargs
        tc = self._term_cfg()
        if not tc.enabled:
            return "远程终端总开关没开だぜ。"
        return SE.shutdown_action(
            max(0, int(seconds or 30)), bool(cancel),
            str(tc.workdir_default),
            require_confirm=bool(tc.shutdown_confirm),
        )

    # ───────────────── 待确认：意图归模型，执行归这里 ─────────────────

    def _pending_hint(self) -> str:
        """给主人消息附一句"现在有什么待确认"。

        这是"意图判断交给模型"的落点：不再用正则去猜主人那句话是不是回执，
        而是把待确认清单搭在消息上，让模型自己看着办。
        """

        parts = [f"P{item.get('code')}={str(item.get('cmd') or '')[:40]}" for item in SE.list_pending()]
        plan = CP.get_pending_plan()
        if plan:
            parts.append(f"清理方案P{plan.get('code')}")
        return "待确认 " + "；".join(parts) if parts else ""

    def _render_pending(self) -> str:
        """待确认清单的文本形式（给模型看）。"""

        items = SE.list_pending()
        plan = CP.get_pending_plan()
        if not items and not plan:
            return "现在没有待确认的东西だぜ。"
        lines = ["待确认："]
        for item in items:
            lines.append(
                f"· [terminal] P{item.get('code')}（{item.get('kind')}）：{str(item.get('cmd') or '')[:120]}"
            )
        if plan:
            note = str(plan.get("summary") or plan.get("reason") or "").strip()
            lines.append(f"· [cleanup] P{plan.get('code')}：清理方案{'（' + note[:80] + '）' if note else ''}")
        lines.append("要执行用 marisa_pending_confirm，要取消用 marisa_pending_cancel，记得带 source。")
        return "\n".join(lines)

    @Tool(
        "marisa_pending_list",
        description="列出当前所有待确认项（终端/关机命令、清理方案）及其编号。主人回复『执行 P1』『算了』这类话时，先用它看清有什么待确认，再决定调用确认还是取消工具。",
    )
    async def tool_pending_list(self, **kwargs: Any) -> str:
        if not await self._tool_owner_ok(kwargs):
            return "这个操作得让主人来だぜ～"
        del kwargs
        return self._render_pending()

    @Tool(
        "marisa_pending_confirm",
        description="执行一条**已存在**的待确认项。source=terminal 执行终端/关机待确认；source=cleanup 执行清理方案。编号取自 marisa_pending_list，不存在或已过期会被拒绝。",
        parameters=[
            ToolParameterInfo(name="code", param_type=ToolParamType.INTEGER, description="待确认编号（见 marisa_pending_list）", required=True),
            ToolParameterInfo(name="source", param_type=ToolParamType.STRING, description="terminal 或 cleanup", required=False),
            ToolParameterInfo(name="selected", param_type=ToolParamType.STRING, description="清理方案：要处理的条目（如 1,3；留空=全部）", required=False),
            ToolParameterInfo(name="permanent", param_type=ToolParamType.BOOLEAN, description="清理方案：是否彻底删除（默认进回收站）", required=False),
        ],
    )
    async def tool_pending_confirm(
        self,
        code: int = 0,
        source: str = "terminal",
        selected: str = "",
        permanent: bool = False,
        **kwargs: Any,
    ) -> str:
        if not await self._tool_owner_ok(kwargs):
            return "这个操作得让主人来だぜ～"
        stream_id = str(kwargs.get("stream_id") or "")
        if str(source or "").strip().lower() == "cleanup":
            # 交给既有的清理流程：编号/条目/是否彻底的机械解析由 CP.parse_confirm 做，
            # 但它吃的是**这里拼出来的字符串**，不是主人的原话——正则不再判断意图。
            text = f"执行 P{int(code)}"
            if str(selected or "").strip():
                text += f" {str(selected).strip()}"
            if bool(permanent):
                text += " 彻底"
            handled = await self._maybe_handle_cleanup_confirm(text, stream_id)
            return "清理方案已受理だぜ。" if handled else f"没有编号 P{int(code)} 的清理方案（可能已过期）。"

        item = SE.pop_pending(int(code))
        if item is None:
            return f"没有编号 P{int(code)} 的待确认命令（可能已过期）。"
        tc = self._term_cfg()
        return SE.execute_pending(item, timeout_sec=float(tc.timeout_shell_sec))

    @Tool(
        "marisa_pending_cancel",
        description="取消一条待确认项（不会执行）。source=terminal 取消终端/关机待确认；source=cleanup 取消清理方案。",
        parameters=[
            ToolParameterInfo(name="code", param_type=ToolParamType.INTEGER, description="待确认编号（见 marisa_pending_list）", required=True),
            ToolParameterInfo(name="source", param_type=ToolParamType.STRING, description="terminal 或 cleanup", required=False),
        ],
    )
    async def tool_pending_cancel(self, code: int = 0, source: str = "terminal", **kwargs: Any) -> str:
        if not await self._tool_owner_ok(kwargs):
            return "这个操作得让主人来だぜ～"
        stream_id = str(kwargs.get("stream_id") or "")
        if str(source or "").strip().lower() == "cleanup":
            handled = await self._maybe_handle_cleanup_confirm(f"取消 P{int(code)}", stream_id)
            return "清理方案已取消だぜ。" if handled else f"没有编号 P{int(code)} 的清理方案（可能已过期）。"

        item = SE.pop_pending(int(code))
        if item is None:
            return f"没有编号 P{int(code)} 的待确认命令（可能已过期）。"
        replies = ["已取消该待办だぜ。"]
        if item.get("kind") == "shutdown":
            tc = self._term_cfg()
            replies.append(
                SE.shutdown_action(0, cancel=True, default_workdir=str(tc.workdir_default))
            )
        return "\n".join(replies)

    # ───────────────────────── 电脑管家（只读体检） ─────────────────────────

    @Tool(
        "marisa_disk_report",
        description="体检主人的电脑：磁盘剩余空间、回收站占用、可清理的缓存/临时目录、大文件、重复文件。只读扫描，不会删除或移动任何文件。",
        parameters=[
            ToolParameterInfo(
                name="topic",
                param_type=ToolParamType.STRING,
                description="留空=总览；可选 大文件 / 重复 / 临时",
                required=False,
            ),
        ],
    )
    async def tool_disk_report(self, topic: str = "", **kwargs: Any) -> str:
        if not await self._tool_owner_ok(kwargs):
            return "这个操作得让主人来だぜ～"
        del kwargs
        chosen = str(topic or "").strip()
        if chosen in ("大文件", "大文件扫描"):
            return await asyncio.to_thread(self._report_large_files)
        if chosen in ("重复", "重复文件"):
            return await asyncio.to_thread(self._report_duplicates)
        if chosen in ("临时", "临时文件", "缓存"):
            return await asyncio.to_thread(self._report_temp)
        return await asyncio.to_thread(self._report_overview)

    @Tool(
        "marisa_project_audit",
        description="审查主人的代码仓库：各项目磁盘占用、可再生产物（依赖/构建产物）体积、未提交改动、未推送提交与分支数。只读，不会修改任何仓库。",
    )
    async def tool_project_audit(self, **kwargs: Any) -> str:
        if not await self._tool_owner_ok(kwargs):
            return "这个操作得让主人来だぜ～"
        del kwargs
        return await asyncio.to_thread(self._report_projects)

    # ───────────────────────── 主人命令（仅 /斜杠；宿主 operator 权限拦截） ─────────────────────────

    @Command("marisa_screenshot", description="截屏并回图：/截图（仅管理员）", pattern=r"^(?:\s*〔[^〕]*〕\s*)?(?:\s*(?:\[At:[^\]]*\]|@[\w\u4e00-\u9fa5]+)\s*)*(?:魔理沙[，,、\s]*)?(?P<raw>/(?:截图|截屏)(?:\s+[\s\S]*)?\s*)$", permission="operator")
    async def cmd_screenshot(self, **kwargs: Any) -> Tuple[bool, str, bool]:
        """截屏回图（/截图）。"""
        stream_id = str((kwargs or {}).get("stream_id") or "")
        if not await self._tool_owner_ok(kwargs):
            await self._try_reply(stream_id, "这个操作得让主人来だぜ～")
            return True, "", True
        try:
            img = ImageGrab.grab(all_screens=True)
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=80)
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            segments = [
                {"type": "text", "content": "这是主人屏幕现在的样子だぜ！📷"},
                {"type": "image", "content": b64},
            ]
            await self._send_hybrid(stream_id, segments)
            if self._fav.get() < 100:
                self._fav.add(2, "远程截图", hourly_cap=int(self.config.rules.fav_tool_cap_per_hour))
        except Exception as exc:
            await self._try_reply(stream_id, f"截屏失败了：{exc}")
        return True, "", True

    # ───────────────────────── 电脑管家指令（QQ 与桌面两条通道共用宿主派发） ─────────────────────────

    @Command(
        "marisa_disk_report",
        description="电脑体检：磁盘、回收站、缓存、大文件、重复文件（只读扫描）：/体检 [大文件|重复|临时]（仅管理员）",
        pattern=r"^(?:\s*〔[^〕]*〕\s*)?(?:\s*(?:\[At:[^\]]*\]|@[\w\u4e00-\u9fa5]+)\s*)*(?:魔理沙[，,、\s]*)?(?P<raw>/体检(?:\s+[\s\S]*)?\s*)$",
        permission="operator",
    )
    async def cmd_disk_report(self, **kwargs: Any) -> Tuple[bool, str, bool]:
        """电脑体检（只读）。"""
        stream_id = str((kwargs or {}).get("stream_id") or "")
        if not await self._tool_owner_ok(kwargs):
            await self._try_reply(stream_id, "这个操作得让主人来だぜ～")
            return True, "", True
        text = self._raw(kwargs)
        topic = text.split(None, 1)[1].strip() if len(text.split(None, 1)) > 1 else ""
        self.ctx.logger.info("[marisa_gate] /体检 stream_id=%s topic=%s", stream_id or "(空)", topic or "(总览)")
        await self._try_reply(stream_id, "体检开始だぜ，扫完就把报告发你（只读扫描，不动任何文件）。")
        asyncio.create_task(self._bg_disk_report(topic, stream_id))
        return True, "", 1

    @Command(
        "marisa_project_audit",
        description="审查代码仓库占用与 Git 状态（只读扫描）：/项目（仅管理员）",
        pattern=r"^(?:\s*〔[^〕]*〕\s*)?(?:\s*(?:\[At:[^\]]*\]|@[\w\u4e00-\u9fa5]+)\s*)*(?:魔理沙[，,、\s]*)?(?P<raw>/(?:项目|项目审查)(?:\s+[\s\S]*)?\s*)$",
        permission="operator",
    )
    async def cmd_project_audit(self, **kwargs: Any) -> Tuple[bool, str, bool]:
        """项目审查（只读）。"""
        stream_id = str((kwargs or {}).get("stream_id") or "")
        if not await self._tool_owner_ok(kwargs):
            await self._try_reply(stream_id, "这个操作得让主人来だぜ～")
            return True, "", True
        self.ctx.logger.info("[marisa_gate] /项目 stream_id=%s", stream_id or "(空)")
        await self._try_reply(stream_id, "正在翻你的项目だぜ，稍等一会儿（只读扫描）。")
        asyncio.create_task(self._bg_project_audit(stream_id))
        return True, "", 1

    @Command(
        "marisa_cleanup",
        description="电脑管家：生成清理方案（临时缓存 + 桌面整理，确认后才执行）：/清理（仅管理员）",
        pattern=r"^(?:\s*〔[^〕]*〕\s*)?(?:\s*(?:\[At:[^\]]*\]|@[\w\u4e00-\u9fa5]+)\s*)*(?:魔理沙[，,、\s]*)?(?P<raw>/清理(?:\s+[\s\S]*)?\s*)$",
        permission="operator",
    )
    async def cmd_cleanup(self, **kwargs: Any) -> Tuple[bool, str, bool]:
        """生成清理方案（只生成，不执行）。"""
        stream_id = str((kwargs or {}).get("stream_id") or "")
        if not await self._tool_owner_ok(kwargs):
            await self._try_reply(stream_id, "这个操作得让主人来だぜ～")
            return True, "", True
        platform = str((kwargs or {}).get("platform") or "")
        self.ctx.logger.info("[marisa_gate] /清理 stream_id=%s platform=%s", stream_id or "(空)", platform or "?")
        await self._try_reply(
            stream_id,
            "正在盘点可清理的缓存和桌面散落文件だぜ，稍等（只读扫描，确认后才动手）。",
        )
        asyncio.create_task(self._bg_build_cleanup_plan(stream_id, platform))
        return True, "", 1

    @Command(
        "marisa_cleanup_undo",
        description="撤销上一次清理执行：移动的还原、回收站的尽力找回：/撤销（仅管理员）",
        pattern=r"^(?:\s*〔[^〕]*〕\s*)?(?:\s*(?:\[At:[^\]]*\]|@[\w\u4e00-\u9fa5]+)\s*)*(?:魔理沙[，,、\s]*)?(?P<raw>/(?:撤销|清理撤销)(?:\s+[\s\S]*)?\s*)$",
        permission="operator",
    )
    async def cmd_cleanup_undo(self, **kwargs: Any) -> Tuple[bool, str, bool]:
        """撤销上一次清理。"""
        stream_id = str((kwargs or {}).get("stream_id") or "")
        if not await self._tool_owner_ok(kwargs):
            await self._try_reply(stream_id, "这个操作得让主人来だぜ～")
            return True, "", True
        self.ctx.logger.info("[marisa_gate] /撤销 stream_id=%s", stream_id or "(空)")
        await self._try_reply(stream_id, "正在按执行记录撤销上一次清理だぜ…")
        asyncio.create_task(self._bg_undo_cleanup(stream_id))
        return True, "", 1

    # ───────────────────────── 电脑管家工具（只生成方案，不执行） ─────────────────────────

    @Tool(
        "marisa_cleanup_plan",
        description="生成电脑清理方案：扫描临时缓存与桌面散落文件，给出可清理清单并推送给主人确认；只生成方案不执行，确认后才动手。",
    )
    async def tool_cleanup_plan(self, **kwargs: Any) -> str:
        if not await self._tool_owner_ok(kwargs):
            return "这个操作得让主人来だぜ～"
        stream_id = str(kwargs.get("stream_id") or "")
        platform = str(kwargs.get("platform") or "")
        plan = await asyncio.to_thread(self._build_cleanup_plan, stream_id, platform)
        if not plan.get("ok"):
            return f"没能生成清理方案：{plan.get('error')}"
        old = CP.get_pending_plan()
        if old is not None:
            CP.mark_plan_status(old, "replaced", replaced_at=time.time())
            await self._notify_pet_close(old)
        plan = CP.save_plan(plan)
        await self._present_cleanup_plan(plan, stream_id, platform)
        return (
            f"清理方案 P{plan.get('code')} 已生成并发给主人"
            f"（共 {len(plan.get('items') or [])} 项），等主人确认后才执行。"
        )

    # ───────────────────────── 桌宠弹窗确认（跨插件公开 API） ─────────────────────────

    @API(
        "cleanup_confirm",
        description="桌宠弹窗对清理方案的确认结果（宿主内部使用）",
        version="1",
        public=True,
    )
    async def api_cleanup_confirm(
        self,
        plan_code: int = 0,
        req_id: str = "",
        approved: bool = False,
        selected: Optional[List[int]] = None,
        permanent: bool = False,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """接收桌宠弹窗的确认/取消，与 QQ 确认共享同一份待确认方案。"""

        del kwargs
        plan = CP.get_pending_plan()
        if plan is None:
            return {"accepted": False, "error": "没有待确认的清理方案（可能已过期或已处理）"}
        if int(plan.get("code") or 0) != int(plan_code or 0):
            return {"accepted": False, "error": "方案编号不匹配"}
        if str(plan.get("req_id") or "") != str(req_id or ""):
            return {"accepted": False, "error": "确认请求已过期，请重新生成方案"}
        if not approved:
            CP.mark_plan_status(plan, "cancelled", cancelled_at=time.time())
            source = plan.get("source") or {}
            if source.get("stream_id"):
                await self._try_reply(str(source["stream_id"]), f"清理方案 P{plan.get('code')} 已在桌宠端取消。")
            return {"accepted": True, "cancelled": True}
        if permanent and not bool(self.config.cleanup.allow_permanent_delete):
            return {"accepted": False, "error": "彻底删除未开启"}
        source = plan.get("source") or {}
        asyncio.create_task(
            self._bg_execute_cleanup(plan, selected, bool(permanent), str(source.get("stream_id") or ""))
        )
        return {"accepted": True}

def create_plugin() -> MarisaGatePlugin:
    """创建插件实例。"""
    return MarisaGatePlugin()
