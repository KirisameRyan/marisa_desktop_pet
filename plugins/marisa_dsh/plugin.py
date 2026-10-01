"""魔理沙 DSH 桥：用 QQ 远程驱动 DeepSeek Harness（多轮对话 + 过程可视化）。

三种用法：

1. **对话模式（推荐）**：``/dsh 开始`` 之后，主人直接发普通消息就是在跟 DSH 说话——
   一轮一轮地提要求、看结果、接着改，直到 ``/dsh 结束``。会话上下文连续，
   不用每条都带前缀，也不用重述做过什么。
2. **单次任务**：``/dsh <任务>`` 发一条就完事（后台跑，结果推回聊天）。
3. **工具调用**：``marisa_dsh_task`` 供模型主动把"要在电脑上真的动手"的请求转给 DSH。

**过程可视化**：走 ``dsh --profile <档> headless --json <任务>``，它的 NDJSON 事件流是
**实时**吐出的（实测：t=2.8s 起逐条出现），字段如下——

```
{"type":"session","sessionId":"…","cwd":"…"}
{"type":"status","phase":"turn_start"|"step_start"|"step_end"|"turn_end","turn":N,"step":N,"usage":{…}}
{"type":"thinking","text":"…"}                       ← 可读的推理过程
{"type":"tool_call","callId":"…","tool":"pwsh","input":{…}}
{"type":"tool_result","callId":"…","status":"completed","result":"…"}
{"type":"text","text":"…"}                            ← 回答流
{"type":"final","text":"…"}
```

所以这里不再只取 ``final.text``，而是**边跑边把工具调用/结果/思考推回 QQ**，让你看得见它
在干什么；``step_end`` 的 usage 逐轮累加，回执里给出真实开销。

关于"会话键"：命令侧拿到的 ``stream_id`` 与钩子侧 ``message["session_id"]`` 都是**同一个
聊天流标识**，两侧统一用它当键，对话模式才能把「/dsh 开始」和随后那条普通消息接到同一条
DSH 会话上。

其它实现要点：

- 任务以**参数列表**交给子进程（不拼 shell 字符串），``.cmd`` / ``.bat`` 经 ``cmd.exe /c``。
- 子进程 stdout 由**独立线程**逐行读取并投进 asyncio 队列——这样无论宿主用的是哪种事件
  循环策略都能工作，也不会阻塞事件循环。
- stderr 并入 stdout：DSH 的诊断也走 stderr，混进来由「不以 { 开头就跳过」过滤掉，
  同时避免两条管道互相塞死。
- 命令一律 ``permission="operator"``；工具与对话模式再做一次运行时主人校验
  （对话模式走 ``chat.receive.before_process`` 钩子，群友发的话绝不会被当成 DSH 指令）。
- 每次执行写审计日志 ``data/marisa/dsh_audit.log``，完整事件流落 ``data/marisa/dsh_runs/``。
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from maibot_sdk import Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import HookMode, HookOrder, ToolParameterInfo, ToolParamType

_PLUGIN_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _PLUGIN_DIR.parent.parent
_MARISA_DATA_DIR = _PROJECT_ROOT / "data" / "marisa"
_AUDIT_PATH = _MARISA_DATA_DIR / "dsh_audit.log"
_SESSION_PATH = _MARISA_DATA_DIR / "dsh_sessions.json"
_FULL_OUTPUT_DIR = _MARISA_DATA_DIR / "dsh_runs"

_OWNER_DENY_TEXT = "这事只有主人能让我做だぜ。"
_LAUNCH_DEFAULT = "D:\\deepseek_harness\\resources\\runtime\\cli\\bin\\dsh.cmd"
_CHAT_MODE = "chat"
_TOOL_OK_STATUS = ("completed", "success", "ok", "")

# 命令正则前缀：容忍 〔主人〕 标记、@At 与「魔理沙」前缀（与 marisa_gate 的写法一致）
_AT_MARK = r"(?:\s*〔[^〕]*〕\s*)?"
_TRIGGER = r"(?:\s*(?:\[At:[^\]]*\]|@[\w\u4e00-\u9fa5]+)\s*)*(?:魔理沙[，,、\s]*)?"


def cmd_pattern(inner: str) -> str:
    """统一命令正则前缀：容忍 @At 与「魔理沙」前缀。"""

    return "^" + _AT_MARK + _TRIGGER + "(?P<raw>" + inner + ")$"


def _deep_get(data: Any, path: List[str], default: Any = None) -> Any:
    """按路径取嵌套字典里的值，中途不是字典就返回默认值。"""

    current = data
    for key in path:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
        if current is None:
            return default
    return current


def _brief(value: Any, limit: int = 180) -> str:
    """把一段可能很长的文本压成一行短摘要（用于过程消息）。"""

    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _clip(text: str, limit: int) -> str:
    """按字符数截断，保留头尾——中段才是可有可无的日志。"""

    body = (text or "").strip()
    if limit <= 0 or len(body) <= limit:
        return body
    head = body[: int(limit * 0.6)]
    tail = body[-int(limit * 0.3):]
    return f"{head}\n…（中间省略 {len(body) - len(head) - len(tail)} 字，完整过程见 data/marisa/dsh_runs/）…\n{tail}"


def _parse_line(line: str) -> Optional[Dict[str, Any]]:
    """把一行 NDJSON 解析成事件字典；不是 JSON 就返回 None（stderr 也混在流里）。"""

    text = line.strip()
    if not text.startswith("{"):
        return None
    try:
        event = json.loads(text)
    except json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) else None


def _tool_input_brief(payload: Any) -> str:
    """从工具入参里挑一句最能说明"它在干什么"的话。"""

    if not isinstance(payload, dict):
        return _brief(payload)
    for key in ("description", "command", "path", "query", "task", "url"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return _brief(value)
    for value in payload.values():
        if isinstance(value, str) and value.strip():
            return _brief(value)
    return ""


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.1.0", description="配置版本")


class DshSectionConfig(PluginConfigBase):
    """DSH 执行配置。"""

    __ui_label__ = "DSH 执行"
    __ui_icon__ = "terminal"
    __ui_order__ = 1

    enabled: bool = Field(default=True, description="总开关：是否允许通过魔理沙驱动 DSH")
    cli_path: str = Field(default=_LAUNCH_DEFAULT, description="DSH CLI 启动器路径（dsh.cmd）")
    profile: str = Field(default="headless", description="用哪个 DSH 配置档跑任务")
    workdir: str = Field(default=str(_PROJECT_ROOT), description="任务的工作目录（DSH 在此目录下操作文件）")
    timeout_sec: float = Field(default=1800.0, description="单轮任务超时（秒）")
    max_output_chars: int = Field(default=1800, description="回传最终回答的上限（字符）")
    allow_task_tool: bool = Field(default=True, description="是否把跑任务暴露成 LLM 可见的工具")
    read_only_by_default: bool = Field(default=True, description="工具调用默认按只读下发")


class StreamSectionConfig(PluginConfigBase):
    """过程可视化：把 DSH 的执行过程实时推回聊天。"""

    __ui_label__ = "过程可视化"
    __ui_icon__ = "activity"
    __ui_order__ = 2

    enabled: bool = Field(default=True, description="是否把执行过程（工具调用/思考）实时推回聊天")
    show_tool_calls: bool = Field(default=True, description="推送「它调用了什么工具、干什么」")
    show_tool_results: bool = Field(default=True, description="推送工具返回结果（关掉后只推失败的）")
    show_thinking: bool = Field(default=False, description="推送模型的推理过程（比较啰嗦，默认关）")
    max_messages: int = Field(default=25, description="每轮最多推多少条过程消息，超出只回最终回答")


class ChatSectionConfig(PluginConfigBase):
    """对话模式：像聊天一样一轮轮地驱动 DSH。"""

    __ui_label__ = "对话模式"
    __ui_icon__ = "message-circle"
    __ui_order__ = 3

    enabled: bool = Field(default=True, description="允许用「/dsh 开始」进入逐轮对话模式")
    idle_timeout_min: float = Field(default=30.0, description="多久没说话自动退出对话模式（分钟）")


class SessionSectionConfig(PluginConfigBase):
    """会话保留配置。"""

    __ui_label__ = "会话保留"
    __ui_icon__ = "history"
    __ui_order__ = 4

    keep: bool = Field(default=True, description="保留 sessionId，让后续轮次接续同一次 DSH 会话")
    max_entries: int = Field(default=50, description="最多保留多少条会话记录")


class RulesSectionConfig(PluginConfigBase):
    """主人名单（回退用）。"""

    __ui_label__ = "规则"
    __ui_icon__ = "shield"
    __ui_order__ = 5

    owners: List[str] = Field(default_factory=lambda: ["3895418161"], description="主人 QQ 号；运行时优先取宿主 plugin.permission")


class MarisaDshConfig(PluginConfigBase):
    """魔理沙 DSH 桥配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    dsh: DshSectionConfig = Field(default_factory=DshSectionConfig)
    stream: StreamSectionConfig = Field(default_factory=StreamSectionConfig)
    chat: ChatSectionConfig = Field(default_factory=ChatSectionConfig)
    session: SessionSectionConfig = Field(default_factory=SessionSectionConfig)
    rules: RulesSectionConfig = Field(default_factory=RulesSectionConfig)


class _RunState:
    """一次执行的可变状态（回答、会话号、用量、过程消息计数）。"""

    def __init__(self, cap: int) -> None:
        self.chunks: List[str] = []
        self.final: str = ""
        self.session_id: str = ""
        self.steps: int = 0
        self.sent: int = 0
        self.suppressed: int = 0
        self.cap: int = cap
        self.usage: Dict[str, int] = {
            "inputTokens": 0,
            "cacheReadTokens": 0,
            "outputTokens": 0,
        }

    @property
    def answer(self) -> str:
        """最终回答：优先 final，其次把 text 流拼起来。"""

        return self.final.strip() or "".join(self.chunks).strip()


# ───────────────────────── 插件主体 ─────────────────────────


class MarisaDshPlugin(MaiBotPlugin):
    """把 DSH 接进 QQ：多轮对话、过程可见、远程迭代。"""

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
            "[marisa_dsh] 已加载：DSH %s，档=%s，工作目录=%s，CLI=%s，过程可视化=%s，对话模式=%s",
            "开启" if dsh.enabled else "关闭",
            dsh.profile,
            dsh.workdir,
            "已找到" if cli_ok else f"**找不到（{dsh.cli_path}）**",
            "开" if self.config.stream.enabled else "关",
            "开" if self.config.chat.enabled else "关",
        )
        if dsh.enabled and not cli_ok:
            self.ctx.logger.warning("[marisa_dsh] CLI 不存在，任务会直接失败；请检查 [dsh] cli_path")

    async def on_unload(self) -> None:
        self._save_sessions()

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        """配置热更新：重读设置，并在关掉会话保留时清空记录。

        宿主对每个插件都强制要求实现本方法（见 plugin_loader 的校验），
        所以这里不做"有没有配置变化"的猜测，只把当前生效值如实打出来。
        """

        dsh = self.config.dsh
        self.ctx.logger.info(
            "[marisa_dsh] 配置已更新（scope=%s，version=%s）：%s，档=%s，超时=%ss，过程可视化=%s",
            scope,
            version,
            "开启" if dsh.enabled else "关闭",
            dsh.profile,
            dsh.timeout_sec,
            "开" if self.config.stream.enabled else "关",
        )
        if not bool(self.config.session.keep):
            with self._session_lock:
                self._sessions = {}

    # ───────────────────────── 会话记录 ─────────────────────────

    def _load_sessions(self) -> None:
        """从磁盘读回会话记录。"""

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
        """把会话记录写回磁盘；对话模式中的记录优先保留，其余按最近使用淘汰。"""

        if not bool(self.config.session.keep):
            return
        try:
            _MARISA_DATA_DIR.mkdir(parents=True, exist_ok=True)
            with self._session_lock:
                items = sorted(
                    self._sessions.items(),
                    key=lambda kv: (str(kv[1].get("mode") or ""), str(kv[1].get("ts") or "")),
                    reverse=True,
                )
                keep = max(1, int(self.config.session.max_entries))
                trimmed = dict(items[:keep])
                self._sessions = trimmed
                _SESSION_PATH.write_text(json.dumps(trimmed, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("[marisa_dsh] 会话记录写入失败: %s", exc)

    def _entry(self, key: str) -> Dict[str, Any]:
        """取某个会话的记录（不存在返回空字典）。"""

        if not key:
            return {}
        with self._session_lock:
            return dict(self._sessions.get(key) or {})

    def _remember(self, key: str, session_id: str, task: str) -> None:
        """记下某个会话最近一次的 DSH sessionId（保留已有的对话模式标记）。"""

        if not key or not session_id:
            return
        with self._session_lock:
            entry = dict(self._sessions.get(key) or {})
            entry["session_id"] = session_id
            entry["ts"] = _dt.datetime.now().isoformat(timespec="seconds")
            entry["task"] = task[:200]
            self._sessions[key] = entry
        self._save_sessions()

    def _last_session(self, key: str) -> str:
        """取某个会话最近一次的 DSH sessionId（没有则空串）。"""

        return str(self._entry(key).get("session_id") or "")

    def _set_mode(self, key: str, mode: Optional[str]) -> None:
        """设置/清除某个会话的对话模式标记。"""

        if not key:
            return
        with self._session_lock:
            entry = dict(self._sessions.get(key) or {})
            if mode:
                entry["mode"] = mode
                entry["ts"] = _dt.datetime.now().isoformat(timespec="seconds")
            else:
                entry.pop("mode", None)
            if entry:
                self._sessions[key] = entry
            else:
                self._sessions.pop(key, None)
        self._save_sessions()

    def _chat_expired(self, key: str) -> bool:
        """对话模式是否已经闲置超时。"""

        entry = self._entry(key)
        stamp = str(entry.get("ts") or "")
        if not stamp:
            return True
        timeout = float(self.config.chat.idle_timeout_min)
        if timeout <= 0:
            return False
        try:
            last = _dt.datetime.fromisoformat(stamp)
        except ValueError:
            return True
        return (_dt.datetime.now() - last).total_seconds() > timeout * 60.0

    def _touch(self, key: str) -> None:
        """刷新会话时间戳，让闲置超时从最后一次对话重新计。"""

        if not key:
            return
        with self._session_lock:
            if key in self._sessions:
                self._sessions[key]["ts"] = _dt.datetime.now().isoformat(timespec="seconds")

    # ───────────────────────── 进程与事件 ─────────────────────────

    def _build_argv(self, task: str, session_id: str) -> List[str]:
        """拼出一次 headless 任务的命令行（参数列表，不拼 shell 字符串）。"""

        cli = str(self.config.dsh.cli_path or "").strip() or _LAUNCH_DEFAULT
        args = ["--profile", str(self.config.dsh.profile), "headless", "--json"]
        if session_id:
            args += ["--session-id", session_id]
        args.append(task)
        if cli.lower().endswith((".cmd", ".bat")):
            # Windows 不能直接 CreateProcess 执行 .cmd/.bat，必须经 cmd.exe
            return ["cmd.exe", "/c", cli, *args]
        return [cli, *args]

    def _spawn(self, task: str, session_id: str) -> subprocess.Popen:
        """起一个 DSH 子进程。"""

        cli = str(self.config.dsh.cli_path or "").strip() or _LAUNCH_DEFAULT
        if not Path(cli).is_file():
            raise FileNotFoundError(f"找不到 DSH 启动器：{cli}")
        return subprocess.Popen(  # noqa: S603 - 参数列表调用，非 shell 字符串
            self._build_argv(task, session_id),
            cwd=str(self.config.dsh.workdir),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=0x08000000,
        )

    async def _run(self, task: str, session_id: str, stream_id: str) -> Dict[str, Any]:
        """流式跑一轮 DSH：边读事件边把过程推回聊天，最后返回汇总。

        读取放在独立线程里（`for line in proc.stdout` 是阻塞的），通过
        `call_soon_threadsafe` 投进 asyncio 队列——不依赖宿主的 event loop 策略。
        """

        state = _RunState(cap=max(1, int(self.config.stream.max_messages)))
        timeout_sec = float(self.config.dsh.timeout_sec)
        raw_lines: List[str] = []
        timed_out = False
        self._running += 1

        try:
            proc = self._spawn(task, session_id)
        except Exception as exc:  # noqa: BLE001
            self._running -= 1
            return {"rc": -2, "answer": "", "session_id": "", "err": f"DSH 起不来：{exc}"}

        loop = asyncio.get_running_loop()
        queue: "asyncio.Queue[Optional[str]]" = asyncio.Queue()

        def _pump() -> None:
            """后台线程：逐行读子进程输出，投进事件循环。"""

            try:
                if proc.stdout is not None:
                    for line in proc.stdout:
                        loop.call_soon_threadsafe(queue.put_nowait, line)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        threading.Thread(target=_pump, name="marisa-dsh-reader", daemon=True).start()

        try:
            deadline = loop.time() + timeout_sec
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    timed_out = True
                    proc.kill()
                    break
                try:
                    line = await asyncio.wait_for(queue.get(), timeout=min(remaining, 5.0))
                except asyncio.TimeoutError:
                    continue
                if line is None:
                    break
                raw_lines.append(line)
                event = _parse_line(line)
                if event is None:
                    continue
                await self._handle_event(event, state, stream_id)
        finally:
            returncode = await asyncio.to_thread(proc.wait)
            self._running -= 1

        result = {
            "rc": -1 if timed_out else returncode,
            "answer": state.answer,
            "session_id": state.session_id,
            "usage": state.usage,
            "steps": state.steps,
            "sent": state.sent,
            "suppressed": state.suppressed,
            "err": "超时，已终止" if timed_out else "",
            "raw": "\n".join(raw_lines),
        }
        self._audit(task, session_id, result)
        return result

    async def _handle_event(self, event: Dict[str, Any], state: _RunState, stream_id: str) -> None:
        """处理一个事件：更新状态 + 按配置把过程推回聊天。"""

        kind = str(event.get("type") or "")
        if kind == "session":
            state.session_id = str(event.get("sessionId") or "")
            return
        if kind == "text":
            piece = str(event.get("text") or "")
            if piece:
                state.chunks.append(piece)
            return
        if kind == "final":
            state.final = str(event.get("text") or "")
            return
        if kind == "status":
            usage = event.get("usage")
            if isinstance(usage, dict):
                state.steps += 1
                for name in state.usage:
                    state.usage[name] += int(usage.get(name) or 0)
            return

        message = self._render_event(event)
        if not message or not stream_id or not bool(self.config.stream.enabled):
            return
        if state.sent >= state.cap:
            state.suppressed += 1
            return
        state.sent += 1
        await self._try_send(stream_id, message)

    def _render_event(self, event: Dict[str, Any]) -> Optional[str]:
        """把一个事件渲染成一条聊天消息；不推送的类型返回 None。"""

        stream = self.config.stream
        kind = str(event.get("type") or "")

        if kind == "tool_call":
            if not bool(stream.show_tool_calls):
                return None
            tool = str(event.get("tool") or "?")
            detail = _tool_input_brief(event.get("input"))
            return f"🔧 {tool}：{detail}" if detail else f"🔧 {tool}"

        if kind == "tool_result":
            failed = str(event.get("status") or "") not in _TOOL_OK_STATUS
            # 失败的工具结果无条件暴露（AGENTS：错误要完整暴露，不许被配置静默掉）
            if not failed and not bool(stream.show_tool_results):
                return None
            head = "⚠️" if failed else "↳"
            return f"{head} {_brief(event.get('result'), 220)}"

        if kind == "thinking":
            if not bool(stream.show_thinking):
                return None
            text = _brief(event.get("text"), 220)
            return f"🧠 {text}" if text else None

        return None

    def _audit(self, task: str, session_id: str, result: Dict[str, Any]) -> None:
        """写审计日志，并把完整事件流落盘（聊天里只回摘要）。"""

        try:
            _MARISA_DATA_DIR.mkdir(parents=True, exist_ok=True)
            _FULL_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            dump = _FULL_OUTPUT_DIR / f"dsh-{stamp}-rc{result.get('rc')}.log"
            dump.write_text(str(result.get("raw") or ""), encoding="utf-8")
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
                                "steps": result.get("steps"),
                                "usage": result.get("usage"),
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

    def _format_result(self, result: Dict[str, Any]) -> str:
        """把执行结果整理成适合聊天读的一段话。"""

        rc = result.get("rc")
        answer = str(result.get("answer") or "").strip()
        err = str(result.get("err") or "").strip()
        usage = result.get("usage") or {}
        steps = int(result.get("steps") or 0)

        if rc == -1:
            return f"DSH 那边超时了だぜ：{err or '任务没在限定时间内结束'}"
        if rc == -2:
            return str(err or "DSH 起不来")
        if not answer:
            tail = _brief(err, 400) or f"（rc={rc}，没有输出）"
            return f"DSH 跑完了但没给出回答，rc={rc}：\n{tail}"

        head = f"DSH 干完了（rc={rc}，{steps} 步）：\n{_clip(answer, int(self.config.dsh.max_output_chars))}"
        if int(result.get("suppressed") or 0):
            head += f"\n\n〔过程消息太多，省略了 {result['suppressed']} 条〕"
        if any(usage.values()):
            head += (
                f"\n\n〔本轮 tokens：新增 {usage.get('inputTokens', 0)}"
                f" / 缓存命中 {usage.get('cacheReadTokens', 0)}"
                f" / 输出 {usage.get('outputTokens', 0)}〕"
            )
        return head

    # ───────────────────────── 工具 ─────────────────────────

    @Tool("marisa_dsh_status", description="查看 DSH（DeepSeek Harness）桥的状态：是否可用、用哪个配置档、当前有没有任务在跑。")
    async def tool_dsh_status(self, **kwargs: Any) -> Dict[str, Any]:
        if not await self._caller_authorized(str(kwargs.get("platform") or ""), str(kwargs.get("user_id") or "")):
            return {"success": False, "content": _OWNER_DENY_TEXT}
        dsh = self.config.dsh
        cli = str(dsh.cli_path or "")
        lines = [
            "DSH 桥状态：",
            f"· 总开关：{'开启' if dsh.enabled else '关闭'}",
            f"· 配置档：{dsh.profile}",
            f"· 工作目录：{dsh.workdir}",
            f"· 启动器：{cli}（{'存在' if Path(cli).is_file() else '**不存在**'}）",
            f"· 过程可视化：{'开' if self.config.stream.enabled else '关'}",
            f"· 对话模式：{'开' if self.config.chat.enabled else '关'}",
            f"· 正在跑的任务：{self._running}",
        ]
        return {"success": True, "content": "\n".join(lines)}

    @Tool(
        "marisa_dsh_task",
        description=(
            "把一件需要在本机实际动手的事交给 DSH（DeepSeek Harness）执行，主人不在电脑前时用它远程干活。"
            "适用：更新/安装插件、改配置、在工作区里新建或整理文件与目录、跑一次迭代、排查报错、"
            "「帮我把某个文件夹加进工作区」这类要在电脑上真的动手的请求。"
            "任务会转到后台执行，执行过程与结果**直接发到这个会话**；本工具立刻返回「已提交」，"
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
        # 所以执行过程与结果都由后台任务直接推回会话，而不是等在这条工具调用里。
        stream_id = str(kwargs.get("stream_id") or "")
        if not stream_id:
            return {
                "success": False,
                "content": "拿不到会话上下文，没法把结果回推；请在 QQ 里直接用 /dsh <任务>。",
            }

        final_task = self._apply_read_only(body, bool(read_only))
        asyncio.create_task(self._bg_run(final_task, stream_id, stream_id, self._last_session(stream_id)))
        return {
            "success": True,
            "content": (
                f"已经把任务交给 DSH 在后台跑了（{dsh.profile} 档，工作目录 {dsh.workdir}），"
                "执行过程和结果会直接发到这个会话——在结果到达之前，不要跟主人说任务已经完成。"
            ),
        }

    # ───────────────────────── 对话模式（钩子） ─────────────────────────

    @HookHandler(
        "chat.receive.before_process",
        name="marisa_dsh_conversation",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
    )
    async def handle_conversation(self, **kwargs: Any) -> Dict[str, Any]:
        """对话模式：处于该模式的会话里，主人发的普通消息直接作为下一轮交给 DSH。

        用 LATE 顺序，让 marisa_gate 的门控先跑完——被门控挡下的消息根本不会到这。
        """

        cont = {"action": "continue", "modified_kwargs": kwargs}
        try:
            if not bool(self.config.dsh.enabled) or not bool(self.config.chat.enabled):
                return cont
            message = kwargs.get("message")
            if not isinstance(message, dict):
                return cont
            if bool(_deep_get(message, ["is_notify"], False)):
                return cont
            platform = str(_deep_get(message, ["platform"], "qq") or "qq")
            if platform != "qq":
                return cont
            text = str(_deep_get(message, ["processed_plain_text"], "") or "").strip()
            # 斜杠指令照旧走命令链，不由这里接管
            if not text or text.startswith("/"):
                return cont

            # 命令侧拿到的 stream_id 与这里的 session_id 是同一个聊天流标识
            key = str(_deep_get(message, ["session_id"], "") or "")
            entry = self._entry(key)
            if entry.get("mode") != _CHAT_MODE:
                return cont

            sender = str(_deep_get(message, ["message_info", "user_info", "user_id"], "") or "")
            if not await self._caller_authorized(platform, sender):
                # 群友在对话模式下发话：放回主链，绝不喂给 DSH
                return cont

            if self._chat_expired(key):
                self._set_mode(key, None)
                await self._try_send(key, "DSH 对话模式闲置太久，已经自动退出了だぜ（要接着聊就再发 /dsh 开始）。")
                return cont

            self._touch(key)
            await self._try_send(key, "▶ 收到，交给 DSH 了…")
            asyncio.create_task(self._bg_run(text, key, key, self._last_session(key)))
            return {"action": "abort"}
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("[marisa_dsh] 对话模式拦截异常: %s", exc)
            return cont

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
        raw = str((matched or {}).get("raw") or "") if isinstance(matched, dict) else ""

        if not await self._caller_authorized(platform, user_id):
            await self._try_send(stream_id, _OWNER_DENY_TEXT)
            return True, "", True

        dsh = self.config.dsh
        if not dsh.enabled:
            await self._try_send(stream_id, "DSH 桥的总开关没开だぜ。")
            return True, "", True

        body = raw.strip()[len("/dsh"):].strip() if raw.strip().startswith("/dsh") else ""
        # 会话键统一用回复目标（与钩子里的 message["session_id"] 同一个值）
        key = stream_id or f"{platform or 'qq'}:{kwargs.get('chat_id') or kwargs.get('group_id') or user_id or '?'}"

        if not body or body in ("帮助", "help", "?"):
            await self._try_send(stream_id, self._help_text())
            return True, "", True

        if body == "状态":
            status = await self.tool_dsh_status(platform=platform, user_id=user_id)
            await self._try_send(stream_id, str(status.get("content") or ""))
            return True, "", True

        if body == "会话":
            entry = self._entry(key)
            mode = "对话模式" if entry.get("mode") == _CHAT_MODE else "单次"
            await self._try_send(stream_id, f"当前会话：{entry.get('session_id') or '（还没有）'}（{mode}）")
            return True, "", True

        if body == "忘记":
            with self._session_lock:
                self._sessions.pop(key, None)
            self._save_sessions()
            await self._try_send(stream_id, "好，忘掉了，下次开新会话だぜ。")
            return True, "", True

        if body == "开始":
            if not bool(self.config.chat.enabled):
                await self._try_send(stream_id, "配置里关掉了对话模式だぜ。")
                return True, "", True
            self._set_mode(key, _CHAT_MODE)
            await self._try_send(
                stream_id,
                "好，进入 DSH 对话模式だぜ。接下来你直接说话就是在跟 DSH 讲，一轮一轮改到满意为止；"
                "要退出发「/dsh 结束」。",
            )
            return True, "", True

        if body == "结束":
            self._set_mode(key, None)
            await self._try_send(stream_id, "好，退出 DSH 对话模式了だぜ。")
            return True, "", True

        # 默认接续本会话上一次的 DSH 会话：上下文连续，主人不用重述做过什么；
        # 要干净地另起一次就用「/dsh 新 <任务>」。
        reuse = ""
        if body.startswith("新"):
            body = body[len("新"):].strip()
            if not body:
                await self._try_send(stream_id, "开新会话也要带任务：/dsh 新 <任务>")
                return True, "", True
        else:
            reuse = self._last_session(key)

        note = f"接上次那次会话（{reuse}）" if reuse else "开一次新会话"
        await self._try_send(stream_id, f"收到，{note}交给 DSH 了，过程我会实时发你だぜ。")
        asyncio.create_task(self._bg_run(body, key, stream_id, reuse))
        return True, "", True

    def _help_text(self) -> str:
        """命令帮助文本。"""

        dsh = self.config.dsh
        return (
            "用法：\n"
            "· /dsh 开始 —— 进入 DSH 对话模式，之后直接说话就是一轮一轮地指挥它\n"
            "· /dsh 结束 —— 退出对话模式\n"
            "· /dsh <任务> —— 单次任务（默认接续上次会话），过程实时发你\n"
            "· /dsh 新 <任务> —— 不管上次，干净地开一次新会话\n"
            "· /dsh 状态 / 会话 / 忘记 —— 看状态 / 看会话号 / 丢掉会话号\n"
            f"当前跑在 {dsh.profile} 档，工作目录 {dsh.workdir}，"
            f"过程可视化 {'开' if self.config.stream.enabled else '关'}"
        )

    async def _bg_run(self, task: str, key: str, stream_id: str, session_id: str) -> None:
        """后台跑一轮；事件流由 _run 边读边推，这里只负责收尾与记账。"""

        try:
            result = await self._run(task, session_id, stream_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            await self._try_send(stream_id, f"DSH 任务异常：{type(exc).__name__}: {exc}")
            return

        self._remember(key, str(result.get("session_id") or ""), task)
        await self._try_send(stream_id, self._format_result(result))

    # ───────────────────────── 小工具 ─────────────────────────

    def _apply_read_only(self, task: str, read_only: bool) -> str:
        """按只读策略给任务附加约束。"""

        if not (read_only and bool(self.config.dsh.read_only_by_default)):
            return task
        return f"{task}\n\n（只读：不要修改任何文件，只分析并给出结论与建议。）"

    async def _try_send(self, stream_id: str, text: str) -> None:
        """往会话里发一条文本；发不出去只记日志，不打断执行。"""

        if not stream_id or not text:
            return
        try:
            await self.ctx.send.text(text, stream_id)
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("[marisa_dsh] 消息发送失败: %s", exc)


def create_plugin() -> MarisaDshPlugin:
    """创建插件实例。"""

    return MarisaDshPlugin()
