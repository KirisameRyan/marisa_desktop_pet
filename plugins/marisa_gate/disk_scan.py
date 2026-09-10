# -*- coding: utf-8 -*-
"""磁盘与项目只读扫描：为「电脑管家」提供确定性证据，不做任何修改。

设计原则（参照同类项目的成熟做法）：
- **只读**：本模块只统计与列清单，删除/移动一律由 cleanup_plan 在用户确认后执行；
- **保护优先**：系统目录、符号链接/junction、工程目录、.git 一律不进入候选；
- **限时限量**：大目录遍历带截止时间与条数上限，避免把命令链卡死；
- **结构化输出**：返回 dict 而不是文本，便于上层排版成卡片或清单。
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ───────────────────────── 保护规则 ─────────────────────────

# 这些目录名一律不进入候选（系统/版本控制/回收站本体）
PROTECTED_DIR_NAMES = {
    ".git",
    ".hg",
    ".svn",
    "$recycle.bin",
    "system volume information",
    "windows",
    "program files",
    "program files (x86)",
    "programdata",
    "recovery",
    "perflogs",
    "appdata",  # AppData 只允许精确命中缓存子目录，不做整树扫描
}

# 带这些标记的目录视为「工程目录」，扫描时跳过（同类项目的教训：不要自信清理工程目录）
PROJECT_MARKER_FILES = (".uproject", ".godot", "project.godot")
PROJECT_MARKER_DIRS = ("ProjectSettings",)

# 可再生产物：删掉能重新生成，属于「清理」的合法对象
REPRODUCIBLE_DIR_NAMES = (
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
    "target",
    ".next",
    ".nuxt",
    ".turbo",
    "coverage",
    "htmlcov",
    ".tox",
)

# 临时项候选：已知的缓存/临时目录（只报大小，不在本模块删除）
TEMP_CANDIDATES = (
    ("用户临时文件", "%TEMP%"),
    ("pip 缓存", "%LOCALAPPDATA%/pip/Cache"),
    ("npm 缓存", "%LOCALAPPDATA%/npm-cache"),
    ("npm 缓存(_cacache)", "%LOCALAPPDATA%/npm/_cacache"),
    ("Chrome 缓存", "%LOCALAPPDATA%/Google/Chrome/User Data/Default/Cache"),
    ("Edge 缓存", "%LOCALAPPDATA%/Microsoft/Edge/User Data/Default/Cache"),
)

DEFAULT_DEADLINE_SEC = 25.0
DEFAULT_MAX_ENTRIES = 400_000


def human_size(num_bytes: float) -> str:
    """把字节数转成人类可读文本（1024 进制）。"""

    value = float(max(0.0, num_bytes))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _expand(path_like: str) -> Optional[Path]:
    """展开 %VAR% 形式的路径；不存在则返回 None。"""

    expanded = os.path.expandvars(str(path_like))
    if "%" in expanded:  # 变量没展开（环境变量缺失）
        return None
    candidate = Path(expanded)
    return candidate if candidate.exists() else None


def is_protected(path: Path) -> bool:
    """判断路径是否命中保护规则（系统目录、.git、工程目录、符号链接）。"""

    try:
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            return True
    except OSError:
        return True

    parts = [part.lower() for part in path.parts]
    if any(name in parts for name in PROTECTED_DIR_NAMES):
        return True
    # 盘符根目录本身不允许作为操作对象
    if len(path.parts) <= 1:
        return True
    try:
        if any((path / name).exists() for name in PROJECT_MARKER_FILES):
            return True
        if any((path / name).is_dir() for name in PROJECT_MARKER_DIRS):
            return True
    except OSError:
        return True
    return False


def is_reproducible_dir(name: str) -> bool:
    """目录名是否属于可再生产物（依赖、缓存、构建输出）。"""

    return name.lower() in REPRODUCIBLE_DIR_NAMES


def _iter_dir(path: Path):
    """稳定遍历目录，出错时跳过而不是中断整个扫描。"""

    try:
        with os.scandir(path) as entries:
            for entry in entries:
                yield entry
    except (PermissionError, FileNotFoundError, OSError):
        return


# ───────────────────────── 容量与临时项 ─────────────────────────


class _SHQUERYRBINFO(ctypes.Structure):
    """SHQueryRecycleBin 的结果结构。"""

    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("i64Size", ctypes.c_longlong),
        ("i64NumItems", ctypes.c_longlong),
    ]


def _recycle_bin_info() -> Dict[str, Any]:
    """读取回收站占用（不清理，只报告——回收站里的文件仍占磁盘）。"""

    info = _SHQUERYRBINFO()
    info.cbSize = ctypes.sizeof(_SHQUERYRBINFO)
    try:
        hr = ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(info))
        if hr == 0:
            return {"size": int(info.i64Size), "items": int(info.i64NumItems)}
    except Exception:
        pass
    return {"size": 0, "items": 0}


def _dir_size(
    path: Path,
    deadline: float,
    counter: List[int],
    max_entries: int,
) -> Tuple[int, bool]:
    """递归统计目录大小，返回 (字节数, 是否完整扫完)。

    超时或超量时立即停止并标记为不完整——上层必须如实标注「至少 X」，
    不能把没扫完的数字当成准确的占用报给主人。
    """

    total = 0
    complete = True
    stack = [path]
    while stack:
        if time.monotonic() > deadline or counter[0] > max_entries:
            complete = False
            break
        current = stack.pop()
        for entry in _iter_dir(current):
            counter[0] += 1
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
    return total, complete


def scan_overview(deadline_sec: float = DEFAULT_DEADLINE_SEC) -> Dict[str, Any]:
    """体检总览：各盘容量、回收站、常见临时项占用。"""

    deadline = time.monotonic() + deadline_sec
    counter = [0]

    drives: List[Dict[str, Any]] = []
    for letter in "CDEFGHIJ":
        root = Path(f"{letter}:\\")
        if not root.exists():
            continue
        try:
            usage = shutil.disk_usage(str(root))
        except OSError:
            continue
        drives.append(
            {
                "path": str(root),
                "total": usage.total,
                "used": usage.used,
                "free": usage.free,
            }
        )

    temps: List[Dict[str, Any]] = []
    for label, raw in TEMP_CANDIDATES:
        target = _expand(raw)
        if target is None or not target.is_dir():
            continue
        size, complete = _dir_size(target, deadline, counter, DEFAULT_MAX_ENTRIES)
        if size > 0:
            temps.append({"label": label, "path": str(target), "size": size, "complete": complete})

    return {
        "drives": drives,
        "recycle_bin": _recycle_bin_info(),
        "temp_candidates": sorted(temps, key=lambda item: item["size"], reverse=True),
        "truncated": time.monotonic() > deadline or counter[0] > DEFAULT_MAX_ENTRIES,
        "scanned_entries": counter[0],
    }


# ───────────────────────── 大文件与重复文件 ─────────────────────────


def scan_large_files(
    roots: Iterable[Path],
    *,
    min_mb: int = 200,
    limit: int = 60,
    deadline_sec: float = DEFAULT_DEADLINE_SEC,
) -> Dict[str, Any]:
    """列出超过阈值的大文件（跳过保护目录）。"""

    deadline = time.monotonic() + deadline_sec
    min_bytes = max(1, min_mb) * 1024 * 1024
    found: List[Dict[str, Any]] = []
    counter = [0]

    for root in roots:
        if is_protected(root):
            continue
        stack = [root]
        while stack and time.monotonic() < deadline:
            current = stack.pop()
            for entry in _iter_dir(current):
                counter[0] += 1
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name.lower() in PROTECTED_DIR_NAMES:
                            continue
                        stack.append(Path(entry.path))
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    stat = entry.stat(follow_symlinks=False)
                    if stat.st_size >= min_bytes:
                        found.append(
                            {
                                "path": entry.path,
                                "size": stat.st_size,
                                "mtime": stat.st_mtime,
                            }
                        )
                except OSError:
                    continue

    found.sort(key=lambda item: item["size"], reverse=True)
    return {
        "files": found[:limit],
        "total_found": len(found),
        "truncated": time.monotonic() > deadline,
        "scanned_entries": counter[0],
    }


def _hash_file(path: Path, chunk: int = 1 << 20) -> str:
    """计算文件内容哈希（blake2b，够快且够稳）。"""

    digest = hashlib.blake2b(digest_size=16)
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def scan_duplicates(
    roots: Iterable[Path],
    *,
    min_mb: int = 10,
    limit: int = 40,
    deadline_sec: float = DEFAULT_DEADLINE_SEC,
) -> Dict[str, Any]:
    """按「先比大小、再比哈希」找重复文件。

    可再生产物目录（node_modules、构建输出等）整体跳过——它们天然包含
    大量同名同内容文件，报出来只是噪声，且删了也会被重新生成。
    """

    deadline = time.monotonic() + deadline_sec
    min_bytes = max(1, min_mb) * 1024 * 1024

    by_size: Dict[int, List[Path]] = {}
    counter = [0]
    for root in roots:
        if is_protected(root):
            continue
        stack = [root]
        while stack and time.monotonic() < deadline:
            current = stack.pop()
            for entry in _iter_dir(current):
                counter[0] += 1
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        name = entry.name.lower()
                        if name in PROTECTED_DIR_NAMES or is_reproducible_dir(name):
                            continue
                        stack.append(Path(entry.path))
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    size = entry.stat(follow_symlinks=False).st_size
                    if size >= min_bytes:
                        by_size.setdefault(size, []).append(Path(entry.path))
                except OSError:
                    continue

    groups: List[Dict[str, Any]] = []
    wasted = 0
    for size, paths in by_size.items():
        if len(paths) < 2 or time.monotonic() > deadline:
            continue
        by_hash: Dict[str, List[Path]] = {}
        for path in paths:
            try:
                by_hash.setdefault(_hash_file(path), []).append(path)
            except OSError:
                continue
        for digest, same in by_hash.items():
            if len(same) < 2:
                continue
            wasted += size * (len(same) - 1)
            groups.append(
                {
                    "size": size,
                    "count": len(same),
                    "paths": [str(item) for item in same],
                    "digest": digest,
                }
            )

    groups.sort(key=lambda item: item["size"] * (item["count"] - 1), reverse=True)
    return {
        "groups": groups[:limit],
        "total_groups": len(groups),
        "wasted_bytes": wasted,
        "truncated": time.monotonic() > deadline,
        "scanned_entries": counter[0],
    }


# ───────────────────────── 项目审查 ─────────────────────────


def _git(workdir: Path, args: List[str], timeout: float = 10.0) -> str:
    """执行只读 git 命令并返回 stdout（失败返回空串）。"""

    try:
        result = subprocess.run(
            ["git", "-C", str(workdir), *args],
            capture_output=True,
            text=True,
            errors="ignore",
            timeout=timeout,
        )
        if result.returncode != 0:
            return ""
        return result.stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def scan_projects(roots: Iterable[Path], *, deadline_sec: float = DEFAULT_DEADLINE_SEC) -> Dict[str, Any]:
    """审查代码仓库：占用、可再生产物体积、未提交改动、最近提交、分支数。"""

    deadline = time.monotonic() + deadline_sec
    counter = [0]
    projects: List[Dict[str, Any]] = []

    for root in roots:
        if not root.is_dir():
            continue
        # 根目录本身是仓库就先算它，否则向下找一层（常见的「工作区/项目名」结构）
        candidates: List[Path] = []
        if (root / ".git").exists():
            candidates.append(root)
        else:
            for entry in _iter_dir(root):
                if not entry.is_dir(follow_symlinks=False):
                    continue
                child = Path(entry.path)
                if child.name.lower() in PROTECTED_DIR_NAMES:
                    continue
                if (child / ".git").exists():
                    candidates.append(child)

        for repo in candidates:
            if time.monotonic() > deadline:
                break
            total, total_complete = _dir_size(repo, deadline, counter, DEFAULT_MAX_ENTRIES)
            reproducible: Dict[str, int] = {}
            stack = [repo]
            while stack and time.monotonic() < deadline:
                current = stack.pop()
                for entry in _iter_dir(current):
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name.lower() == ".git" or entry.name in PROTECTED_DIR_NAMES:
                                continue
                            if is_reproducible_dir(entry.name):
                                size, _complete = _dir_size(Path(entry.path), deadline, counter, DEFAULT_MAX_ENTRIES)
                                reproducible[entry.name] = reproducible.get(entry.name, 0) + size
                                continue
                            stack.append(Path(entry.path))
                    except OSError:
                        continue

            status = _git(repo, ["status", "--porcelain"])
            dirty = len([line for line in status.splitlines() if line.strip()])
            last_commit = _git(repo, ["log", "-1", "--format=%ct"]).strip()
            branch_count = len([line for line in _git(repo, ["branch"]).splitlines() if line.strip()])
            upstream_gap = _git(repo, ["rev-list", "--count", "@{u}..HEAD"]).strip()

            projects.append(
                {
                    "path": str(repo),
                    "name": repo.name,
                    "total_bytes": total,
                    "total_complete": total_complete,
                    "reproducible": reproducible,
                    "reproducible_bytes": sum(reproducible.values()),
                    "dirty_files": dirty,
                    "last_commit_ts": int(last_commit) if last_commit.isdigit() else 0,
                    "branch_count": branch_count,
                    "unpushed_commits": int(upstream_gap) if upstream_gap.isdigit() else 0,
                }
            )

    projects.sort(key=lambda item: item["total_bytes"], reverse=True)
    return {
        "projects": projects,
        "truncated": time.monotonic() > deadline,
        "scanned_entries": counter[0],
    }


def default_roots() -> List[Path]:
    """默认扫描根：桌面 / 下载 / 文档（白名单由配置提供，这里只兜底）。"""

    home = Path.home()
    candidates = [
        home / "Desktop",
        home / "Downloads",
        home / "Documents",
    ]
    return [path for path in candidates if path.is_dir()]


# ───────────────────────── 桌面散落文件 ─────────────────────────

# 这些是桌面上的功能性文件，不属于「散乱文件」，整理时一律跳过
DESKTOP_SKIP_SUFFIXES = {".lnk", ".url", ".ini"}

# 桌面整理分类规则（按扩展名归类；未命中归入「其他」）
DESKTOP_CATEGORY_RULES: List[Tuple[str, Tuple[str, ...]]] = [
    ("文档", (".doc", ".docx", ".pdf", ".txt", ".md", ".rtf", ".odt", ".xls", ".xlsx",
              ".ppt", ".pptx", ".csv", ".wps", ".et", ".dps")),
    ("图片", (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg", ".psd", ".ai",
              ".tif", ".tiff", ".heic")),
    ("压缩包", (".zip", ".rar", ".7z", ".tar", ".gz", ".xz", ".bz2")),
    ("安装包", (".exe", ".msi", ".msix")),
    ("音视频", (".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".mp4", ".mkv", ".avi",
                ".mov", ".wmv", ".flv", ".webm")),
]


def classify_desktop_file(name: str) -> str:
    """按扩展名把桌面文件归入整理类别。"""

    suffix = Path(name).suffix.lower()
    for category, suffixes in DESKTOP_CATEGORY_RULES:
        if suffix in suffixes:
            return category
    return "其他"


def scan_desktop_clutter(
    desktop: Path,
    *,
    min_age_hours: int = 24,
    limit: int = 200,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """扫描桌面根目录的散落文件（只列清单，不移动）。

    只处理超过 ``min_age_hours`` 没动过的普通文件：快捷方式、网址、目录与
    刚放上桌面的文件都跳过，避免把主人正在用的东西挪走。
    """

    result: Dict[str, Any] = {"root": str(desktop), "files": [], "total_found": 0, "truncated": False}
    if not desktop.is_dir():
        return result

    cutoff = float(now if now is not None else time.time()) - max(0, int(min_age_hours)) * 3600
    found: List[Dict[str, Any]] = []
    for entry in _iter_dir(desktop):
        try:
            if entry.is_symlink() or entry.is_dir(follow_symlinks=False):
                continue
            if not entry.is_file(follow_symlinks=False):
                continue
            if Path(entry.name).suffix.lower() in DESKTOP_SKIP_SUFFIXES:
                continue
            stat = entry.stat(follow_symlinks=False)
            if stat.st_mtime > cutoff:
                continue
            found.append(
                {
                    "path": entry.path,
                    "name": entry.name,
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                    "category": classify_desktop_file(entry.name),
                }
            )
        except OSError:
            continue

    found.sort(key=lambda item: item["mtime"])
    result["total_found"] = len(found)
    result["files"] = found[:limit]
    result["truncated"] = len(found) > limit
    return result
