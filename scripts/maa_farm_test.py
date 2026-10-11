"""MAA 直连测试：绕过 MaiBot，直接驱动 MaaCore 刷 1-7，验证链路是否真的能挂机。

为什么需要独立测试：插件跑在 MaiBot 的扩展进程里，排查时要同时盯 QQ、插件、MaaCore
三处日志。这个脚本把中间层全去掉，只留 驱动 → 游戏，把问题范围缩到最小。

成功判据（按可靠度排序）：
    1. 收到 SubTaskExtraInfo / what=StageDrops —— **真的打完一局并识别到掉落**，铁证
    2. 收到 TaskChainCompleted {taskchain:"Fight"}
    3. 截图里出现战斗/结算界面
只要没出现 StageDrops，就说明根本没进关卡，别被"任务完成"骗了。

用法：
    python scripts/maa_farm_test.py --stage 1-7 --times 1 --input 64 --minutes 6
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugins" / "marisa_maa"))

from maa_driver import MaaDriver, SCREENCAP_FRAME_POOL  # noqa: E402

MSG_ALL_DONE = 3
MSG_TASK_CHAIN_START = 10000
MSG_TASK_CHAIN_COMPLETED = 10002
MSG_TASK_CHAIN_ERROR = 10003
MSG_SUB_TASK_ERROR = 20003 - 1   # 20002 之后的错误号，仅用于打印
MSG_SUB_TASK_EXTRA_INFO = 20003


def main() -> int:
    parser = argparse.ArgumentParser(description="MAA 直连挂机测试")
    parser.add_argument("--maa-dir", type=Path, default=Path("D:/MAA"))
    parser.add_argument("--window", default="明日方舟")
    parser.add_argument("--stage", default="1-7")
    parser.add_argument("--times", type=int, default=1)
    parser.add_argument("--input", type=int, default=64, help="输入方式：64=PostMessageWithCursorPos")
    parser.add_argument("--screencap", type=int, default=SCREENCAP_FRAME_POOL)
    parser.add_argument("--minutes", type=float, default=6.0, help="最多跑多少分钟")
    parser.add_argument("--task", default="", help="任务类型，留空=StartUp+Fight；生息演算填 Reclamation")
    parser.add_argument("--params", default="", help="任务参数 JSON，如 '{\"theme\":\"RelaunchAnchor\",\"mode\":\"RA4\"}'")
    parser.add_argument("--repeat", type=int, default=1, help="把任务重复入队几次（刷几遍）")
    parser.add_argument("--startup", action="store_true",
                        help="先入队 StartUp 回到游戏主界面。生息演算等任务需要从主界面开始导航"
                             "（MAA 的 Tales@RA@Navigation-FromMainInterface），否则会卡在导航阶段")
    parser.add_argument("--out", type=Path, default=Path("data/maa_farm_test"))
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    events = []
    driver = MaaDriver(
        maa_dir=args.maa_dir,
        window_title=args.window,
        screencap_method=args.screencap,
        input_method=args.input,
        on_event=events.append,
    )

    print(f"[1/4] 加载 MaaCore 并附着窗口…（输入方式={args.input}，截图={args.screencap}）", flush=True)
    version = driver.load()
    print(f"      版本 {version} · 已连接={driver.connected()} · 客户区={driver.client_size()}", flush=True)

    shot = driver.screenshot()
    if shot:
        (args.out / "00_before.png").write_bytes(shot)
        print(f"[2/4] 基线截图 {len(shot)} 字节", flush=True)

    if args.task:
        # 指定了任务类型（如 Reclamation=生息演算）就直接跑它，不掺 StartUp——
        # 除非显式 --startup：生息演算的导航阶段要求从「主界面」出发
        # （MAA 日志里期望识别 Tales@RA@Navigation-FromMainInterface），
        # 游戏若停在别的界面（比如 RA-4 关卡地图），导航会重试到上限后报错。
        task_params = json.loads(args.params) if args.params else {}
        queued = []
        if args.startup:
            queued.append(driver.append_task("StartUp",
                                             {"client_type": "Official", "start_game_enabled": False}))
        # --params 传数组时，每个元素各入队一个任务。
        # 生息演算需要两步：先 Tales@RA@Navigation 从主界面导航进生息演算，
        # 再 RelaunchAnchor@RA@RA4-Entry 进 RA-4（Custom 只会执行数组里第一个匹配上的任务）
        for param_set in (task_params if isinstance(task_params, list) else [task_params] * max(1, args.repeat)):
            queued.append(driver.append_task(args.task, param_set))
        print(f"[3/4] 入队 {'StartUp + ' if args.startup else ''}{args.task} × {len(queued) - (1 if args.startup else 0)}"
              f" → task_id={queued}，参数={task_params}", flush=True)
        if not all(queued):
            print("      ❌ 有任务入队失败（返回 0），MaaCore 拒绝了", flush=True)
            return 2
    else:
        startup = driver.append_task("StartUp", {"client_type": "Official", "start_game_enabled": False})
        fight = driver.append_task("Fight", {"stage": args.stage, "times": args.times})
        print(f"[3/4] 入队 StartUp={startup} Fight={fight}（{args.stage} × {args.times}）", flush=True)
        if not startup or not fight:
            print("      ❌ 入队失败，MaaCore 拒绝了任务", flush=True)
            return 2

    worker = threading.Thread(target=driver.start, daemon=True)

    def _run() -> None:
        # 注意：AsstStart **不阻塞**——它在 MaaCore 里只是开个后台线程就返回 true
        # （C 源码：if (running()) return false; m_thread = std::thread(...); return true;）
        # 首版把它当成"跑完才返回"，于是线程秒退、脚本以为干完了就退出进程，
        # AsstStop 把刚起的任务掐死 —— 表现是"入了队、返回 True、什么都没发生"。
        print(f"      调用 start()… 已连接={driver.connected()}", flush=True)
        ok = driver._asst.start()
        print(f"      start() 返回 {ok}（它只表示已启动，不代表跑完）", flush=True)

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()

    drops_seen = False
    fight_done = False
    deadline = time.time() + args.minutes * 60
    step = 0
    while time.time() < deadline:
        time.sleep(5)
        # 消费新增的回调
        while events:
            event = events.pop(0)
            if event.get("type") != "maa_message":
                continue
            msg = int(event.get("msg") or 0)
            data = event.get("data") or {}
            if msg == MSG_SUB_TASK_EXTRA_INFO:
                what = data.get("what") or ""
                if what == "StageDrops":
                    details = data.get("details") or {}
                    items = "、".join(
                        f"{d.get('itemName')}×{d.get('quantity')}"
                        for d in (details.get("drops") or []) if d.get("itemName")
                    )
                    print(f"      ★★ 掉落识别到了：{items}", flush=True)
                    drops_seen = True
                elif what in ("StageInfo", "UseMedicine", "StoneUsed"):
                    print(f"      子任务信息 {what}: {json.dumps(data.get('details'), ensure_ascii=False)[:120]}", flush=True)
            elif msg == MSG_TASK_CHAIN_START:
                print(f"      ▶ 开始 {data.get('taskchain')}", flush=True)
            elif msg == MSG_TASK_CHAIN_COMPLETED:
                chain = data.get("taskchain")
                print(f"      ✔ 完成 {chain}", flush=True)
                if chain == "Fight":
                    fight_done = True
            elif msg == MSG_TASK_CHAIN_ERROR:
                print(f"      ✘ 出错 {data.get('taskchain')} · {data.get('why')}", flush=True)
            elif msg == MSG_ALL_DONE:
                print("      🎉 全部完成", flush=True)

        step += 1
        if step % 4 == 0:      # 每 20 秒存一张图
            shot = driver.screenshot()
            if shot:
                (args.out / f"{step:02d}_t{step * 5}s.png").write_bytes(shot)

        # 结束条件看 **AsstRunning**，不看线程存活——start() 是立即返回的（见上面的注释）
        if fight_done or not driver.is_running:
            if not driver.is_running:
                time.sleep(3)          # 给最后几条回调一点时间落地
                break
            break

    print(f"\n[4/4] 结果：仍在运行={driver.is_running} · Fight完成={fight_done} · "
          f"**掉落={drops_seen}**", flush=True)
    print(f"      截图与现场见 {args.out}", flush=True)
    driver.close()
    return 0 if drops_seen else 1


if __name__ == "__main__":
    raise SystemExit(main())
