"""电脑管家清理引擎（cleanup_plan）的离线测试。

引擎位于 ``plugins/marisa_gate``，该目录在主仓库中被 .gitignore 忽略，
因此未安装插件时整组测试自动跳过。测试全部使用临时目录与注入的假回收站
函数，不会触碰真实文件或系统回收站。
"""

import importlib.util
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, List, Tuple

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "marisa_gate"
MODULE_PATH = PLUGIN_DIR / "cleanup_plan.py"

pytestmark = pytest.mark.skipif(
    not MODULE_PATH.exists(),
    reason="未安装 marisa_gate 插件，跳过清理引擎测试",
)

if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))


def _load_module() -> ModuleType:
    """按文件路径加载清理引擎模块（与插件自身的加载方式保持一致）。"""

    module_name = "marisa_gate_cleanup_plan_under_test"
    if (cached := sys.modules.get(module_name)) is not None:
        return cached
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


cleanup = _load_module()


@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把计划/清单/审计路径重定向到临时目录，避免污染真实数据。"""

    monkeypatch.setattr(cleanup, "PLAN_PATH", tmp_path / "cleanup_plan.json")
    monkeypatch.setattr(cleanup, "MANIFEST_DIR", tmp_path / "manifests")
    monkeypatch.setattr(cleanup, "AUDIT_PATH", tmp_path / "audit.log")
    # pytest 的临时目录在 AppData 下，而生产环境的桌面不在；测试里去掉这条整树保护，
    # 保护规则的专项用例仍用 windows 目录名验证。
    monkeypatch.setattr(
        cleanup.DS,
        "PROTECTED_DIR_NAMES",
        cleanup.DS.PROTECTED_DIR_NAMES - {"appdata"},
    )
    return tmp_path


def _make_move_item(src: Path, dst: Path, item_id: int = 1) -> Dict[str, Any]:
    """构造一个移动条目（与 build_plan 的字段保持一致）。"""

    stat = src.stat()
    return {
        "id": item_id,
        "kind": "move",
        "category": "桌面整理",
        "label": src.name,
        "path": str(src),
        "size": stat.st_size,
        "mtime": stat.st_mtime,
        "dst": str(dst),
        "strict": True,
        "undoable": True,
        "reason": "test",
    }


def _make_plan(items: List[Dict[str, Any]], code: int = 1) -> Dict[str, Any]:
    """构造一个 pending 方案并计算指纹。"""

    plan: Dict[str, Any] = {
        "ok": True,
        "code": code,
        "created": time.time(),
        "expires_at": time.time() + 600,
        "ttl_sec": 600,
        "status": "pending",
        "req_id": f"test-{code}",
        "source": {},
        "items": items,
    }
    plan["fingerprint"] = cleanup.compute_fingerprint(plan)
    return plan


def test_build_plan_groups_categories_and_limits_items(tmp_path: Path) -> None:
    """候选会按临时缓存 / 桌面整理分组，超上限直接拒绝。"""

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    loose = desktop / "notes.pdf"
    loose.write_text("hello", encoding="utf-8")

    plan = cleanup.build_plan(
        temp_candidates=[{"label": "pip 缓存", "path": str(cache_dir), "size": 1024}],
        desktop_files=[
            {"path": str(loose), "name": loose.name, "size": loose.stat().st_size,
             "mtime": loose.stat().st_mtime, "category": "文档"}
        ],
        desktop_root=desktop,
        max_items=10,
    )

    assert plan["ok"] is True
    assert [item["category"] for item in plan["items"]] == ["临时缓存", "桌面整理"]
    assert plan["items"][1]["kind"] == "move"
    assert plan["fingerprint"]

    too_many = cleanup.build_plan(
        temp_candidates=[{"label": "pip 缓存", "path": str(cache_dir), "size": 1024}],
        desktop_files=[],
        desktop_root=None,
        max_items=0,
    )
    assert too_many["ok"] is False
    assert "上限" in too_many["error"]


def test_fingerprint_is_stable_and_sensitive() -> None:
    """指纹对相同方案稳定，对条目大小变化敏感。"""

    plan = _make_plan([
        {"id": 1, "kind": "clean_dir", "path": "C:/tmp/x", "size": 10, "mtime": 1.0, "dst": None},
    ])
    baseline = cleanup.compute_fingerprint(plan)
    assert cleanup.compute_fingerprint(plan) == baseline

    plan["items"][0]["size"] = 11
    assert cleanup.compute_fingerprint(plan) != baseline


def test_execute_move_and_undo_round_trip(tmp_path: Path, store: Path) -> None:
    """移动条目执行后可被 /撤销 精确还原。"""

    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    src = desktop / "a.txt"
    src.write_text("hello", encoding="utf-8")
    dst = desktop / "整理_20260910" / "文档" / "a.txt"

    plan = _make_plan([_make_move_item(src, dst)])
    cleanup.save_plan(plan)

    result = cleanup.execute_plan(plan, desktop_root=desktop)
    assert result["ok"] is True
    assert result["summary"]["moved_bytes"] == len("hello")
    assert dst.exists() and not src.exists()

    undo = cleanup.undo_plan()
    assert undo["ok"] is True
    assert undo["restored"] == 1
    assert src.read_text(encoding="utf-8") == "hello"
    assert not dst.exists()

    second = cleanup.undo_plan()
    assert second["ok"] is False
    assert "已经撤销过" in second["error"]


def test_status_write_skips_after_plan_replaced(store: Path) -> None:
    """旧方案后台执行结束时，不能覆盖主人刚生成的新方案。"""

    item = {"id": 1, "kind": "clean_dir", "path": "x", "size": 1, "mtime": 0.0}
    plan_a = cleanup.save_plan(_make_plan([dict(item)], code=1))
    plan_b = cleanup.save_plan(_make_plan([dict(item)], code=2))

    cleanup.mark_plan_status(plan_a, "done")

    stored = cleanup.load_plan()
    assert stored is not None
    assert stored["req_id"] == plan_b["req_id"]
    assert stored["status"] == "pending"


def test_execute_aborts_when_file_drifted(tmp_path: Path, store: Path) -> None:
    """执行前发现文件大小变化时应整体中止，不移动任何东西。"""

    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    src = desktop / "a.txt"
    src.write_text("hello", encoding="utf-8")
    dst = desktop / "整理_20260910" / "文档" / "a.txt"

    plan = _make_plan([_make_move_item(src, dst)])
    cleanup.save_plan(plan)

    src.write_text("hello world", encoding="utf-8")  # 计划生成后文件被改动
    result = cleanup.execute_plan(plan, desktop_root=desktop)

    assert result["ok"] is False
    assert result["problems"]
    assert src.exists() and not dst.exists()


def test_execute_refuses_permanent_without_config(tmp_path: Path, store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """彻底删除未开启时即使请求也不执行。"""

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / "junk.bin").write_bytes(b"x" * 16)
    monkeypatch.setattr(cleanup, "_allowed_temp_paths", lambda: [cache_dir])

    plan = _make_plan([
        {"id": 1, "kind": "clean_dir", "category": "临时缓存", "label": "缓存",
         "path": str(cache_dir), "size": 16, "mtime": cache_dir.stat().st_mtime,
         "strict": False, "undoable": True, "reason": "test"},
    ])
    cleanup.save_plan(plan)

    result = cleanup.execute_plan(plan, permanent=True, allow_permanent=False)
    assert result["ok"] is False
    assert "彻底删除未开启" in result["error"]
    assert cache_dir.exists()


def test_clean_dir_outside_allowed_paths_is_rejected(
    tmp_path: Path, store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不在已知缓存目录内的 clean_dir 条目必须被拒绝。"""

    allowed = tmp_path / "cache"
    allowed.mkdir()
    outsider = tmp_path / "random_dir"
    outsider.mkdir()
    (outsider / "data.bin").write_bytes(b"y" * 8)
    monkeypatch.setattr(cleanup, "_allowed_temp_paths", lambda: [allowed])

    plan = _make_plan([
        {"id": 1, "kind": "clean_dir", "category": "临时缓存", "label": "伪装缓存",
         "path": str(outsider), "size": 8, "mtime": outsider.stat().st_mtime,
         "strict": False, "undoable": True, "reason": "test"},
    ])
    cleanup.save_plan(plan)

    result = cleanup.execute_plan(plan, trash_fn=lambda paths: (True, ""))
    assert result["ok"] is False
    assert "不在允许清理的缓存目录内" in "\n".join(result["problems"])
    assert outsider.exists()


def test_protected_path_is_rejected(tmp_path: Path, store: Path) -> None:
    """命中保护规则的条目（如 windows 目录名）必须被拒绝。"""

    protected = tmp_path / "windows" / "system32"
    protected.mkdir(parents=True)
    target = protected / "x.dll"
    target.write_text("dll", encoding="utf-8")

    plan = _make_plan([_make_move_item(target, tmp_path / "Desktop" / "x.dll")])
    cleanup.save_plan(plan)

    result = cleanup.execute_plan(plan, desktop_root=tmp_path / "Desktop")
    assert result["ok"] is False
    assert "保护规则" in "\n".join(result["problems"])
    assert target.exists()


@pytest.mark.parametrize(
    ("text", "expected_action", "expected_selected", "expected_permanent"),
    [
        ("执行 P3", "exec", None, False),
        ("确认执行 P3", "exec", None, False),
        ("执行P3 1,3", "exec", [1, 3], False),
        ("执行 P3 1 3 彻底", "exec", [1, 3], True),
        ("取消 P3", "cancel", None, False),
    ],
)
def test_parse_confirm_accepts_explicit_code(
    text: str,
    expected_action: str,
    expected_selected: Any,
    expected_permanent: bool,
) -> None:
    """带方案编号的显式口令才能命中清理确认。"""

    plan = _make_plan([
        {"id": 1, "kind": "clean_dir", "path": "x", "size": 1, "mtime": 0.0},
        {"id": 2, "kind": "move", "path": "y", "size": 1, "mtime": 0.0, "dst": "z"},
        {"id": 3, "kind": "move", "path": "w", "size": 1, "mtime": 0.0, "dst": "v"},
    ], code=3)

    parsed = cleanup.parse_confirm(text, plan)
    assert parsed is not None
    assert parsed["action"] == expected_action
    assert parsed["selected"] == expected_selected
    assert parsed["permanent"] == expected_permanent


@pytest.mark.parametrize("text", ["执行", "取消", "执行 P4", "执行 P33 1,2"])
def test_parse_confirm_ignores_bare_or_mismatched_code(text: str) -> None:
    """裸口令与不匹配的编号不能命中清理方案（留给终端待办）。"""

    plan = _make_plan([{"id": 1, "kind": "clean_dir", "path": "x", "size": 1, "mtime": 0.0}], code=3)
    assert cleanup.parse_confirm(text, plan) is None


def test_undo_skips_when_original_exists(tmp_path: Path, store: Path) -> None:
    """撤销时原位置已被新文件占用则跳过，绝不覆盖。"""

    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    src = desktop / "a.txt"
    src.write_text("hello", encoding="utf-8")
    dst = desktop / "整理_20260910" / "文档" / "a.txt"

    plan = _make_plan([_make_move_item(src, dst)])
    cleanup.save_plan(plan)
    cleanup.execute_plan(plan, desktop_root=desktop)

    src.write_text("new file", encoding="utf-8")  # 原位置被新文件占用
    undo = cleanup.undo_plan()

    assert undo["ok"] is True
    assert undo["restored"] == 0
    assert undo["skipped"] == 1
    assert src.read_text(encoding="utf-8") == "new file"
    assert dst.exists()


def test_trash_and_restore_hooks_are_used(tmp_path: Path, store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """clean_dir 执行走注入的回收站函数，撤销走注入的还原函数。"""

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / "junk.bin").write_bytes(b"z" * 32)
    monkeypatch.setattr(cleanup, "_allowed_temp_paths", lambda: [cache_dir])

    trashed: List[Tuple[str, ...]] = []

    def fake_trash(paths):
        trashed.append(tuple(paths))
        for path in paths:
            target = Path(path)
            if target.is_dir():
                for child in target.iterdir():
                    child.unlink()
                target.rmdir()
        return True, ""

    plan = _make_plan([
        {"id": 1, "kind": "clean_dir", "category": "临时缓存", "label": "缓存",
         "path": str(cache_dir), "size": 32, "mtime": cache_dir.stat().st_mtime,
         "strict": False, "undoable": True, "reason": "test"},
    ])
    cleanup.save_plan(plan)

    result = cleanup.execute_plan(plan, trash_fn=fake_trash)
    assert result["ok"] is True
    assert result["summary"]["trashed_bytes"] == 32
    assert trashed == [(str(cache_dir),)]
    assert not cache_dir.exists()

    restore_calls: List[Tuple[str, ...]] = []

    def fake_restore(paths):
        restore_calls.append(tuple(paths))
        return {str(cache_dir): "restored"}

    undo = cleanup.undo_plan(restore_fn=fake_restore)
    assert undo["ok"] is True
    assert undo["restored"] == 1
    assert restore_calls == [(str(cache_dir),)]
