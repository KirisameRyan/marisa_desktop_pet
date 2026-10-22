"""探测 MaaCore 接受的 Reclamation（生息演算）参数。

用法（不需要管理员、不需要开游戏）：
    D:\\MarisaBOT\\.venv\\Scripts\\python.exe resources\\probe_reclamation.py

只做 AsstAppendTask 入队验证：返回 id 非 0 = 参数合法。
不会 attach 窗口、不会启动任务。

结论（实测，MaaCore v6.18.0 —— 对应源码 ReclamationConfig.cpp/h）：
    1. theme 只有 "Tales"（沙洲遗闻）和 "RelaunchAnchor"（重启锚点）两个合法值，
       "Fire" 已下线。
    2. **重启锚点的 mode 是 flags 值**：
           RA-1  = 1 << 4 = 16
           RA-15 = 2 << 4 = 32
           RA-4  = 3 << 4 = 48
       沙洲遗闻的 mode 才是 0/1（无存档/有存档刷点数）。
    3. **参数必须给全**（increment_mode / num_craft_batches / tools_to_craft /
       clear_store）。只给 theme+mode 时 MaaCore 内部会抛 C++ 异常
       （ctypes 报 OSError 0xe06d7363）——MaaCore 自身的处理缺陷，
       插件永远按完整参数下发（见 plugin._build_tasks）。
"""

from __future__ import annotations

import sys
from pathlib import Path

MAA_DIR = Path(r"D:\MAA")
MAA_PYTHON_DIR = MAA_DIR / "Python"

if str(MAA_PYTHON_DIR) not in sys.path:
    sys.path.insert(0, str(MAA_PYTHON_DIR))

from asst.asst import Asst  # noqa: E402


def full_params(theme: str, mode: int) -> dict:
    """与 MAA GUI 序列化结果完全一致的完整参数。"""

    return {
        "theme": theme,
        "mode": mode,
        "increment_mode": 0,
        "num_craft_batches": 16,
        "tools_to_craft": ["荧光棒"],
        "clear_store": False,
    }


def main() -> int:
    if not Asst.load(path=MAA_DIR):
        print(f"MaaCore 资源加载失败：{MAA_DIR}")
        return 1

    # 回调必须持有 CFUNCTYPE 引用（被 GC 后核心回调就是野指针），这里挂一个空的
    asst = Asst(callback=Asst.CallBackType(lambda msg, details, arg: None))

    cases = [
        ("RA-4（RelaunchAnchor + 48）", full_params("RelaunchAnchor", 48)),
        ("RA-1（RelaunchAnchor + 16）", full_params("RelaunchAnchor", 16)),
        ("RA-15（RelaunchAnchor + 32）", full_params("RelaunchAnchor", 32)),
        ("沙洲遗闻-有存档（Tales + 1）", full_params("Tales", 1)),
        ("错误写法：RelaunchAnchor + 1（预期 id=0）", full_params("RelaunchAnchor", 1)),
        ("错误写法 theme=Fire（预期 id=0）", full_params("Fire", 1)),
    ]

    print(f"MaaCore 版本：{asst.get_version()}")
    for label, params in cases:
        try:
            task_id = asst.append_task("Reclamation", params)
            verdict = "接受" if task_id else "拒绝"
            print(f"  {label} -> {verdict}（id={task_id}）")
        except OSError as exc:
            print(f"  {label} -> 异常：{exc}")
    del asst
    print("完成：id 非 0 的写法才是插件该用的参数。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
