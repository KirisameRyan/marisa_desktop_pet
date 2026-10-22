"""RA-4 全流程实机测试脚本（脱离 QQ 直接驱动 MaaDriver）。

用途：不经过 MaiBot / QQ，直接验证「加载 → 附着 → 截图 → 自动进场 → 挂 RA-4 →
停止 → 销毁」整条链路。改插件或改 MAA 版本后，先用它复测再上线。

用法（**需要管理员权限**，游戏客户端开着并登录）：
    D:\\MarisaBOT\\.venv\\Scripts\\python.exe resources\\ra4_live_test.py [观察秒数]

默认观察 1200 秒。日志与截图写到系统临时目录 marisa_maa_ra4/ 下：
    ra4_live.log / ra4_before.png / ra4_after.png

MAA 目录与窗口标题从插件 config.toml 读取（与插件运行时保持一致）。
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from tempfile import gettempdir

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

from maa_driver import MaaDriver  # noqa: E402

WORK_DIR = Path(gettempdir()) / "marisa_maa_ra4"
WORK_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = WORK_DIR / "ra4_live.log"
SHOT_BEFORE = WORK_DIR / "ra4_before.png"
SHOT_AFTER = WORK_DIR / "ra4_after.png"

_log_lock = threading.Lock()


def read_config() -> tuple[Path, str]:
    """从插件 config.toml 读 MAA 目录与窗口标题。"""

    maa_dir = Path(r"D:\MAA")
    window_title = "明日方舟"
    config_path = PLUGIN_DIR / "config.toml"
    if config_path.exists():
        import tomllib

        cfg = tomllib.loads(config_path.read_text(encoding="utf-8"))
        maa_cfg = cfg.get("maa", {})
        maa_dir = Path(maa_cfg.get("maa_dir", maa_dir))
        window_title = str(maa_cfg.get("window_title", window_title))
    return maa_dir, window_title


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    with _log_lock:
        print(line, flush=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


MSG_NAMES = {
    1: "InitFailed", 2: "ConnectionInfo", 3: "AllTasksCompleted",
    4: "AsyncCallInfo", 5: "Destroyed",
    10000: "TaskChainError", 10001: "TaskChainStart", 10002: "TaskChainCompleted",
    10003: "TaskChainExtraInfo", 10004: "TaskChainStopped",
    20000: "SubTaskError", 20001: "SubTaskStart", 20002: "SubTaskCompleted",
    20003: "SubTaskExtraInfo", 20004: "SubTaskStopped",
}
# 只关注这些关键节点的完成事件（等待类任务会大量重复，20 秒内同名只记一次）
KEY_SUB = ("Settle", "Exit", "Continue", "FinishConstruction", "Prepare",
           "OpenConstruction", "UseEWSkill", "WaitEWSkill", "ChooseStrategy", "Entry")
_last_keys: dict = {}


def on_event(ev: dict) -> None:
    msg = int(ev.get("msg") or 0)
    data = ev.get("data") or {}
    name = MSG_NAMES.get(msg, f"MSG{msg}")
    chain = data.get("taskchain") or ""
    if msg in (10001, 10002, 10000, 10004, 3, 1, 2, 5):
        log(f"事件 {name} chain={chain}")
    elif msg == 20000:
        details = data.get("details") or {}
        log(f"事件 SubTaskError task={details.get('task')} chain={chain}")
    elif msg == 20002:
        details = data.get("details") or {}
        task = str(details.get("task") or "")
        if any(k in task for k in KEY_SUB):
            now = time.time()
            if now - _last_keys.get(task, 0) > 20:
                _last_keys[task] = now
                log(f"关键节点 {task}")
    elif msg == 20003:
        what = data.get("what") or ""
        if what in ("StageDrops", "RecruitResult", "RecruitTagsDetected"):
            log(f"事件 ExtraInfo {what} {str(data.get('details'))[:160]}")


def main() -> int:
    observe_sec = int(sys.argv[1]) if len(sys.argv) > 1 else 1200
    maa_dir, window_title = read_config()
    LOG_PATH.write_text("", encoding="utf-8")
    log(f"插件目录：{PLUGIN_DIR}")
    log(f"MAA 目录：{maa_dir}，窗口：{window_title}")

    driver = MaaDriver(
        maa_dir=maa_dir, window_title=window_title,
        screencap_method=16, input_method=64, keyboard_method=2,
        on_event=on_event,
    )
    log("加载 MaaCore ...")
    version = driver.load()
    log(f"附着成功，核心版本 {version}，客户区 {driver.client_size()}")

    shot = driver.screenshot()
    if shot:
        SHOT_BEFORE.write_bytes(shot)
        log(f"起始截图 {len(shot)} 字节 -> {SHOT_BEFORE}")
    else:
        log("起始截图失败（继续）")

    params = {
        "theme": "RelaunchAnchor",
        "mode": 48,  # RA-4 = 3 << 4
        "increment_mode": 0,
        "num_craft_batches": 16,
        "tools_to_craft": ["荧光棒"],
        "clear_store": False,
    }
    # 完整自动流程：回主界面 → 自动进 RA-4（自定义链）→ 官方 Reclamation 挂机
    sequence = [
        ("Custom", {"task_names": ["MarisaGoHome"]}),
        ("Custom", {"task_names": ["MarisaEnterRA"]}),
        ("Reclamation", params),
    ]
    for type_name, task_params in sequence:
        tid = driver.append_task(type_name, task_params)
        label = task_params.get("task_names", [type_name])
        log(f"入队 {type_name} {label} -> id={tid}")
        if not tid:
            log("!! 入队失败，退出")
            driver.close()
            return 1

    started = time.time()
    if not driver.start():
        log("!! AsstStart 返回 False")
        driver.close()
        return 1
    log(f"已启动，观察 {observe_sec} 秒（鼠标会被 MAA 使用，结束后自动交还）...")

    deadline = started + observe_sec
    while time.time() < deadline:
        time.sleep(10)
        elapsed = int(time.time() - started)
        running = driver.is_running
        log(f"…运行中 {elapsed}s running={running} connected={driver.connected()}")
        if not running:
            log("任务队列已跑完（AllTasksCompleted）")
            break

    log("执行停止并截图 ...")
    driver.stop()
    time.sleep(2.0)
    shot = driver.screenshot()
    if shot:
        SHOT_AFTER.write_bytes(shot)
        log(f"结束截图 {len(shot)} 字节 -> {SHOT_AFTER}")
    log("销毁实例 ...")
    driver.close()
    log(f"完成。总时长 {int(time.time() - started)} 秒")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
