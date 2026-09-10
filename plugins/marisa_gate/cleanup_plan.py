# -*- coding: utf-8 -*-
"""电脑管家清理引擎：候选计划 → 指纹 → 确认执行 → manifest → 撤销。

设计原则（与 disk_scan 的只读原则衔接）：
- **确定性候选**：只有临时缓存目录与桌面散落文件会进入方案，模型不决定删什么；
- **计划与执行分离**：计划只落盘；执行前逐项校验指纹与文件状态，任何漂移都中止
  （fail closed），不降级硬执行；
- **可恢复优先**：用户文件只移动或送系统回收站；彻底删除只对缓存目录开放，
  且必须显式勾选 + 配置允许；
- **全程留痕**：每次计划、执行、撤销都写 manifest 与审计日志，撤销按 manifest 逆向。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import ctypes
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import threading
import time
import uuid

import disk_scan as DS

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
MARISA_DATA_DIR = PROJECT_ROOT / "data" / "marisa"
PLAN_PATH = MARISA_DATA_DIR / "cleanup_plan.json"
MANIFEST_DIR = MARISA_DATA_DIR / "cleanup_manifests"
AUDIT_PATH = MARISA_DATA_DIR / "cleanup_audit.log"

DEFAULT_TTL_SEC = 600
MAX_CODE = 9999

_lock = threading.RLock()

# ───────────────────────── 计划结构 ─────────────────────────


def _path_mtime(path: Path) -> float:
    """读取文件/目录的 mtime；读不到返回 0。"""

    try:
        return float(path.stat().st_mtime)
    except OSError:
        return 0.0


def _unique_target(dst: Path) -> Path:
    """目标已存在时追加 (2)/(3)… 直到不冲突。"""

    if not dst.exists():
        return dst
    stem, suffix = dst.stem, dst.suffix
    for index in range(2, 100):
        candidate = dst.with_name(f"{stem} ({index}){suffix}")
        if not candidate.exists():
            return candidate
    return dst.with_name(f"{stem} ({uuid.uuid4().hex[:6]}){suffix}")


def build_plan(
    *,
    temp_candidates: List[Dict[str, Any]],
    desktop_files: List[Dict[str, Any]],
    desktop_root: Optional[Path] = None,
    organize_date: Optional[str] = None,
    max_items: int = 200,
    ttl_sec: int = DEFAULT_TTL_SEC,
    source: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """由扫描结果生成一份待确认的清理方案。

    Args:
        temp_candidates: ``disk_scan.scan_overview()["temp_candidates"]``。
        desktop_files: ``disk_scan.scan_desktop_clutter()["files"]``。
        desktop_root: 桌面根目录；提供时桌面文件才会转成整理项。
        organize_date: 整理目标文件夹的日期串（默认今天）。
        max_items: 方案条目上限，超了直接报错要求缩小范围。
        ttl_sec: 方案有效期（秒）。
        source: 方案来源 ``{platform, stream_id}``，用于执行后回执。

    Returns:
        Dict[str, Any]: 方案字典；超限时返回 ``{"ok": False, "error": ...}``。
    """

    items: List[Dict[str, Any]] = []

    # 1) 系统临时/缓存：整目录送回收站（可重新生成）
    for candidate in temp_candidates:
        raw_path = str(candidate.get("path") or "").strip()
        if not raw_path:
            continue
        path = Path(raw_path)
        items.append(
            {
                "id": len(items) + 1,
                "kind": "clean_dir",
                "category": "临时缓存",
                "label": str(candidate.get("label") or path.name),
                "path": str(path),
                "size": int(candidate.get("size") or 0),
                "mtime": _path_mtime(path),
                "strict": False,
                "undoable": True,
                "reason": "缓存可重新生成；送回收站不释放空间，彻底删除才真正腾地方",
            }
        )

    # 2) 桌面散落文件：按类别移动到 桌面/整理_日期/，只移动不删除
    if desktop_root is not None:
        day = organize_date or _dt.date.today().strftime("%Y%m%d")
        for entry in desktop_files:
            raw_path = str(entry.get("path") or "").strip()
            if not raw_path:
                continue
            src = Path(raw_path)
            category = str(entry.get("category") or "其他")
            dst = _unique_target(desktop_root / f"整理_{day}" / category / src.name)
            items.append(
                {
                    "id": len(items) + 1,
                    "kind": "move",
                    "category": "桌面整理",
                    "label": src.name,
                    "path": str(src),
                    "size": int(entry.get("size") or 0),
                    "mtime": float(entry.get("mtime") or 0.0),
                    "dst": str(dst),
                    "strict": True,
                    "undoable": True,
                    "reason": f"按类别归入 桌面/整理_{day}/{category}/",
                }
            )

    if not items:
        return {"ok": False, "error": "没有找到可清理的候选（临时缓存为空，桌面也没有需要整理的散落文件）"}
    if len(items) > max_items:
        return {
            "ok": False,
            "error": f"候选共 {len(items)} 项，超过单次上限 {max_items} 项；请先缩小清理范围",
        }

    now = time.time()
    plan: Dict[str, Any] = {
        "ok": True,
        "code": 0,
        "created": now,
        "expires_at": now + max(60, int(ttl_sec)),
        "ttl_sec": max(60, int(ttl_sec)),
        "fingerprint": "",
        "items": items,
        "status": "pending",
        "req_id": "",
        "source": dict(source or {}),
    }
    plan["fingerprint"] = compute_fingerprint(plan)
    return plan


def compute_fingerprint(plan: Dict[str, Any]) -> str:
    """对方案的确定性字段计算指纹（路径/大小/mtime/动作/目标）。"""

    payload = {
        "created": plan.get("created"),
        "items": [
            {key: item.get(key) for key in ("id", "kind", "path", "size", "mtime", "dst")}
            for item in plan.get("items", [])
        ],
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ───────────────────────── 计划持久化 ─────────────────────────


def _load_store() -> Dict[str, Any]:
    try:
        data = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {"last_code": 0, "plan": None}


def _save_store(store: Dict[str, Any]) -> None:
    MARISA_DATA_DIR.mkdir(parents=True, exist_ok=True)
    PLAN_PATH.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")


def save_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    """分配方案编号并持久化（单飞：替换旧方案）。"""

    with _lock:
        store = _load_store()
        code = int(store.get("last_code") or 0) + 1
        if code > MAX_CODE:
            code = 1
        plan["code"] = code
        plan["req_id"] = f"cleanup-{code}-{uuid.uuid4().hex[:8]}"
        store["last_code"] = code
        store["plan"] = plan
        _save_store(store)
    _audit("plan_create", code=plan.get("code"), items=len(plan.get("items", [])))
    return plan


def load_plan() -> Optional[Dict[str, Any]]:
    """读取当前方案（含已结束的）。"""

    plan = _load_store().get("plan")
    return plan if isinstance(plan, dict) else None


def get_pending_plan() -> Optional[Dict[str, Any]]:
    """返回仍在有效期内的待确认方案；过期/已结束返回 None。"""

    plan = load_plan()
    if not plan or plan.get("status") != "pending":
        return None
    if time.time() > float(plan.get("expires_at") or 0):
        return None
    return plan


def mark_plan_status(plan: Dict[str, Any], status: str, **extra: Any) -> Dict[str, Any]:
    """更新并落盘方案状态。

    如果方案已被新方案替换（req_id 不一致），旧状态不再回写，避免后台执行
    结束时把主人刚生成的新方案覆盖掉。
    """

    with _lock:
        store = _load_store()
        current = store.get("plan") if isinstance(store.get("plan"), dict) else None
        if current is not None and str(current.get("req_id") or "") != str(plan.get("req_id") or ""):
            return dict(plan)
        updated = dict(plan)
        updated["status"] = status
        updated.update(extra)
        store["plan"] = updated
        _save_store(store)
    return updated


# ───────────────────────── 确认解析 ─────────────────────────

_CONFIRM_WORDS = ("执行", "确认执行", "好的执行", "就执行", "执行吧", "确认")
_CANCEL_WORDS = ("取消", "算了", "别执行", "不执行")
_CODE_RE = re.compile(r"[pP]\s*(\d{1,4})")
_ITEM_RE = re.compile(r"(?<![\d.])(\d{1,3})(?![\d.])")


def _extract_code(text: str) -> Optional[int]:
    match = _CODE_RE.search(text or "")
    return int(match.group(1)) if match else None


def parse_confirm(text: str, plan: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """解析主人对清理方案的确认/取消。

    只认带方案编号的显式指令（如 ``执行 P3`` / ``执行 P3 1,3 彻底`` / ``取消 P3``），
    裸「执行」「取消」继续归终端待办处理，避免两条确认链互相抢话。
    """

    if not plan or plan.get("status") != "pending":
        return None
    raw = (text or "").strip()
    if not raw:
        return None
    code = _extract_code(raw)
    if code is None or code != int(plan.get("code") or 0):
        return None

    if any(word in raw for word in _CANCEL_WORDS):
        return {"action": "cancel", "selected": None, "permanent": False}

    if not any(word in raw for word in _CONFIRM_WORDS):
        return None

    body = _CODE_RE.sub(" ", raw, count=1)
    permanent = ("彻底" in body) or ("永久" in body)
    valid_ids = {int(item.get("id") or 0) for item in plan.get("items", [])}
    picked = sorted({int(match.group(1)) for match in _ITEM_RE.finditer(body)} & valid_ids)
    return {"action": "exec", "selected": picked or None, "permanent": permanent}


# ───────────────────────── 安全校验 ─────────────────────────


def _norm(path: Path) -> str:
    """规范化路径用于包含判断（解析软链接、统一大小写）。"""

    return os.path.normcase(str(path.resolve()))


def is_within(path: Path, root: Path) -> bool:
    """path 是否等于 root 或位于 root 之下。"""

    try:
        normalized_path = _norm(path)
        normalized_root = _norm(root)
    except OSError:
        return False
    return normalized_path == normalized_root or normalized_path.startswith(normalized_root + os.sep)


def _allowed_temp_paths() -> List[Path]:
    """当前配置下允许清理的已知缓存目录（每次执行时重新展开环境变量）。"""

    allowed: List[Path] = []
    for _label, raw in DS.TEMP_CANDIDATES:
        target = DS._expand(raw)
        if target is not None:
            allowed.append(target)
    return allowed


def _preflight_item(item: Dict[str, Any], desktop_root: Optional[Path]) -> Optional[str]:
    """执行前校验单个条目；返回错误文本，None 表示通过。"""

    raw_path = str(item.get("path") or "").strip()
    if not raw_path:
        return "条目缺少路径"
    path = Path(raw_path)
    if not path.exists():
        return f"源已不存在：{path}"
    if path.is_symlink():
        return f"符号链接不处理：{path}"

    kind = str(item.get("kind") or "")
    if kind == "clean_dir":
        # 缓存目录本身就在 AppData 下（%TEMP%、pip/npm 缓存），不能套用整树保护规则；
        # 这里改为「必须命中已知缓存目录白名单」这一条硬约束。
        if not path.is_dir():
            return f"不是目录：{path}"
        allowed = _allowed_temp_paths()
        if not any(is_within(path, root) or _norm(path) == _norm(root) for root in allowed):
            return f"不在允许清理的缓存目录内：{path}"
        return None

    if kind == "move":
        if DS.is_protected(path):
            return f"命中保护规则：{path}"
        if desktop_root is None:
            return "未配置桌面目录"
        if not path.is_file():
            return f"不是文件：{path}"
        if _norm(path.parent) != _norm(desktop_root):
            return f"只整理桌面根目录下的散落文件：{path}"
        dst = Path(str(item.get("dst") or ""))
        if not str(dst):
            return "整理项缺少目标路径"
        if not is_within(dst, desktop_root):
            return f"整理目标不在桌面内：{dst}"
        if dst.exists():
            return f"目标已存在，不覆盖：{dst}"
        try:
            stat_result = path.stat()
        except OSError as exc:
            return f"读取文件状态失败：{exc}"
        if stat_result.st_size != int(item.get("size") or 0):
            return f"文件大小已变化：{path}"
        if abs(stat_result.st_mtime - float(item.get("mtime") or 0.0)) > 0.001:
            return f"文件修改时间已变化：{path}"
        return None

    return f"未知动作类型：{kind}"


# ───────────────────────── 回收站与删除 ─────────────────────────


class _SHFILEOPSTRUCTW(ctypes.Structure):
    """SHFileOperationW 的参数结构。"""

    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("wFunc", ctypes.c_uint),
        ("pFrom", ctypes.c_wchar_p),
        ("pTo", ctypes.c_wchar_p),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", ctypes.c_int),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", ctypes.c_wchar_p),
    ]


def trash_files(paths: Iterable[str]) -> Tuple[bool, str]:
    """把文件/目录送进系统回收站（可恢复）。返回 (是否成功, 说明)。"""

    path_list = [str(item) for item in paths if str(item)]
    if not path_list:
        return True, ""
    try:
        FO_DELETE = 3
        FOF_ALLOWUNDO = 0x40
        FOF_NOCONFIRMATION = 0x10
        FOF_SILENT = 0x4
        FOF_NOERRORUI = 0x400

        buffer = "\0".join(os.path.abspath(item) for item in path_list) + "\0\0"
        operation = _SHFILEOPSTRUCTW()
        operation.wFunc = FO_DELETE
        operation.pFrom = buffer
        operation.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI

        shell32 = ctypes.windll.shell32
        shell32.SHFileOperationW.argtypes = [ctypes.POINTER(_SHFILEOPSTRUCTW)]
        shell32.SHFileOperationW.restype = ctypes.c_int
        result = shell32.SHFileOperationW(ctypes.byref(operation))
        if result == 0 and not operation.fAnyOperationsAborted:
            return True, ""
        return False, f"SHFileOperationW 返回 {result}（部分文件可能被占用未删除）"
    except Exception as exc:
        return False, f"送回收站失败：{str(exc)[:200]}"


def permanent_delete(path: Path) -> Tuple[bool, str]:
    """彻底删除（不可恢复）；只允许作用在缓存目录上。"""

    errors: List[str] = []

    def _retry_after_clear(func: Any, target: str, _exc_info: Any) -> None:
        """遇到只读属性时清位重试。"""

        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except Exception as exc:
            errors.append(f"{target}: {str(exc)[:120]}")

    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, onexc=_retry_after_clear)
        else:
            try:
                os.remove(path)
            except PermissionError:
                os.chmod(path, stat.S_IWRITE)
                os.remove(path)
    except Exception as exc:
        return False, str(exc)[:200]
    if errors:
        return False, "；".join(errors[:3])
    return True, ""


def _ps_quote(text: str) -> str:
    """PowerShell 单引号包裹并转义。"""

    return "'" + str(text).replace("'", "''") + "'"


_RESTORE_SCRIPT = """
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'Stop'
$targets = @({paths})
$shell = New-Object -ComObject Shell.Application
$bin = $shell.Namespace(0xA)
$result = @{{}}
foreach ($t in $targets) {{ $result[$t] = 'not_found' }}
foreach ($item in $bin.Items()) {{
  $from = [string]$item.ExtendedProperty('System.Recycle.DeletedFrom')
  if (-not $from) {{ continue }}
  $full = ($from.TrimEnd('\\') + '\\' + $item.Name)
  foreach ($t in $targets) {{
    if ($result[$t] -ne 'not_found') {{ continue }}
    if ($full -ieq $t) {{
      $restoreVerb = $null
      foreach ($verb in $item.Verbs()) {{
        $name = $verb.Name -replace '&',''
        if ($name -match '还原|復元|Restore') {{ $restoreVerb = $verb; break }}
      }}
      if ($null -eq $restoreVerb) {{ $result[$t] = 'no_verb'; continue }}
      $restoreVerb.DoIt()
      Start-Sleep -Milliseconds 200
      $result[$t] = 'restored'
    }}
  }}
}}
$result | ConvertTo-Json -Compress
"""


def restore_from_recycle_bin(paths: Iterable[str]) -> Dict[str, str]:
    """尽力从系统回收站还原指定原始路径。

    Returns:
        Dict[str, str]: 原始路径 -> restored / not_found / no_verb / error。
    """

    path_list = [str(item) for item in paths if str(item)]
    if not path_list:
        return {}
    script = _RESTORE_SCRIPT.format(paths=", ".join(_ps_quote(item) for item in path_list))
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            creationflags=0x08000000,
        )
        payload = (result.stdout or "").strip()
        if not payload:
            return {item: "error" for item in path_list}
        data = json.loads(payload)
        if isinstance(data, dict):
            return {item: str(data.get(item, "not_found")) for item in path_list}
    except Exception as exc:
        return {item: f"error: {str(exc)[:80]}" for item in path_list}
    return {item: "error" for item in path_list}


# ───────────────────────── 执行 ─────────────────────────


def _write_manifest(plan: Dict[str, Any], entries: List[Dict[str, Any]], summary: Dict[str, Any]) -> Path:
    """把本次执行写成 manifest，供 /撤销 逆向。"""

    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    path = MANIFEST_DIR / f"plan{int(plan.get('code') or 0)}-{int(time.time())}.json"
    data = {
        "plan_code": int(plan.get("code") or 0),
        "created": time.time(),
        "fingerprint": plan.get("fingerprint", ""),
        "items": entries,
        "summary": summary,
        "undo": None,
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def execute_plan(
    plan: Dict[str, Any],
    *,
    selected: Optional[Iterable[int]] = None,
    permanent: bool = False,
    allow_permanent: bool = False,
    desktop_root: Optional[Path] = None,
    trash_fn: Optional[Callable[[Iterable[str]], Tuple[bool, str]]] = None,
) -> Dict[str, Any]:
    """逐项校验并执行清理方案；任何预检失败都整体中止（fail closed）。"""

    if plan.get("status") != "pending":
        return {"ok": False, "error": "该方案已结束，请重新生成"}
    if time.time() > float(plan.get("expires_at") or 0):
        mark_plan_status(plan, "expired")
        return {"ok": False, "error": "方案已过期，请重新生成"}
    if compute_fingerprint(plan) != plan.get("fingerprint"):
        mark_plan_status(plan, "aborted", aborted_at=time.time())
        return {"ok": False, "error": "方案指纹校验失败，已拒绝执行"}

    all_items = list(plan.get("items") or [])
    if selected is None:
        chosen = all_items
    else:
        wanted = {int(item) for item in selected}
        chosen = [item for item in all_items if int(item.get("id") or 0) in wanted]
        if not chosen:
            return {"ok": False, "error": "选中的条目编号都不存在"}

    if permanent and not allow_permanent:
        return {"ok": False, "error": "彻底删除未开启（WebUI 插件配置里打开 allow_permanent_delete 后才能用）"}

    # 标记执行中：期间新方案可以照常生成，但旧方案的状态不再回写覆盖新方案
    mark_plan_status(plan, "executing", started_at=time.time())

    # 预检：全过才动手，任何一项漂移都要求重新生成方案
    problems: List[str] = []
    for item in chosen:
        problem = _preflight_item(item, desktop_root)
        if problem:
            problems.append(f"#{item.get('id')} {item.get('label')}：{problem}")
    if problems:
        mark_plan_status(plan, "aborted", aborted_at=time.time(), problems=problems[:10])
        _audit("execute_aborted", code=plan.get("code"), problems=problems[:10])
        return {
            "ok": False,
            "error": "执行前校验发现文件状态有变化，已整体中止；请重新生成方案",
            "problems": problems,
        }

    trash = trash_fn or trash_files
    entries: List[Dict[str, Any]] = []
    trashed_bytes = 0
    permanent_bytes = 0
    moved_bytes = 0
    failed = 0

    for item in chosen:
        entry: Dict[str, Any] = {
            "id": int(item.get("id") or 0),
            "kind": item.get("kind"),
            "label": item.get("label"),
            "src": item.get("path"),
            "dst": item.get("dst"),
            "size": int(item.get("size") or 0),
            "at": time.time(),
        }
        path = Path(str(item.get("path")))
        if item.get("kind") == "move":
            entry["action"] = "move"
            try:
                Path(str(item.get("dst"))).parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(item.get("dst")))
                entry["status"] = "done"
                moved_bytes += int(item.get("size") or 0)
            except Exception as exc:
                entry["status"] = "failed"
                entry["error"] = str(exc)[:200]
                failed += 1
        elif item.get("kind") == "clean_dir" and permanent:
            entry["action"] = "permanent"
            ok, detail = permanent_delete(path)
            entry["status"] = "done" if ok else "failed"
            if ok:
                permanent_bytes += int(item.get("size") or 0)
            else:
                entry["error"] = detail
                failed += 1
        else:
            entry["action"] = "trash"
            ok, detail = trash([str(path)])
            entry["status"] = "done" if ok else "failed"
            if ok:
                trashed_bytes += int(item.get("size") or 0)
            else:
                entry["error"] = detail
                failed += 1
        entries.append(entry)

    summary = {
        "total": len(chosen),
        "done": len(chosen) - failed,
        "failed": failed,
        "trashed_bytes": trashed_bytes,
        "permanent_bytes": permanent_bytes,
        "moved_bytes": moved_bytes,
        "permanent": bool(permanent),
    }
    manifest_path = _write_manifest(plan, entries, summary)
    mark_plan_status(plan, "partial" if failed else "done", executed_at=time.time(), manifest=str(manifest_path))
    _audit("execute_done", code=plan.get("code"), summary=summary, manifest=str(manifest_path))
    return {"ok": True, "plan_code": plan.get("code"), "summary": summary, "items": entries, "manifest": str(manifest_path)}


# ───────────────────────── 撤销 ─────────────────────────


def latest_manifest() -> Optional[Path]:
    """返回最近一次执行产生的 manifest。"""

    if not MANIFEST_DIR.is_dir():
        return None
    candidates = sorted(MANIFEST_DIR.glob("plan*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def undo_plan(
    manifest_path: Optional[Path] = None,
    *,
    restore_fn: Optional[Callable[[Iterable[str]], Dict[str, str]]] = None,
) -> Dict[str, Any]:
    """按 manifest 逆向：移动项回迁，回收站项尽力还原。"""

    path = Path(manifest_path) if manifest_path else latest_manifest()
    if path is None or not path.exists():
        return {"ok": False, "error": "没有找到可撤销的执行记录"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "error": f"读取撤销记录失败：{exc}"}
    if not isinstance(data, dict):
        return {"ok": False, "error": "撤销记录格式异常"}
    if data.get("undo"):
        return {"ok": False, "error": "这次执行已经撤销过了"}

    restore = restore_fn or restore_from_recycle_bin
    results: List[Dict[str, Any]] = []
    restored = 0
    skipped = 0

    # 先把所有需要从回收站还原的路径收集起来，一次 PowerShell 批量处理
    trash_paths = [
        str(entry.get("src"))
        for entry in data.get("items", [])
        if entry.get("status") == "done" and entry.get("action") == "trash"
    ]
    restore_map: Dict[str, str] = {}
    if trash_paths:
        restore_map = restore(trash_paths)

    for entry in data.get("items", []):
        record: Dict[str, Any] = {"id": entry.get("id"), "label": entry.get("label"), "action": entry.get("action")}
        status = entry.get("status")
        src = Path(str(entry.get("src") or ""))
        if status != "done":
            record["status"] = "skipped"
            record["error"] = "该项当时未执行成功"
            skipped += 1
        elif entry.get("action") == "move":
            dst = Path(str(entry.get("dst") or ""))
            if src.exists():
                record["status"] = "skipped"
                record["error"] = "原位置已有同名文件，未覆盖"
                skipped += 1
            elif not dst.exists():
                record["status"] = "skipped"
                record["error"] = "找不到被移动的文件"
                skipped += 1
            else:
                try:
                    shutil.move(str(dst), str(src))
                    record["status"] = "restored"
                    restored += 1
                except Exception as exc:
                    record["status"] = "failed"
                    record["error"] = str(exc)[:200]
        elif entry.get("action") == "permanent":
            record["status"] = "skipped"
            record["error"] = "彻底删除不可恢复"
            skipped += 1
        else:
            if src.exists():
                record["status"] = "skipped"
                record["error"] = "原位置已有同名文件，未覆盖"
                skipped += 1
            else:
                outcome = str(restore_map.get(str(entry.get("src")), "not_found"))
                if outcome == "restored":
                    record["status"] = "restored"
                    restored += 1
                else:
                    record["status"] = "failed"
                    record["error"] = f"回收站还原失败（{outcome}）"
        results.append(record)

    data["undo"] = {"at": time.time(), "restored": restored, "skipped": skipped, "items": results}
    result: Dict[str, Any] = {
        "ok": True,
        "manifest": str(path),
        "restored": restored,
        "skipped": skipped,
        "items": results,
    }
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        result["warning"] = f"撤销记录写回失败：{str(exc)[:120]}"
    _audit("undo_done", manifest=str(path), restored=restored, skipped=skipped)
    return result


# ───────────────────────── 审计 ─────────────────────────


def _audit(event: str, **fields: Any) -> None:
    """追加一条审计记录（失败不影响主流程）。"""

    try:
        MARISA_DATA_DIR.mkdir(parents=True, exist_ok=True)
        record = {"ts": _dt.datetime.now().isoformat(timespec="seconds"), "event": event, **fields}
        with _lock:
            with open(AUDIT_PATH, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass
