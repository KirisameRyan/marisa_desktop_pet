"""鼠标点击坐标记录器：把主人的点击直接换算成 MAA 的 1280x720 参考空间坐标。

为什么不用现成的鼠标宏（TinyTask / KeymouseGo / AHK）：
    它们录出来的是**屏幕坐标**，而 MAA 任务链的 roi / specificRect 用的是
    **1280x720 参考空间**。中间隔着窗口位置 + DPI 缩放两层换算 ——
    这两层今天已经坑了很多次（UI 缩放 125% 时坐标会整片错位）。
    所以直接在这里换算好，主人点完就能把数字抄进任务链。

用法：
    1. 让游戏停在要点击的那一屏，窗口别动
    2. **以管理员身份运行**（双击 data/record_clicks.bat，它会自提权）
    3. 依次点击你要记录的按钮，每点一下就记一条
    4. 按 Esc 结束，坐标会打印出来并写入 data/maa_clicks.json

**必须提权**：Windows 的 UIPI 禁止低完整性进程读取高完整性进程的输入状态。
明日方舟 PC 客户端是提权的，所以普通权限下 GetAsyncKeyState 对游戏里的点击
一律返回 0 —— 主人实测的现象就是"游戏里的点击记不到、游戏外的都能记到"。
（普通窗口的点击不受影响，所以一开始容易误以为是脚本坏了。）

输出里同时给屏幕坐标和 MAA 坐标，方便核对。
"""

from __future__ import annotations

import ctypes
import json
import sys
import time
from pathlib import Path

# DPI 感知必须开：否则在 125% 缩放的显示器上 GetCursorPos 返回的是逻辑值，
# 和 MAA（DPI 感知）看到的物理像素对不上，换算出来的坐标会整体偏掉。
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)   # PROCESS_PER_MONITOR_DPI_AWARE
except Exception as exc:  # noqa: BLE001
    print(f"  警告：设置 DPI 感知失败（{exc}），坐标可能不准", flush=True)

u32 = ctypes.windll.user32


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


VK_LBUTTON = 0x01
VK_ESCAPE = 0x1B
MAA_W, MAA_H = 1280, 720

# 每屏备注，方便我拿到坐标后知道它属于哪一步（主人点的时候按顺序对应）
STEPS = [
    "① 点「生息演算」卡片",
    "② 选主题「重启锚点」",
    "③ 进入后的第一个按钮",
    "④ 后续按钮（自行对应）",
]


def game_geometry() -> tuple[int, int, int, int]:
    """返回游戏客户区的屏幕原点和尺寸。"""

    hwnd = u32.FindWindowW(None, "明日方舟")
    if not hwnd:
        raise RuntimeError("找不到「明日方舟」窗口 —— 游戏没开？")
    origin = POINT(0, 0)
    u32.ClientToScreen(hwnd, ctypes.byref(origin))   # 客户区左上角的屏幕坐标
    rect = RECT()
    u32.GetClientRect(hwnd, ctypes.byref(rect))
    return origin.x, origin.y, rect.right - rect.left, rect.bottom - rect.top


def main() -> int:
    origin_x, origin_y, client_w, client_h = game_geometry()
    print("  游戏客户区: 原点=(%d,%d) 尺寸=%dx%d" % (origin_x, origin_y, client_w, client_h))
    print("  MAA 参考空间: %dx%d" % (MAA_W, MAA_H))
    print("  换算方式: maa = (屏幕 - 客户区原点) * 参考尺寸 / 客户区尺寸")
    print()
    print("  请依次点击要记录的按钮，每点一下记一条；按 Esc 结束。")
    for i, step in enumerate(STEPS):
        print("      %s" % step)
    print()

    records: list[dict[str, object]] = []
    was_down = False
    out_path = Path(__file__).resolve().parent.parent / "data" / "maa_clicks.json"

    while True:
        if u32.GetAsyncKeyState(VK_ESCAPE) & 0x8000:
            print("\n  收到 Esc，结束记录。", flush=True)
            break

        down = bool(u32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)
        if down and not was_down:                       # 只记按下的那一下
            pos = POINT()
            u32.GetCursorPos(ctypes.byref(pos))
            maa_x = round((pos.x - origin_x) * MAA_W / client_w)
            maa_y = round((pos.y - origin_y) * MAA_H / client_h)
            record = {
                "index": len(records) + 1,
                "screen": [pos.x, pos.y],
                "maa": [maa_x, maa_y],
            }
            records.append(record)
            print("    #%d  屏幕(%d,%d)  →  MAA(%d,%d)"
                  % (record["index"], pos.x, pos.y, maa_x, maa_y), flush=True)
        was_down = down
        time.sleep(0.02)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps({"client": {"origin": [origin_x, origin_y], "size": [client_w, client_h]},
                    "clicks": records}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("\n  已写入 %s（%d 条）" % (out_path, len(records)))
    print("  把这些坐标发给我，我就能写进 MAA 任务链。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
