"""截图方式对比：MAA 看到的画面 vs 真实屏幕。

目的（主人要的"决定性实验"）：
    MAA 在 Wait* 任务上死等，怀疑是 PrintWindow 对 Unity/D3D 窗口返回**过期帧** ——
    它看到的画面停在某一刻不再更新，于是永远等一个"在真实画面里早就出现、
    在它看到的旧帧里还没出现"的元素。
    这里在同一时刻抓两张图：
        A = MaaCore 的截图（materials，PrintWindow）
        B = 直接抓屏幕（Pillow ImageGrab，真实画面）
    再算像素差异：
        差异很小 → MAA 看得到，问题在识别/ROI
        差异很大 → MAA 看到的是过期帧，截图方式是元凶 ★

必须提权：MaaCore 附着窗口要求管理员（UIPI）。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugins" / "marisa_maa"))

from maa_driver import MaaDriver  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "data"


def game_rect() -> tuple[int, int, int, int]:
    u32 = ctypes.windll.user32
    hwnd = u32.FindWindowW(None, "明日方舟")
    origin = wt.POINT(0, 0)
    u32.ClientToScreen(hwnd, ctypes.byref(origin))
    rect = wt.RECT()
    u32.GetClientRect(hwnd, ctypes.byref(rect))
    return origin.x, origin.y, rect.right - rect.left, rect.bottom - rect.top


def main() -> int:
    from PIL import Image, ImageChops, ImageGrab

    ox, oy, cw, ch = game_rect()
    print(f"  游戏客户区 原点=({ox},{oy}) 尺寸={cw}x{ch}", flush=True)

    driver = MaaDriver(
        maa_dir=Path("D:/MAA"),
        window_title="明日方舟",
        screencap_method=16,      # PrintWindow
        input_method=64,
    )
    driver.load()
    print(f"  已连接={driver.connected()}", flush=True)

    OUT.mkdir(parents=True, exist_ok=True)
    shots: list[tuple[str, Path]] = []

    # 连抓 3 张 MAA 视角的图：中间隔 3 秒。若三张完全一样 → PrintWindow 给了静态帧（过期）
    for i in range(3):
        data = driver.screenshot()
        if data:
            p = OUT / f"cmp_maa_{i}.png"
            p.write_bytes(data)
            shots.append((f"MAA视角#{i}", p))
            print(f"  MAA 截图 #{i}: {len(data)} 字节", flush=True)
        else:
            print(f"  MAA 截图 #{i}: 失败", flush=True)
        if i < 2:
            import time
            time.sleep(3)

    # 真实屏幕（同一区域）
    ground = ImageGrab.grab(bbox=(ox, oy, ox + cw, oy + ch))
    gp = OUT / "cmp_screen.png"
    ground.save(gp)
    shots.append(("真实屏幕", gp))
    print(f"  真实屏幕截图: {ground.size}", flush=True)
    driver.close()

    # 对比：把 MAA 的三张缩放到屏幕图尺寸后逐像素比较
    print()
    import numpy as np
    base = np.asarray(ground.convert("RGB"), dtype=np.int16)
    prev = None
    for label, p in shots[:-1]:
        im = Image.open(p).convert("RGB").resize(ground.size)
        arr = np.asarray(im, dtype=np.int16)
        diff = np.abs(arr - base).mean()
        print(f"  {label} vs 真实屏幕：平均像素差 {diff:6.2f}", flush=True)
        if prev is not None:
            d2 = np.abs(arr - prev).mean()
            print(f"  {label} vs 上一张 MAA 图：平均像素差 {d2:6.2f}", flush=True)
        prev = arr

    print()
    print("  判定参考：")
    print("    · MAA 三张之间差异≈0  → PrintWindow 返回【静态帧】（过期）★ 换截图方式")
    print("    · MAA 三张之间有差异  → 截图在更新，问题在识别/ROI")
    print("    · MAA 图与真实屏幕差异大 → 内容对不上，截图方式有问题")
    print(f"\n  图片已存到 {OUT}（cmp_*.png），主人可直接看效果")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
