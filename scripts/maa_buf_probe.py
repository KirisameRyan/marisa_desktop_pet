"""探测 MaaCore 实际返回的截图尺寸，以及它是否受缓冲区大小影响。

背景：主人游戏内是 1920x1080，但抓到的 PNG 一直是 1280x720。两种可能：
    ① MAA 内部就按 1280x720 参考分辨率处理（那点击坐标的换算基准就不是 1920x1080）
    ② 我给 AsstGetImage 的缓冲区不够（我按 client_size() 算，而本进程 DPI 不感知，
       读到的是 1536x864 逻辑值，可能偏小）
这个脚本用不同大小的缓冲区各取一次，看 PNG 的 IHDR 尺寸有没有变化。

必须提权运行：未提权时 PrintWindow 会被 UIPI 拒（error 5）。
"""

from __future__ import annotations

import ctypes
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugins" / "marisa_maa"))

from maa_driver import MaaDriver  # noqa: E402


def png_size(data: bytes) -> tuple[int, int] | None:
    """从 PNG 的 IHDR 读宽高。"""

    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return struct.unpack(">II", data[16:24])


def main() -> int:
    driver = MaaDriver(
        maa_dir=Path("D:/MAA"),
        window_title="明日方舟",
        screencap_method=16,      # PrintWindow，与 GUI 的 Win32Extra 一致
        input_method=64,          # PostMessageWithCursorPos
    )
    driver.load()
    lib, ptr = driver._lib, driver._ptr
    lib.AsstAsyncScreencap.argtypes = [ctypes.c_void_p, ctypes.c_uint8]
    lib.AsstAsyncScreencap.restype = ctypes.c_int32
    lib.AsstGetImage.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint64]
    lib.AsstGetImage.restype = ctypes.c_uint64

    print(f"  进程 DPI 感知 = {ctypes.windll.user32.GetAwarenessFromDpiAwarenessContext(ctypes.windll.user32.GetThreadDpiAwarenessContext())}")
    print(f"  driver.client_size() = {driver.client_size()}")
    width, height = driver.client_size()
    print(f"  屏幕 = {ctypes.windll.user32.GetSystemMetrics(0)}x{ctypes.windll.user32.GetSystemMetrics(1)}")

    for label, size in (
        ("按 client_size 算", width * height * 4),
        ("屏幕大小 *4", ctypes.windll.user32.GetSystemMetrics(0) * ctypes.windll.user32.GetSystemMetrics(1) * 4),
        ("64 MB", 64 * 1024 * 1024),
    ):
        lib.AsstAsyncScreencap(ptr, 1)
        buffer = ctypes.create_string_buffer(size)
        got = int(lib.AsstGetImage(ptr, buffer, size))
        if not got or got >= (1 << 64) - 1:
            print(f"  {label:<18} 缓冲 {size // 1024 // 1024:>3}MB → 取图失败")
            continue
        data = buffer.raw[:got]
        wh = png_size(data)
        print(f"  {label:<18} 缓冲 {size // 1024 // 1024:>3}MB → {got:>9} 字节 · PNG {wh}")
        if wh:
            (Path("data") / f"probe_{wh[0]}x{wh[1]}.png").write_bytes(data)
    driver.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
