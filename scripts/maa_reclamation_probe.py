"""探测 Reclamation（生息演算）任务在 C API 里的正确写法。

背景：GUI 配置里是
    $type = ReclamationTask, TaskType = "Reclamation"
    Theme = "RelaunchAnchor", Mode = "RA4", IncrementMode = 0, MaxCraftCountPerRound = 16
但直接 append_task("Reclamation", {"theme":..., "mode":"RA4"}) 返回 0（被 MaaCore 拒）。
GUI 用的是 PascalCase 的内部模型名，C API 用 snake_case，两者不一定一一对应——
所以这里一次把所有候选写法都试一遍，看哪个返回非 0。

必须提权运行：未提权时附着就会被 UIPI 拒。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugins" / "marisa_maa"))

from maa_driver import MaaDriver  # noqa: E402

VARIANTS = [
    ("Reclamation", {}),
    ("Reclamation", {"theme": "RelaunchAnchor"}),
    ("Reclamation", {"theme": "RelaunchAnchor", "mode": 4}),
    ("Reclamation", {"theme": "RelaunchAnchor", "mode": "RA4"}),
    ("Reclamation", {"theme": "RelaunchAnchor", "mode": "RA4", "increment_mode": 0}),
    ("Reclamation", {"theme": "RelaunchAnchor", "mode": "RA4", "increment_mode": 0,
                     "max_craft_count_per_round": 16, "clear_store": False}),
    ("Reclamation", {"theme": "RelaunchAnchor", "mode": "RA4", "increment_mode": 0,
                     "max_craft_count_per_round": 16, "clear_store": False, "tool_to_craft": ""}),
    ("ReclamationAlgorithm", {}),
    ("ReclamationTask", {}),
]


def main() -> int:
    driver = MaaDriver(
        maa_dir=Path("D:/MAA"),
        window_title="明日方舟",
        screencap_method=16,   # PrintWindow（官方要求）
        input_method=128,      # PostMsg-WindowPos（官方推荐）
    )
    print("  附着中…", flush=True)
    driver.load()
    print(f"  已连接={driver.connected()} 客户区={driver.client_size()}", flush=True)
    print("", flush=True)

    ok_list = []
    for type_name, params in VARIANTS:
        try:
            task_id = driver.append_task(type_name, params)
        except Exception as exc:   # noqa: BLE001
            print(f"  {type_name:<22} {json.dumps(params, ensure_ascii=False)[:60]:<62} 异常: {exc}", flush=True)
            continue
        mark = "✅ 接受" if task_id else "❌ 被拒"
        print(f"  {type_name:<22} {json.dumps(params, ensure_ascii=False)[:60]:<62} {mark} (id={task_id})", flush=True)
        if task_id:
            ok_list.append((type_name, params, task_id))

    print("", flush=True)
    if ok_list:
        print("  === 可用写法 ===", flush=True)
        for type_name, params, task_id in ok_list:
            print(f"    {type_name} {json.dumps(params, ensure_ascii=False)}", flush=True)
    else:
        print("  全部被拒 —— 任务名或参数都不对，需要查 MAA 的 Reclamation 任务文档", flush=True)
    driver.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
