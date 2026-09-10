"""魔理沙桌面巡检的上下文判定测试。

巡检模块位于 ``plugins/marisa_desktop``，该目录在主仓库中被 .gitignore 忽略，
因此未安装插件时整组测试自动跳过。
"""

import importlib.util
import sys
import time
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, List

import pytest

PLUGIN_MODULE_PATH = Path(__file__).resolve().parents[2] / "plugins" / "marisa_desktop" / "patrol.py"

pytestmark = pytest.mark.skipif(
    not PLUGIN_MODULE_PATH.exists(),
    reason="未安装 marisa_desktop 插件，跳过巡检测试",
)


def _load_patrol_module() -> ModuleType:
    """按文件路径加载巡检模块（与插件自身的加载方式保持一致）。"""

    module_name = "marisa_desktop_patrol_under_test"
    if (cached := sys.modules.get(module_name)) is not None:
        return cached
    spec = importlib.util.spec_from_file_location(module_name, PLUGIN_MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


patrol = _load_patrol_module()


class _LoggerStub:
    """收集日志调用，便于断言。"""

    def __init__(self) -> None:
        self.messages: List[str] = []

    def info(self, message: str, *args: Any) -> None:
        self.messages.append(message % args if args else message)


def _build_director(reply: str = "喝口水吧だぜ！") -> Any:
    """构造一个使用固定回复的巡检导演。"""

    async def fake_llm(*, model: str, prompt: Any) -> str:
        del model, prompt
        return reply

    return patrol.PatrolDirector(llm_call=fake_llm, logger=_LoggerStub())


@pytest.mark.parametrize(
    ("hour", "expected"),
    [
        (23, "late_night"),
        (3, "late_night"),
        (8, "early_morning"),
        (10, "forenoon"),
        (13, "noon"),
        (16, "afternoon"),
        (19, "evening"),
        (21, "night"),
    ],
)
def test_time_slot_covers_a_full_day(hour: int, expected: str) -> None:
    assert patrol.time_slot(datetime(2026, 9, 10, hour, 0)) == expected


@pytest.mark.parametrize(
    ("exe", "title", "expected"),
    [
        ("Code.exe", "main.py - MarisaBOT", "code"),
        ("chrome.exe", "哔哩哔哩", "video"),
        ("steam.exe", "Counter-Strike 2", "game"),
        ("WeChat.exe", "微信", "chat"),
        ("", "", "idle"),
        ("explorer.exe", "下载", "other"),
    ],
)
def test_classify_app_recognizes_main_activities(exe: str, title: str, expected: str) -> None:
    assert patrol.classify_app(exe, title) == expected


def test_work_state_keeps_since_time_for_same_category() -> None:
    """同一分类内切换窗口不应重置专注时长。"""

    state = patrol.WorkState()

    # 首次从默认的 other 切到 code，应报告被切走的旧分类
    assert state.update("code", "Code.exe", "a.py") == "other"
    started_at = state.since

    assert state.update("code", "Code.exe", "b.py") is None
    assert state.since == started_at
    assert state.title == "b.py"

    assert state.update("video", "chrome.exe", "bilibili") == "code"
    assert state.since >= started_at
    assert state.duration_minutes() == 0


def test_angle_avoids_repeating_within_cooldown() -> None:
    """刚说过的角度在冷却期内不应再次被选中。"""

    director = _build_director()
    director.observe_foreground({"exe": "Code.exe", "title": "main.py"})

    spoken: List[str] = []
    for _ in range(3):
        angle = director.pick_angle()
        assert angle is not None
        spoken.append(angle)
        director._records.append(
            patrol.PatrolRecord(text=f"第{len(spoken)}句", angle=angle, category="code", at=time.time())
        )

    # 连续三次都应换新角度（写代码时有休息/喝水/护眼三个候选）
    assert len(set(spoken)) == 3


def test_angle_returns_none_when_all_candidates_cooling() -> None:
    """候选角度全部处于冷却时选择沉默，而不是反复念叨同一句。"""

    director = _build_director()
    director.observe_foreground({"exe": "explorer.exe", "title": "下载"})
    now = time.time()
    for angle in ("rest", "hydration", "tease", "care"):
        director._records.append(
            patrol.PatrolRecord(text=f"{angle}", angle=angle, category="other", at=now)
        )

    assert director.pick_angle(now + 60) is None


@pytest.mark.asyncio
async def test_generate_skips_when_all_angles_cooling() -> None:
    """全部候选冷却时不应调用模型，也不应播报。"""

    calls: List[str] = []

    async def fake_llm(*, model: str, prompt: Any) -> str:
        del prompt
        calls.append(model)
        return "不该出现的一句话"

    director = patrol.PatrolDirector(llm_call=fake_llm, logger=_LoggerStub())
    director.observe_foreground({"exe": "explorer.exe", "title": "下载"})
    now = time.time()
    for angle in ("rest", "hydration", "tease", "care"):
        director._records.append(
            patrol.PatrolRecord(text=f"{angle}", angle=angle, category="other", at=now)
        )

    record = await director.generate(
        observation="主人在整理文件",
        favorability=10,
        rank_name="初识的魔法使",
        model="utils",
    )

    assert record is None
    assert calls == []


@pytest.mark.asyncio
async def test_generate_passes_state_and_recent_lines_to_model() -> None:
    """生成时应把时段、活动、已说过的话一起交给模型。"""

    captured: Dict[str, Any] = {}

    async def fake_llm(*, model: str, prompt: Any) -> str:
        captured["model"] = model
        captured["prompt"] = prompt
        return "  又坐了一小时啦，起来动动だぜ！  "

    director = patrol.PatrolDirector(llm_call=fake_llm, logger=_LoggerStub())
    director.observe_foreground({"exe": "Code.exe", "title": "main.py - MarisaBOT"})
    director._records.append(
        patrol.PatrolRecord(text="上次说过：该喝水了", angle="rest", category="code", at=1.0)
    )

    record = await director.generate(
        observation="主人正在写 Python 代码",
        favorability=76,
        rank_name="亲密搭档",
        model="utils",
    )

    assert record is not None
    assert record.text == "又坐了一小时啦，起来动动だぜ！"
    assert captured["model"] == "utils"
    system_prompt = captured["prompt"][0]["content"]
    assert "亲密搭档" in system_prompt
    assert "写代码/开发" in system_prompt
    assert "主人正在写 Python 代码" in system_prompt
    assert "上次说过：该喝水了" in system_prompt


@pytest.mark.asyncio
async def test_generate_skips_empty_reply() -> None:
    """模型没给出内容时不应播报，也不应写入记录。"""

    director = _build_director(reply="   ")
    record = await director.generate(observation="空白", favorability=10, rank_name="初识的魔法使", model="utils")
    assert record is None
    assert director._records == []
