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

# 急停标志：跑的时候在仓库根目录建这个文件，脚本下一轮就会收工并释放实例。
# 起因是测试进程被提权拉起后，非提权的 shell 停不掉它（CommandLine 读不到、
# 模块枚举不了、taskkill 报 Access denied），只能等 --minutes 到点。
STOP_FLAG = Path(__file__).resolve().parent.parent / "data" / "maa_stop.flag"


def main() -> int:
    parser = argparse.ArgumentParser(description="MAA 直连挂机测试")
    parser.add_argument("--maa-dir", type=Path, default=Path("D:/MAA"))
    parser.add_argument("--window", default="明日方舟")
    parser.add_argument("--stage", default="1-7")
    parser.add_argument("--times", type=int, default=1)
    parser.add_argument("--input", type=int, default=64, help="输入方式：64=PostMessageWithCursorPos")
    parser.add_argument("--screencap", type=int, default=SCREENCAP_FRAME_POOL)
    parser.add_argument("--minutes", type=float, default=6.0, help="最多跑多少分钟")
    parser.add_argument("--stall-sec", type=float, default=180.0,
                        help="看门狗阈值：多少秒没有任何任务事件就判定卡住并重启任务链。"
                             "MAA 的任务链是线性的，游戏偏离预期（战斗结束/弹窗/识别失败）时"
                             "next 里没有一项匹配，它会停在 Wait* 上死等 —— 这是主人观察到的"
                             "『跑到一半卡住不知道自己该干嘛』。官方链靠 next 末尾挂"
                             "ClickCornerUntilReturnButton/ReturnButtons/CloseAnnos/StartUp 兜底")
    parser.add_argument("--task", default="", help="任务类型，留空=StartUp+Fight；生息演算填 Reclamation")
    parser.add_argument("--params", default="", help="任务参数 JSON，如 '{\"theme\":\"RelaunchAnchor\",\"mode\":\"RA4\"}'")
    parser.add_argument("--queue", default="",
                        help="一次下发多种任务：[{\"type\":\"Depot\",\"params\":{}}, ...]。"
                             "用于测试「首页 → 各功能页」的导航链路")
    parser.add_argument("--queue-file", default="",
                        help="从文件读任务队列 JSON。**推荐用这个** —— 经 .bat 传 JSON 会被"
                             "cmd 的引号规则搞坏（踩过），落成文件再传路径最稳")
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

    queue_spec = args.queue or (Path(args.queue_file).read_text(encoding="utf-8")
                                if args.queue_file else "")
    # 看门狗重启任务链时要按原样重新下发，所以留一份副本
    pending_tasks: list[dict] = []
    if queue_spec:
        # 一次下发多种任务：用来测「首页 → 各功能页」的导航。
        # 每个任务自己会从首页导航过去、做完再回来，所以能同时验证多个入口。
        spec = json.loads(queue_spec)
        pending_tasks = spec
        queued = []
        for item in spec:
            type_name = str(item["type"])
            params = item.get("params") or {}
            task_id = driver.append_task(type_name, params)
            queued.append((type_name, task_id))
            print(f"      {type_name:<12} → task_id={task_id}  {'✅' if task_id else '❌ 被拒'}  {params}", flush=True)
        if not all(tid for _, tid in queued):
            print("      ❌ 有任务入队失败", flush=True)
            return 2
    elif args.task:
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
        pending_tasks = [{"type": args.task, "params": ps} for ps in (task_params if isinstance(task_params, list) else [task_params])]
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
    # 看门狗：MAA 的任务链是**线性**的，每步只认它期望的画面。游戏一旦偏离预期
    # （战斗结束、弹窗、识别失败），next 列表里没有一项匹配，MAA 就停在当前任务上
    # **死等**——主人看到的就是"跑到一半卡住不动，不知道自己该干嘛"（实测卡在
    # RelaunchAnchor@RA@RA4-WaitSkill2 上整整 10 分钟）。
    # 官方链靠 next 末尾挂 ClickCornerUntilReturnButton / ReturnButtons / CloseAnnos /
    # StartUp 这些通用恢复出口兜底；我的 Custom 链没挂，所以在外面再加一层看门狗：
    # 超过 STALL_SEC 没有新的任务事件就判定卡住 → AsstStop → 重新下发 → 重新开始。
    stall_sec = args.stall_sec
    last_progress = time.time()
    last_seen_task = ""
    restarts = 0
    while time.time() < deadline:
        time.sleep(5)
        # 消费新增的回调
        while events:
            event = events.pop(0)
            if event.get("type") != "maa_message":
                continue
            msg = int(event.get("msg") or 0)
            data = event.get("data") or {}
            # ★ 只有**任务名发生变化**才算进展，不能用"收到事件"当进展 ——
            # MAA 的 Wait* 类任务 next 里写着 ["下一步", "#self"]，认不到就无限重试
            # 自己，此时**事件一直在产生但没有任何实际推进**。首版看门狗把每个事件
            # 都当进展，于是永远不触发：卡死在 RelaunchAnchor@RA@RA4-WaitSkill2
            # （等 OCR 认到「READY」，实测 10 分钟一动不动）时它还以为一切正常。
            for key in ("task", "taskchain"):
                now = str(data.get(key) or "")
                if now and now != last_seen_task:
                    last_seen_task = now
                    last_progress = time.time()
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

        # 急停开关：测试进程是提权起的，我自己（非提权的 shell）停不掉它
        # —— 提权进程的 CommandLine 读不到、模块枚举不了、taskkill 也是 Access denied。
        # 所以留一个标志文件，脚本每轮检查，发现就主动收工并释放实例。
        if STOP_FLAG.exists():
            print(f"      检测到 {STOP_FLAG}，主动停止并释放 MAA 实例", flush=True)
            driver.stop()
            STOP_FLAG.unlink(missing_ok=True)
            break

        # 看门狗：卡住就重启任务链（见循环开头的说明）
        stalled_for = time.time() - last_progress
        if stalled_for > stall_sec and driver.is_running:
            restarts += 1
            print(f"      ⏰ 已 {int(stalled_for)} 秒没有任何任务事件 → 判定卡住，"
                  f"重启任务链（第 {restarts} 次）", flush=True)
            driver.stop()
            time.sleep(3)
            for item in pending_tasks:      # 重新下发（内容与首次入队时一致）
                driver.append_task(item["type"], item.get("params") or {})
            driver.start()
            last_progress = time.time()

        # 只在**全部任务**跑完时结束。
        # 不能用 fight_done 提前退出：队列里可能还有后续任务 ——
        # 踩过：Fight 完成后脚本立刻退出，进程一没 AsstStop 就把后面的 RA-4 掐死了，
        # 表现为"日志里 0 个 RA 任务，脚本却已 EXITCODE=0"。
        if not driver.is_running:
            time.sleep(3)          # 给最后几条回调一点时间落地
            break

    print(f"\n[4/4] 结果：仍在运行={driver.is_running} · Fight完成={fight_done} · "
          f"**掉落={drops_seen}** · 看门狗重启 {restarts} 次", flush=True)
    print(f"      截图与现场见 {args.out}", flush=True)
    driver.close()
    return 0 if drops_seen else 1


if __name__ == "__main__":
    raise SystemExit(main())
