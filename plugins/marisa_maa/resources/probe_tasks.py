"""探测 MaaCore 对各类任务参数的接受情况（不 attach、不启动）。

用法（不需要管理员、不需要开游戏）：
    D:\\MarisaBOT\\.venv\\Scripts\\python.exe resources\\probe_tasks.py

只做 AsstAppendTask 入队验证：返回 id 非 0 = 参数合法。
用于在改 _build_tasks 的参数后复查（MAA 升级后也建议跑一次）。
"""

from __future__ import annotations

import sys
from pathlib import Path

MAA_DIR = Path(r"D:\MAA")
MAA_PYTHON_DIR = MAA_DIR / "Python"

if str(MAA_PYTHON_DIR) not in sys.path:
    sys.path.insert(0, str(MAA_PYTHON_DIR))

from asst.asst import Asst  # noqa: E402


def load_all_resources() -> bool:
    """与 MAA GUI / 插件一致的三层资源加载。

    只加载基础 resource 层时，热更新内容（YW 活动关卡、肉鸽树海任务）会找不到——
    探测必须复刻插件的加载顺序，否则结论不代表插件行为。
    """

    if not Asst.load(path=MAA_DIR):
        return False
    lib = Asst._Asst__lib
    for parent in (MAA_DIR / "cache", MAA_DIR / "resource" / "platform_diff" / "PC"):
        if (parent / "resource").is_dir():
            lib.AsstLoadResource(str(parent).encode("utf-8"))
    return True


CASES = [
    ("StartUp 现状", "StartUp", {"client_type": "Official", "start_game_enabled": False}),
    ("Fight 现状（无 series）", "Fight", {"stage": "1-7", "times": 3}),
    ("Fight 连战（times=10, series=10）", "Fight", {"stage": "1-7", "times": 10, "series": 10}),
    ("Fight 自动连战（series=0）", "Fight", {"stage": "1-7", "times": 6, "series": 0}),
    ("Fight 不切换（series=-1）", "Fight", {"stage": "1-7", "times": 6, "series": -1}),
    ("Fight 热更关卡 YW-4（验证 cache 层加载）", "Fight", {"stage": "YW-4", "times": 10, "series": 10}),
    ("Recruit 精简（当前插件写法）", "Recruit", {"select": [4], "confirm": [3, 4], "times": 4}),
    ("Recruit GUI 风格（补 refresh/force_refresh/set_time/recruitment_time）", "Recruit", {
        "refresh": True,
        "force_refresh": True,
        "select": [4],
        "confirm": [3, 4],
        "times": 4,
        "set_time": True,
        "expedite": False,
        "recruitment_time": {"3": 540, "4": 540},
    }),
    ("Infrast 现状", "Infrast", {"facility": ["Mfg", "Trade", "Power", "Dorm"], "drones": "Money"}),
    ("Infrast 队列换班（mode=20000 + 全参数，插件新写法）", "Infrast", {
        "facility": ["Power", "Office", "Control", "Mfg", "Trade", "Reception",
                     "Dorm", "Processing", "Training", "AssistantChange"],
        "drones": "Money",
        "mode": 20000,
        "threshold": 0.3,
        "continue_training": False,
        "dorm_notstationed_enabled": True,
        "dorm_trust_enabled": True,
        "replenish": True,
        "reception_message_board": True,
        "reception_clue_exchange": True,
        "reception_send_clue": True,
    }),
    ("Roguelike 黑流树海·刷等级（主人配置）", "Roguelike", {
        "theme": "BlackFlow",
        "mode": 0,
        "difficulty": 2,
        "starts_count": 999999,
        "investment_enabled": True,
        "investment_with_more_score": False,
        "investments_count": 999,
        "stop_when_investment_full": False,
        "squad": "特勤分队",
        "roles": "稳扎稳打",
        "core_char_list": [{"name": "机械师", "use_support": False}],
        "stop_at_final_boss": False,
        "stop_at_max_level": False,
        "use_nonfriend_support": False,
    }),
    ("Mall 现状", "Mall", {"shopping": True, "buy_first": ["招聘许可", "龙门币"]}),
    ("Mall + visit_friends（替代 Visit 用）", "Mall", {
        "shopping": True,
        "buy_first": ["招聘许可", "龙门币"],
        "visit_friends": True,
    }),
    ("Award 现状", "Award", {"award": True, "mail": True}),
    ("CloseDown（关闭游戏客户端）", "CloseDown", {"client_type": "Official"}),
    ("Visit（疑似非法任务类型）", "Visit", {}),
]


def main() -> int:
    if not load_all_resources():
        print(f"MaaCore 资源加载失败：{MAA_DIR}")
        return 1

    asst = Asst(callback=Asst.CallBackType(lambda msg, details, arg: None))
    print(f"MaaCore 版本：{asst.get_version()}")
    for label, task_type, params in CASES:
        try:
            task_id = asst.append_task(task_type, params)
            verdict = "接受" if task_id else "拒绝"
            print(f"  {label} -> {verdict}（id={task_id}）")
        except OSError as exc:
            print(f"  {label} -> 异常：{exc}")
    del asst
    print("完成：id 非 0 的写法才是插件该用的参数。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
