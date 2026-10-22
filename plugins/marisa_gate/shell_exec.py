# -*- coding: utf-8 -*-
"""marisa-gate 终端执行引擎（只读白名单直跑 / 其余提案-确认 / 黑名单拒绝 / 全量审计）

- 执行宿主：MaiBot 插件进程（与桌宠同用户、同桌面会话）
- 命令一律经 PowerShell 子进程执行，带超时与输出截断
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

MARISA_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "marisa"
MARISA_DATA_DIR.mkdir(parents=True, exist_ok=True)
PENDING_PATH = MARISA_DATA_DIR / "pending_cmd.json"
AUDIT_PATH = MARISA_DATA_DIR / "shell_audit.log"

# ---------------- 策略 ---------------- #
_BLACKLIST_RE = [
    re.compile(r"\bnet\s+(user|localgroup|accounts)\b", re.I),
    re.compile(r"\bformat\s+[a-z]:", re.I),
    re.compile(r"\bdiskpart\b", re.I),
    re.compile(r"\breg\s+delete\b", re.I),
    re.compile(r"\bRemove-Item\b", re.I),
    re.compile(r"\brm\s+-(?:r|rf|recurse)\b", re.I),
    re.compile(r"\brmdir\s+/s\b", re.I),
    re.compile(r"\bdel\s+/[a-z]*s", re.I),
    re.compile(r"\btaskkill\b", re.I),
    re.compile(r"\bStop-Process\b", re.I),
    re.compile(r"\bicacls\b", re.I),
    re.compile(r"\btakeown\b", re.I),
    re.compile(r"\bcipher\b", re.I),
    re.compile(r"(?:MarisaBOT|魔理沙桌宠)[^\n]*(?:Remove-Item|rmdir|del\s)", re.I),
]

_SAFE_READONLY_RE = [
    re.compile(r"^git\s+(?:-C\s+\S+\s+)?(?:status|log|diff|show|branch|remote|fetch|tag|stash\s+list|config\s+--get|rev-parse|ls-files)\b", re.I),
    re.compile(r"^(?:pwd|dir|ls|get-childitem|where\b|where.exe|echo|get-date|hostname|ipconfig|systeminfo|ver|whoami)\b", re.I),
    re.compile(r"^(?:git|python)\s+--version\b", re.I),
]

_FORCE_CONFIRM_TOKENS = (
    "git push", "git commit", "git reset", "git clean", "git checkout .",
    "Remove-Item", "rmdir /s", "rd /s", "format ", "diskpart",
    "shutdown", "restart", "Stop-Process", "taskkill", "net user",
    "reg delete", "icacls", "takeown", "Move-Item -Force", "Copy-Item -Force",
)

_lock = threading.RLock()


def _audit(kind: str, mode: str, cmd: str, cwd: str, rc: int, tail: str) -> None:
    try:
        with _lock:
            with open(AUDIT_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": _dt.datetime.now().isoformat(timespec="seconds"),
                    "kind": kind,
                    "mode": mode,
                    "cmd": cmd[:300],
                    "cwd": cwd,
                    "rc": rc,
                    "tail": tail[:200],
                }, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _decode_console(raw: bytes) -> str:
    """解码子进程输出。

    PowerShell 5.1 在管道重定向下是按**系统 ANSI 码页**输出的（中文系统 = GBK），
    不是 UTF-8；直接按 UTF-8 解会得到乱码（踩过：回复里出现
    「取消失败：��Ϊû���κν����еĹػ����̣�…」）。所以先用 UTF-8 严格解码，
    失败即说明它就是 ANSI 码页，再用 mbcs 解。
    """

    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("mbcs", errors="replace")


def _run_pwsh(command: str, cwd: str, timeout_sec: float) -> Dict[str, Any]:
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command],
            cwd=cwd,
            capture_output=True,
            timeout=float(timeout_sec),
            creationflags=0x08000000,
        )
        out = _decode_console(proc.stdout or b"").strip()
        err = _decode_console(proc.stderr or b"").strip()
        if err:
            out = f"{out}\n{err}".strip()
        return {"rc": proc.returncode, "out": out}
    except subprocess.TimeoutExpired:
        return {"rc": -1, "out": f"执行超时（>{timeout_sec}s），已终止。"}
    except Exception as exc:
        return {"rc": -2, "out": f"无法执行：{exc}"}


def _ps_quote(text: str) -> str:
    """PowerShell 单引号包裹并转义。"""
    return "'" + str(text).replace("'", "''") + "'"


def _resolve_cwd(workdir: str, default_workdir: str) -> str:
    p = Path(workdir or default_workdir).expanduser()
    if p.is_dir():
        return str(p)
    return str(Path(default_workdir))


def classify(cmd: str) -> str:
    """返回 refuse / safe / confirm"""
    c = (cmd or "").strip()
    if not c:
        return "refuse"
    for pat in _BLACKLIST_RE:
        if pat.search(c):
            return "refuse"
    for tok in _FORCE_CONFIRM_TOKENS:
        if tok in c.lower():
            return "confirm"
    for pat in _SAFE_READONLY_RE:
        if pat.match(c):
            return "safe"
    return "confirm"


def execute_shell(cmd: str, workdir: str, timeout_sec: float, default_workdir: str) -> str:
    """直跑（供只读白名单与确认后使用），返回展示文本。"""
    cwd = _resolve_cwd(workdir, default_workdir)
    res = _run_pwsh(cmd, cwd, timeout_sec)
    tail = res["out"][:1500] or "(无输出)"
    _audit("shell", "exec", cmd, cwd, res["rc"], tail)
    return f"执行结果（退出码 {res['rc']}）：\n{tail}" if res["rc"] in (0,) else f"执行结束（退出码 {res['rc']}）：\n{tail}"


# ---------------- 待确认登记 ---------------- #

def _load_pending() -> List[Dict[str, Any]]:
    try:
        data = json.loads(PENDING_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_pending(items: List[Dict[str, Any]]) -> None:
    try:
        PENDING_PATH.write_text(json.dumps(items[-5:], ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def propose(kind: str, cmd: str, workdir: str, opts: Optional[Dict[str, Any]] = None, ttl_sec: int = 300) -> str:
    """登记一条待确认，返回提示文本（含编号）。同类待办单飞去重。"""
    with _lock:
        items = _load_pending()
        now = time.time()
        items = [x for x in items if now - float(x.get("created", 0)) < ttl_sec]
        # 单飞去重：关机类只留一条；shell 同命令去重
        items = [
            x for x in items
            if not (
                (x.get("kind") == "shutdown" and x.get("kind") == kind)
                or (x.get("kind") == kind and x.get("cmd") == cmd)
            )
        ]
        code = (max([int(x.get("code", 0)) for x in items] or [0]) + 1) % 10000
        items.append({"code": code, "kind": kind, "cmd": cmd, "workdir": workdir,
                      "opts": opts or {}, "created": now})
        _save_pending(items)
    _audit(kind, "propose", cmd, workdir, 0, "")
    return f"待确认 P{code}：`{cmd}` —— 回复：执行 P{code}"


def list_pending() -> List[Dict[str, Any]]:
    """列出当前全部待确认（不弹出）。

    「这条消息是不是对某条待确认的回执」由模型判断（见 marisa_gate 的
    marisa_pending_* 工具），这里只如实列出，不做任何关键词匹配。
    """

    with _lock:
        return list(_load_pending())


def pop_pending(code: Optional[int]) -> Optional[Dict[str, Any]]:
    """取出一条待确认（指定编号或最新一条）。"""
    with _lock:
        items = _load_pending()
        if code is not None:
            target = next((x for x in items if int(x.get("code", 0)) == code), None)
        else:
            target = items[-1] if items else None
        if target is not None:
            items.remove(target)
            _save_pending(items)
        return target


def cancel_pending(code: Optional[int]) -> bool:
    return pop_pending(code) is not None


# ---------------- 快捷工具入口 ---------------- #

def shell_proposal(cmd: str, workdir: str, default_workdir: str) -> str:
    kind = classify(cmd)
    if kind == "refuse":
        return "这个命令在我的禁止清单里だぜ……换个思路吧。"
    if kind == "safe":
        return execute_shell(cmd, workdir, 60, default_workdir)
    return propose("shell", cmd, _resolve_cwd(workdir, default_workdir))


def shutdown_action(seconds: int, cancel: bool, default_workdir: str, require_confirm: bool = True) -> str:
    if cancel:
        res = _run_pwsh("shutdown /a", default_workdir, 15)
        _audit("shutdown", "cancel", "shutdown /a", default_workdir, res["rc"], res["out"][:300])
        if res["rc"] == 0:
            return "已尝试取消关机/重启だぜ。"
        # 1116 = ERROR_NO_SHUTDOWN_IN_PROGRESS：本来就没排过关机，这不是"取消失败"
        if "1116" in res["out"]:
            return "本来就没有进行中的关机计划だぜ（不用取消）。"
        return f"取消失败：{res['out'][:200]}"
    sec = max(0, int(seconds or 30))
    if not require_confirm:
        # 直接执行（默认模式）：主人说"关机"就启动倒计时——不再走提案-确认，
        # 因为倒计时本身就是缓冲（期间说「取消关机」可中止，shutdown /a 兜底）。
        cmd = f"shutdown /s /t {sec}"
        res = _run_pwsh(cmd, default_workdir, 15)
        _audit("shutdown", "exec", cmd, default_workdir, res["rc"], res["out"][:200])
        if res["rc"] == 0:
            return f"已启动 {sec} 秒关机倒计时だぜ（要反悔就说「取消关机」）。"
        return f"关机失败：{res['out'][:200]}"
    return propose("shutdown", f"shutdown /s /t {sec}", default_workdir, {"seconds": sec})


def execute_pending(item: Dict[str, Any], timeout_sec: float) -> str:
    """执行一条已确认待办。"""
    kind = str(item.get("kind") or "shell")
    cmd = str(item.get("cmd") or "")
    cwd = str(item.get("workdir") or Path.cwd())
    if kind == "shutdown":
        res = _run_pwsh(cmd, cwd, 15)
        _audit("shutdown", "exec", cmd, cwd, res["rc"], res["out"][:200])
        return "已执行关机倒计时（可用「取消」中止）だぜ。" if res["rc"] == 0 else f"关机失败：{res['out'][:200]}"
    return execute_shell(cmd, cwd, timeout_sec, cwd)
