"""魔理沙 DSH 桥：用 QQ 远程驱动 DeepSeek Harness。

场景：主人不在电脑前，用 QQ 让魔理沙把 DSH 跑起来做事——更新插件、改配置、
跑一次迭代，然后把结果回传。需要来回改的，用「/dsh 继续」接续同一个会话。

实现要点：

1. 走 DSH 自带的一次性任务入口 ``dsh --profile <档> headless --json <任务>``。
   ``--json`` 输出 NDJSON 事件流，字段是实测确认过的：

   ```
   {"type":"session","sessionId":"session-…","cwd":"…"}
   {"type":"status","phase":"turn_start","turn":1}
   {"type":"thinking","text":""}
   {"type":"text","text":"…"}
   {"type":"status","phase":"step_end","usage":{"totalTokens":…}}
   {"type":"status","phase":"turn_end","reason":{"kind":"completed"}}
   {"type":"final","text":"…"}
   ```

   取 ``final.text`` 作为回答、``session.sessionId`` 作为可接续的会话号。

2. 任务以**参数列表**交给子进程，不拼 shell 字符串——中文与引号不会被吃掉。
   ``.cmd`` / ``.bat`` 必须经 ``cmd.exe /c``（Windows 不能直接 CreateProcess 执行脚本）。

3. 命令一律 ``permission="operator"``（宿主按 plugin.permission 名单先行拦截），
   工具再做一次运行时主人校验——与 marisa_gate 的做法一致。

4. 每次执行写审计日志 ``data/marisa/dsh_audit.log``，完整输出落盘、QQ 只回摘要。
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

_PLUGIN_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _PLUGIN_DIR.parent.parent
_MARISA_DATA_DIR = _PROJECT_ROOT / "data" / "marisa"
_AUDIT_PATH = _MARISA_DATA_DIR / "dsh_audit.log"
_SESSION_PATH = _MARISA_DATA_DIR / "dsh_sessions.json"
_FULL_OUTPUT_DIR = _MARISA_DATA_DIR / "dsh_runs"

_OWNER_DENY_TEXT = "这事只有主人能让我做だぜ。"
_LAUNCH_DEFAULT = "D:\\deepseek_harness\\resources\\runtime\\cli\\bin\\dsh.cmd"

# 命令正则前缀：容忍 〔主人〕 标记、@At 与「魔理沙」前缀（与 marisa_gate 的写法一致）
_AT_MARK = r"(?:\s*〔[^〕]*〕\s*)?"
_TRIGGER = r"(?:\s*(?:\[At:[^\]]*\]|@[\w\u4e00-\u9fa5]+)\s*)*(?:魔理沙[，,、\s]*)?"


def cmd_pattern(inner: str) -> str:
    """统一命令正则前缀：容忍 @At 与「魔理沙」前缀。"""

    return "^" + _AT_MARK + _TRIGGER + "(?P<raw>" + inner + ")$"


def _parse_events(stdout: str) -> Dict[str, Any]:
    """从 headless 的 NDJSON 事件流里取出回答、会话号与用量。"""

    chunks: List[str] = []
    final_text = ""
    session_id = ""
    usage: Dict[str, Any] = {}
    end_reason = ""

    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = str(event.get("type") or "")
        if kind == "session":
            session_id = str(event.get("sessionId") or "")
        elif kind == "text":
            piece = str(event.get("text") or "")
            if piece:
                chunks.append(piece)
        elif kind == "final":
            final_text = str(event.get("text") or "")
        elif kind == "status":
            if isinstance(event.get("usage"), dict):
                usage = event["usage"]
            reason = event.get("reason")
            if isinstance(reason, dict) and reason.get("kind"):
                end_reason = str(reason["kind"])

    answer = final_text.strip() or "".join(chunks).strip()
    return {
        "answer": answer,
        "session_id": session_id,
        "usage": usage,
        "end_reason": end_reason,
    }


def _clip(text: str, limit: int) -> str:
    """按字符数截断，保留头尾——中段才是可有可无的日志。"""

    body = (text or "").strip()
    if limit <= 0 or len(body) <= limit:
        return body
    head = body[: int(limit * 0.6)]
    tail = body[-int(limit * 0.3):]
    return f"{head}\n…（中间省略 {len(body) - len(head) - len(tail)} 字，完整输出见 data/marisa/dsh_runs/）…\n{tail}"


class DshSectionConfig(PluginConfigBase):
    """DSH 执行配置。"""

    __ui_label__ = "DSH 执行"
    __ui_icon__ = "terminal"
    __ui_order__ = 1

    enabled: bool = Field(default=True, description="总开关：是否允许通过魔理沙驱动 DSH")
    cli_path: str = Field(default=_LAUNCH_DEFAULT, description="DSH CLI 启动器路径（dsh.cmd）")
    profile: str = Field(default="headless", description="用哪个 DSH 配置档跑任务")
    workdir: str = Field(default=str(_PROJECT_ROOT), description="任务的工作目录（DSH 在此目录下操作文件）")
    timeout_sec: float = Field(default=900.0, description="单次任务超时（秒）")
    max_output_chars: int = Field(default=1800, description="回传 QQ 的输出上限（字符）")
    allow_task_tool: bool = Field(default=True, description="是否把跑任务暴露成 LLM 可见的工具")
    read_only_by_default: bool = Field(default=True, description="工具调用默认按只读下发")


class SessionSectionConfig(PluginConfigBase):
    """会话接续配置。"""

    __ui_label__ = "会话接续"
    __ui_icon__ = "history"
    __ui_order__ = 2

    keep: bool = Field(default=True, description="保留 sessionId，让「/dsh 继续」能接续同一次会话")
    max_entries: int = Field(default=50, description="最多保留多少条会话记录")


class RulesSectionConfig(PluginConfigBase):
    """主人名单（回退用）。"""

    __ui_label__ = "规则"
    __ui_icon__ = "shield"
    __ui_order__ = 3

    owners: List[str] = Field(default_factory=lambda: ["3895418161"], description="主人 QQ 号；运行时优先取宿主 plugin.permission")


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.0.0", description="配置版本")


class MarisaDshConfig(PluginConfigBase):
    """魔理沙 DSH 桥配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    dsh: DshSectionConfig = Field(default_factory=DshSectionConfig)
    session: SessionSectionConfig = Field(default_factory=SessionSectionConfig)
    rules: RulesSectionConfig = Field(default_factory=RulesSectionConfig)


# ───────────────────────── 插件主体 ─────────────────────────


class MarisaDshPlugin(MaiBotPlugin):
    """把 DSH 接进 QQ：主人远程提交任务、拿回结果、接续会话。"""

    config_model = MarisaDshConfig

    def __init__(self) -> None:
        super().__init__()
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._session_lock = threading.Lock()
        self._running: int = 0

    # ───────────────────────── 生命周期 ─────────────────────────

    async def on_load(self) -> None:
        dsh = self.config.dsh
        self._load_sessions()
        cli_ok = Path(str(dsh.cli_path)).is_file()
        self.ctx.logger.info(
            "[marisa_dsh] 已加载：DSH %s，档=%s，工作目录=%s，CLI=%s",
            "开启" if dsh.enabled else "关闭",
            dsh.profile,
            dsh.workdir,
            "已找到" if cli_ok else f"**找不到（{dsh.cli_path}）**",
        )
        if dsh.enabled and not cli_ok:
            self.ctx.logger.warning("[marisa_dsh] CLI 不存在，任务会直接失败；请检查 [dsh] cli_path")

    async def on_unload(self) -> None:
        self._save_sessions()

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        """配置热更新：重读 DSH 设置，并在关掉会话保留时清空记录。

        宿主对每个插件都强制要求实现本方法（见 plugin_loader 的校验），
        所以这里不做"有没有配置变化"的猜测，只把当前生效值如实打出来。
        """

        dsh = self.config.dsh
        self.ctx.logger.info(
            "[marisa_dsh] 配置已更新（scope=%s，version=%s）：%s，档=%s，工作目录=%s，超时=%ss",
            scope,
            version,
            "开启" if dsh.enabled else "关闭",
            dsh.profile,
            dsh.workdir,
            dsh.timeout_sec,
        )
        if not bool(self.config.session.keep):
            with self._session_lock:
                self._sessions = {}

    # ───────────────────────── 会话记录 ─────────────────────────

    def _load_sessions(self) -> None:
        """从磁盘读回会话号。"""

        if not bool(self.config.session.keep):
            self._sessions = {}
            return
        try:
            if _SESSION_PATH.is_file():
                data = json.loads(_SESSION_PATH.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._sessions = {str(k): v for k, v in data.items() if isinstance(v, dict)}
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("[marisa_dsh] 会话记录读取失败: %s", exc)
            self._sessions = {}

    def _save_sessions(self) -> None:
        """把会话号写回磁盘；只保留最近使用的若干条。"""

        if not bool(self.config.session.keep):
            return
        try:
            _MARISA_DATA_DIR.mkdir(parents=True, exist_ok=True)
            with self._session_lock:
                items = sorted(self._sessions.items(), key=lambda kv: str(kv[1].get("ts") or ""), reverse=True)
                keep = max(1, int(self.config.session.max_entries))
                trimmed = dict(items[:keep])
                self._sessions = trimmed
                _SESSION_PATH.write_text(json.dumps(trimmed, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("[marisa_dsh] 会话记录写入失败: %s", exc)

    def _remember_session(self, key: str, session_id: str, task: str) -> None:
        """记下某个会话最近一次的 DSH sessionId。"""

        if not session_id:
            return
        with self._session_lock:
            self._sessions[key] = {
                "session_id": session_id,
                "ts": _dt.datetime.now().isoformat(timespec="seconds"),
                "task": task[:200],
            }
        self._save_sessions()

    def _last_session(self, key: str) -> str:
        """取某个会话最近一次的 DSH sessionId（没有则空串）。"""

        with self._session_lock:
            entry = self._sessions.get(key) or {}
        return str(entry.get("session_id") or "")

    # ───────────────────────── 执行 ─────────────────────────

    def _resolve_cli(self) -> str:
        """返回 DSH 启动器路径（配置优先，其次探测默认位置）。"""

        configured = str(self.config.dsh.cli_path or "").strip()
        if configured and Path(configured).is_file():
            return configured
        return configured or _LAUNCH_DEFAULT

    def _build_argv(self, task: str, session_id: str) -> List[str]:
        """拼出一次 headless 任务的命令行（参数列表，不拼 shell 字符串）。"""

        cli = self._resolve_cli()
        args = ["--profile", str(self.config.dsh.profile), "headless", "--json"]
        if session_id:
            args += ["--session-id", session_id]
        args.append(task)
        if cli.lower().endswith((".cmd", ".bat")):
            # Windows 不能直接 CreateProcess 执行 .cmd/.bat，必须经 cmd.exe
            return ["cmd.exe", "/c", cli, *args]
        return [cli, *args]

    def _run_sync(self, task: str, session_id: str, timeout_sec: float) -> Dict[str, Any]:
        """同步跑一次 DSH 任务（由 asyncio.to_thread 调用，避免阻塞事件循环）。"""

        cli = self._resolve_cli()
        if not Path(cli).is_file():
            return {"rc": -2, "answer": "", "session_id": "", "err": f"找不到 DSH 启动器：{cli}", "raw": ""}

        argv = self._build_argv(task, session_id)
        try:
            proc = subprocess.run(
                argv,
                cwd=str(self.config.dsh.workdir),
                capture_output=True,
                timeout=float(timeout_sec),
                creationflags=0x08000000,
            )
        except subprocess.TimeoutExpired:
            return {"rc": -1, "answer": "", "session_id": "", "err": f"超时（>{timeout_sec:.0f}s），已终止", "raw": ""}
        except Exception as exc:  # noqa: BLE001
            return {"rc": -2, "answer": "", "session_id": "", "err": f"无法执行：{exc}", "raw": ""}

        stdout = proc.stdout.decode("utf-8", "replace")
        stderr = proc.stderr.decode("utf-8", "replace")
        parsed = _parse_events(stdout)
        return {
            "rc": proc.returncode,
            "answer": parsed["answer"],
            "session_id": parsed["session_id"],
            "usage": parsed["usage"],
            "end_reason": parsed["end_reason"],
            "err": stderr.strip(),
            "raw": stdout,
        }

    async def _run(self, task: str, session_id: str = "") -> Dict[str, Any]:
        """异步包装：DSH 是 CPU/IO 都可能很重的子进程，放线程里跑。"""

        self._running += 1
        try:
            result = await asyncio.to_thread(
                self._run_sync, task, session_id, float(self.config.dsh.timeout_sec)
            )
        finally:
            self._running -= 1
        self._audit(task, session_id, result)
        return result

    def _audit(self, task: str, session_id: str, result: Dict[str, Any]) -> None:
        """写审计日志，并把完整输出落盘（QQ 只回摘要）。"""

        try:
            _MARISA_DATA_DIR.mkdir(parents=True, exist_ok=True)
            _FULL_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            dump = _FULL_OUTPUT_DIR / f"dsh-{stamp}-rc{result.get('rc')}.json"
            dump.write_text(
                json.dumps(
                    {
                        "ts": _dt.datetime.now().isoformat(timespec="seconds"),
                        "task": task,
                        "session_in": session_id,
                        "session_out": result.get("session_id"),
                        "rc": result.get("rc"),
                        "end_reason": result.get("end_reason"),
                        "usage": result.get("usage"),
                        "answer": result.get("answer"),
                        "stderr": result.get("err"),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            with self._session_lock:
                with open(_AUDIT_PATH, "a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(
                            {
                                "ts": _dt.datetime.now().isoformat(timespec="seconds"),
                                "task": task[:200],
                                "session_in": session_id,
                                "session_out": result.get("session_id"),
                                "rc": result.get("rc"),
                                "answer_tail": str(result.get("answer") or "")[-200:],
                                "dump": str(dump),
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("[marisa_dsh] 审计日志写入失败: %s", exc)

    # ───────────────────────── 权限 ─────────────────────────

    async def _caller_authorized(self, platform: str, user_id: str) -> bool:
        """主人判定：优先宿主 plugin.permission 的 qq 名单，回退插件自身配置。"""

        if platform and platform != "qq":
            return False
        allowed: List[str] = []
        try:
            permission = await self.ctx.config.get("plugin.permission", []) or []
            allowed = [str(x).split(":", 1)[1] for x in permission if str(x).startswith("qq:")]
        except Exception:  # noqa: BLE001
            allowed = []
        allowed += [str(x) for x in (self.config.rules.owners or [])]
        return str(user_id) in {x.strip() for x in allowed if str(x).strip()}

    # ───────────────────────── 输出整形 ─────────────────────────

    def _format_result(self, task: str, result: Dict[str, Any]) -> str:
        """把执行结果整理成适合 QQ 读的一段话。"""

        rc = result.get("rc")
        answer = str(result.get("answer") or "").strip()
        err = str(result.get("err") or "").strip()
        usage = result.get("usage") or {}

        if rc == -1:
            return f"DSH 那边超时了だぜ：{err or '任务没在限定时间内结束'}"
        if rc == -2:
            return f"DSH 起不来：{err}"
        if not answer:
            tail = _clip(err, 600) or f"（rc={rc}，没有输出）"
            return f"DSH 跑完了但没给出回答，rc={rc}：\n{tail}"

        head = f"DSH 干完了（rc={rc}）：\n{_clip(answer, int(self.config.dsh.max_output_chars))}"
        if usage.get("totalTokens"):
            head += f"\n\n〔本轮 {usage.get('totalTokens')} tokens〕"
        return head

    # ───────────────────────── 工具 ─────────────────────────

    @Tool("marisa_dsh_status", description="查看 DSH（DeepSeek Harness）桥的状态：是否可用、用哪个配置档、当前有没有任务在跑。")
    async def tool_dsh_status(self, **kwargs: Any) -> Dict[str, Any]:
        if not await self._caller_authorized(str(kwargs.get("platform") or ""), str(kwargs.get("user_id") or "")):
            return {"success": False, "content": _OWNER_DENY_TEXT}
        dsh = self.config.dsh
        cli = self._resolve_cli()
        lines = [
            "DSH 桥状态：",
            f"· 总开关：{'开启' if dsh.enabled else '关闭'}",
            f"· 配置档：{dsh.profile}",
            f"· 工作目录：{dsh.workdir}",
            f"· 启动器：{cli}（{'存在' if Path(cli).is_file() else '**不存在**'}）",
            f"· 正在跑的任务：{self._running}",
        ]
        return {"success": True, "content": "\n".join(lines)}

    @Tool(
        "marisa_dsh_task",
        description=(
            "把一件需要在本机实际动手的事交给 DSH（DeepSeek Harness）执行，主人不在电脑前时用它远程干活。"
            "适用：更新/安装插件、改配置、在工作区里新建或整理文件与目录、跑一次迭代、排查报错、"
            "「帮我把某个文件夹加进工作区」这类要在电脑上真的动手的请求。"
            "任务会转到后台执行，结果**直接发到这个会话**；本工具立刻返回「已提交」，"
            "所以在结果到达之前不要告诉主人任务已经完成。"
            "read_only=true（默认）会在任务里附加「不要改动任何文件」的约束；主人明确要求改动时才传 false。"
        ),
        parameters=[
            ToolParameterInfo(name="task", param_type=ToolParamType.STRING, description="要交给 DSH 的任务描述", required=True),
            ToolParameterInfo(name="read_only", param_type=ToolParamType.BOOLEAN, description="是否只读执行（不改动文件）", required=False),
        ],
    )
    async def tool_dsh_task(self, task: str = "", read_only: bool = True, **kwargs: Any) -> Dict[str, Any]:
        if not await self._caller_authorized(str(kwargs.get("platform") or ""), str(kwargs.get("user_id") or "")):
            return {"success": False, "content": _OWNER_DENY_TEXT}
        dsh = self.config.dsh
        if not dsh.enabled:
            return {"success": False, "content": "DSH 桥的总开关没开だぜ。"}
        if not bool(dsh.allow_task_tool):
            return {"success": False, "content": "配置里关掉了工具通道，请在 QQ 里用 /dsh 吧。"}
        body = str(task or "").strip()
        if not body:
            return {"success": False, "content": "要交给 DSH 的任务是空的だぜ。"}

        # 交给后台跑：工具本身必须立刻返回。DSH 动辄几分钟，阻塞在这里会把整个 Planner 卡住，
        # 所以结果由后台任务直接推回会话，而不是等在这条工具调用里。
        stream_id = str(kwargs.get("stream_id") or "")
        if not stream_id:
            return {
                "success": False,
                "content": "拿不到会话上下文，没法把结果回推；请在 QQ 里直接用 /dsh <任务>。",
            }

        platform = str(kwargs.get("platform") or "")
        user_id = str(kwargs.get("user_id") or "")
        session_key = f"{platform}:{kwargs.get('chat_id') or kwargs.get('group_id') or user_id or '?'}"
        effective_read_only = bool(read_only) and bool(dsh.read_only_by_default)
        final_task = body if not effective_read_only else f"{body}\n\n（只读：不要修改任何文件，只分析并给出结论与建议。）"

        asyncio.create_task(self._bg_run(final_task, session_key, stream_id, ""))
        return {
            "success": True,
            "content": (
                f"已经把任务交给 DSH 在后台跑了（{dsh.profile} 档，工作目录 {dsh.workdir}）。"
                "跑完我会把结果直接发到这个会话——在结果到达之前，不要跟主人说任务已经完成。"
            ),
        }

    # ───────────────────────── 命令（仅主人） ─────────────────────────

    @Command(
        "marisa_dsh",
        description="远程驱动 DSH：/dsh <任务>（仅管理员）",
        pattern=cmd_pattern(r"/dsh(?:\s+[\s\S]*)?\s*$"),
        permission="operator",
    )
    async def cmd_dsh(self, **kwargs: Any) -> tuple:
        if not isinstance(kwargs, dict):
            return False, "", False

        stream_id = str(kwargs.get("stream_id") or "")
        platform = str(kwargs.get("platform") or "")
        user_id = str(kwargs.get("user_id") or "")
        matched = kwargs.get("matched_groups")
        raw = str((matched or {}).get("raw") or kwargs.get("text") or "") if isinstance(matched, dict) else ""

        if not await self._caller_authorized(platform, user_id):
            if stream_id:
                await self.ctx.send.text(_OWNER_DENY_TEXT, stream_id)
            return True, "", True

        dsh = self.config.dsh
        if not dsh.enabled:
            if stream_id:
                await self.ctx.send.text("DSH 桥的总开关没开だぜ。", stream_id)
            return True, "", True

        body = raw.strip()[len("/dsh"):].strip() if raw.strip().startswith("/dsh") else ""
        session_key = f"{platform}:{kwargs.get('chat_id') or kwargs.get('group_id') or user_id or '?'}"

        if not body or body in ("帮助", "help", "?"):
            if stream_id:
                await self.ctx.send.text(
                    "用法：\n"
                    "· /dsh <任务> —— 交给 DSH 执行，完成后把结果发你\n"
                    "· /dsh 继续 <补充> —— 接续上一次 DSH 会话继续改\n"
                    "· /dsh 状态 —— 看桥和 CLI 的状态\n"
                    "· /dsh 会话 —— 看当前记着的会话号\n"
                    "· /dsh 忘记 —— 丢掉当前会话号（下次开新会话）\n"
                    f"当前跑在 {dsh.profile} 档，工作目录 {dsh.workdir}",
                    stream_id,
                )
            return True, "", True

        if body == "状态":
            status = await self.tool_dsh_status(platform=platform, user_id=user_id)
            if stream_id:
                await self.ctx.send.text(str(status.get("content") or ""), stream_id)
            return True, "", True

        if body == "会话":
            sid = self._last_session(session_key)
            if stream_id:
                await self.ctx.send.text(f"当前会话：{sid or '（还没有）'}", stream_id)
            return True, "", True

        if body == "忘记":
            with self._session_lock:
                self._sessions.pop(session_key, None)
            self._save_sessions()
            if stream_id:
                await self.ctx.send.text("好，忘掉了，下次开新会话だぜ。", stream_id)
            return True, "", True

        if body.startswith("继续"):
            sid = self._last_session(session_key)
            if not sid:
                if stream_id:
                    await self.ctx.send.text("还没有可接续的会话だぜ，先用 /dsh <任务> 开一个。", stream_id)
                return True, "", True
            follow_up = body[len("继续"):].strip()
            if not follow_up:
                if stream_id:
                    await self.ctx.send.text("接续要带上补充说明：/dsh 继续 <补充>", stream_id)
                return True, "", True
            if stream_id:
                await self.ctx.send.text(f"好，接上次那次会话继续（{sid}），完成后把结果发你だぜ。", stream_id)
            asyncio.create_task(self._bg_run(follow_up, session_key, stream_id, sid))
            return True, "", True

        if stream_id:
            await self.ctx.send.text("收到，交给 DSH 了，跑完把结果发你だぜ。", stream_id)
        asyncio.create_task(self._bg_run(body, session_key, stream_id, ""))
        return True, "", True

    async def _bg_run(self, task: str, session_key: str, stream_id: str, session_id: str) -> None:
        """后台跑任务；结果直接推回会话，不占住这一轮对话。"""

        try:
            result = await self._run(task, session_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if stream_id:
                await self.ctx.send.text(f"DSH 任务异常：{type(exc).__name__}: {exc}", stream_id)
            return

        self._remember_session(session_key, str(result.get("session_id") or ""), task)
        if stream_id:
            await self.ctx.send.text(self._format_result(task, result), stream_id)


def create_plugin() -> MarisaDshPlugin:
    """创建插件实例。"""

    return MarisaDshPlugin()
