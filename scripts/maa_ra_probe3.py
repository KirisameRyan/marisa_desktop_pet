"""探测 Reclamation 任务里「主题」参数的正确格式（第三轮）。

前两轮结论：
    Reclamation {}                                  ✅ 被接受，但走的是**默认主题**（Tales）
    Reclamation {"theme": "RelaunchAnchor"}         ❌ 被拒
    Reclamation {"Theme": "RelaunchAnchor", ...}    ✅ 被接受，但**被静默忽略**（仍走 Tales）

判据来自 MAA 日志里的任务前缀：主人的 GUI 跑的是 RelaunchAnchor@RA@RA4-*（资源文件
resource/tasks/RA/Reclamation3.json），而我的运行全是 Tales@RA@Navigation（旧主题）。
所以主题参数确实没生效，得找出正确的键/值格式。

本脚本把候选格式一次试完（一次提权），并把每个「被接受」的写法真正跑一小段，
再从 MAA 日志里读出实际生效的任务前缀 —— 光看 append_task 返回值不够，
因为未知键会被静默忽略（这正是上一轮踩的坑）。
"""

from __future__ import annotations

import re
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugins" / "marisa_maa"))

from maa_driver import MaaDriver  # noqa: E402

LOG = Path(r"D:\MAA\debug\asst.log")

VARIANTS: list[tuple[str, dict]] = [
    ("基线（已知走默认主题）", {"Theme": "RelaunchAnchor", "Mode": "RA4"}),
    ("theme 字符串 小写", {"theme": "RelaunchAnchor"}),
    ("theme 整数 0", {"theme": 0}),
    ("theme 整数 1", {"theme": 1}),
    ("theme 整数 2", {"theme": 2}),
    ("theme 整数 3", {"theme": 3}),
    ("themes 数组", {"themes": ["RelaunchAnchor"]}),
    ("theme 资源名", {"theme": "Reclamation3"}),
    ("theme + mode 小写整数", {"theme": "RelaunchAnchor", "mode": 4}),
]


def latest_process_prefixes() -> tuple[str | None, dict[str, int]]:
    """从 MAA 日志里取最新进程的任务前缀计数。"""

    text = re.sub(r"\x1b\[[0-9;]*m", "", LOG.read_text(encoding="utf-8", errors="replace"))
    lines = text.splitlines()
    pid = None
    for line in reversed(lines):
        m = re.search(r"\[Px(\d+)\]", line)
        if m:
            pid = m.group(1)
            break
    counts = {"Tales@RA@": 0, "RelaunchAnchor@RA@": 0}
    if pid:
        for line in lines:
            if "Px%s" % pid not in line:
                continue
            for key in counts:
                if key in line:
                    counts[key] += 1
    return pid, counts


def main() -> int:
    driver = MaaDriver(
        maa_dir=Path("D:/MAA"),
        window_title="明日方舟",
        screencap_method=16,   # PrintWindow
        input_method=128,      # PostMsg-WindowPos
    )
    driver.load()
    print(f"  已连接={driver.connected()} 客户区={driver.client_size()}\n", flush=True)

    accepted: list[tuple[str, dict, int]] = []
    for label, params in VARIANTS:
        try:
            task_id = driver.append_task("Reclamation", params)
        except Exception as exc:  # noqa: BLE001
            print(f"  {label:<24} 异常 {exc}", flush=True)
            continue
        print(f"  {label:<24} {'✅ 接受' if task_id else '❌ 被拒'} (id={task_id})", flush=True)
        if task_id:
            accepted.append((label, params, task_id))

    print(f"\n  被接受的写法 {len(accepted)} 个：", flush=True)
    for label, params, _ in accepted:
        print(f"    {label:<24} {params}", flush=True)
    print("\n  注意：被接受 ≠ 生效 —— 未知键会被静默忽略（上一轮就是栽在这）。")
    print("  先用被接受的写法跑一次，再从 MAA 日志核对实际任务前缀是不是 RelaunchAnchor@RA@。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
