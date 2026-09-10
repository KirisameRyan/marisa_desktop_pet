"""Windows 原生交互工具箱（ctypes，零第三方依赖）。

提供：空闲检测、窗口信息/枚举、按点找窗、全屏判断、
送文件进回收站（可恢复）、全局热键注册。
"""
import ctypes
import ctypes.wintypes as wt
import os
from typing import List, Optional, Tuple


# ---------------- 空闲检测 ----------------

def idle_seconds() -> int:
    """返回距最近一次键盘/鼠标输入已过秒数。"""
    try:
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(LASTINPUTINFO)
        ok = ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info))
        if not ok:
            return 0
        millis = ctypes.windll.kernel32.GetTickCount() - info.dwTime
        return max(0, int(millis // 1000))
    except Exception:
        return 0


# ---------------- 窗口相关 ----------------

DESKTOP_CLASSES = (
    "Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
    "#32768",                      # 原生菜单
    "tooltips_class32", "tooltips_class", "#32774",
    "MSCTFIME UI", "IME",          # 输入法/提示类
)


def pid_of_hwnd(hwnd: int) -> int:
    try:
        pid = wt.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value
    except Exception:
        return 0


def _dpi_for_monitor(monitor) -> int:
    try:
        shcore = ctypes.WinDLL("shcore")
        dpi_x = ctypes.c_uint()
        dpi_y = ctypes.c_uint()
        if shcore.GetDpiForMonitor(monitor, 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y)) == 0:
            return dpi_x.value or 96
    except Exception:
        pass
    return 96


def dpi_scale_for_hwnd(hwnd: int) -> float:
    """窗口所在显示器的 DPI 缩放系数（物理像素 → 逻辑像素）。"""
    try:
        user32 = ctypes.windll.user32
        monitor = user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
        dpi = _dpi_for_monitor(monitor)
        return max(1.0, dpi / 96.0)
    except Exception:
        return 1.0


def _window_rect_physical(hwnd) -> Optional[Tuple[int, int, int, int]]:
    try:
        rect = wt.RECT()
        if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        return rect.left, rect.top, rect.right, rect.bottom
    except Exception:
        return None


def window_rect(hwnd: int) -> Optional[Tuple[int, int, int, int]]:
    """返回逻辑坐标（与 Qt 全局坐标同一坐标系），自动按显示器 DPI 归一。"""
    phys = _window_rect_physical(hwnd)
    if not phys:
        return None
    s = dpi_scale_for_hwnd(hwnd)
    return (int(round(phys[0] / s)), int(round(phys[1] / s)),
            int(round(phys[2] / s)), int(round(phys[3] / s)))


def window_at(x: int, y: int) -> int:
    """按逻辑坐标点查最顶层窗口句柄（内部换算物理坐标）。"""
    try:
        user32 = ctypes.windll.user32
        # 先用系统级缩放估物理点 → 找显示器 → 用其真实 DPI 修正
        sys_dpi = user32.GetDpiForSystem() or 96
        guess_x, guess_y = int(x * sys_dpi / 96.0), int(y * sys_dpi / 96.0)
        monitor = user32.MonitorFromPoint(wt.POINT(guess_x, guess_y), 2)
        dpi = _dpi_for_monitor(monitor) or sys_dpi
        px, py = int(x * dpi / 96.0), int(y * dpi / 96.0)
        return user32.WindowFromPoint(wt.POINT(px, py))
    except Exception:
        return 0


def foreground_window() -> int:
    try:
        return int(ctypes.windll.user32.GetForegroundWindow())
    except Exception:
        return 0


def window_title(hwnd: int) -> str:
    """取窗口标题文本（失败返回空串）。"""
    try:
        length = int(ctypes.windll.user32.GetWindowTextLengthW(hwnd))
        if length <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buffer, length + 1)
        return buffer.value.strip()
    except Exception:
        return ""


def foreground_app_info() -> dict:
    """返回前台窗口的 {exe, title, cls}，用于判断主人正在用什么软件。"""
    hwnd = foreground_window()
    if not hwnd:
        return {"exe": "", "title": "", "cls": ""}
    return {
        "exe": _pid_exe_name(pid_of_hwnd(hwnd)),
        "title": window_title(hwnd),
        "cls": window_class_name(hwnd),
    }


def is_window_visible(hwnd: int) -> bool:
    try:
        return bool(ctypes.windll.user32.IsWindowVisible(hwnd))
    except Exception:
        return False


def is_window_minimized(hwnd: int) -> bool:
    try:
        return ctypes.windll.user32.IsIconic(hwnd)
    except Exception:
        return False


def window_class_name(hwnd: int) -> str:
    try:
        buf = ctypes.create_unicode_buffer(256)
        ctypes.windll.user32.GetClassNameW(hwnd, buf, 256)
        return buf.value
    except Exception:
        return ""


def is_fullscreen_primary(hwnd: int, ratio: float = 0.985) -> bool:
    """窗口覆盖【它自己所在显示器】≥ratio 才算全屏（与是否最大化无关）。
    用物理坐标与物理显示器矩形比较，避免 DPI 缩放造成的误判。"""
    try:
        rect = _window_rect_physical(hwnd)
        if not rect:
            return False
        user32 = ctypes.windll.user32
        monitor = user32.MonitorFromWindow(hwnd, 2)

        class MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wt.DWORD),
                ("rcMonitor", wt.RECT),
                ("rcWork", wt.RECT),
                ("dwFlags", wt.DWORD),
            ]

        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if not ctypes.windll.user32.GetMonitorInfoW(monitor, ctypes.byref(mi)):
            return False
        mw = mi.rcMonitor.right - mi.rcMonitor.left
        mh = mi.rcMonitor.bottom - mi.rcMonitor.top
        w = rect[2] - rect[0]
        h = rect[3] - rect[1]
        return mw > 0 and mh > 0 and (w * h) >= (mw * mh) * ratio
    except Exception:
        return False


def find_window_of_process(exe_name: str, hwnd_filter=None) -> int:
    """按可执行名找第一个可见主窗口句柄（hwnd_filter 可选谓词）。"""
    return _find_window(exe_name, hwnd_filter)


def _find_window(exe_name: str, hwnd_filter) -> int:
    result = [0]
    EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

    def cb(hwnd, lparam):
        if not is_window_visible(hwnd):
            return True
        pid = wt.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if hwnd_filter and not hwnd_filter(hwnd):
            return True
        if _pid_exe_name(pid.value).lower() == exe_name.lower():
            result[0] = hwnd
            return False
        return True

    ctypes.windll.user32.EnumWindows(EnumProc(cb), 0)
    return result[0]


def _pid_exe_name(pid: int) -> str:
    try:
        import subprocess

        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=8,
        ).stdout
        # 形如: "chrome.exe","1234","Console","1","..."
        if '"' in out:
            return out.split('"')[1]
    except Exception:
        pass
    return ""


# ---------------- 窗口枚举与操作 ----------------

def list_regular_windows(own_pid: int = 0) -> List[dict]:
    """枚举可见普通顶层窗口：返回 [{hwnd, left, top, right, bottom, cls}]。"""
    out = []

    def cb(hwnd, lparam):
        if not is_window_visible(hwnd) or is_window_minimized(hwnd):
            return True
        cls = window_class_name(hwnd)
        if cls in DESKTOP_CLASSES or cls in ("Progman",):
            return True
        rect = window_rect(hwnd)
        if not rect or (rect[2] - rect[0]) < 40 or (rect[3] - rect[1]) < 40:
            return True
        pid = wt.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if own_pid and pid.value == own_pid:
            return True
        if is_fullscreen_primary(hwnd):
            return True
        out.append({"hwnd": hwnd, "left": rect[0], "top": rect[1],
                    "right": rect[2], "bottom": rect[3], "cls": cls})
        return True

    EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    ctypes.windll.user32.EnumWindows(EnumProc(cb), 0)
    return out


def nearest_window_center(cx: int, cy: int, own_pid: int = 0, max_dist: int = 520) -> Optional[dict]:
    """按中心距离找离 (cx,cy) 最近的普通窗口。"""
    best, best_d = None, 10 ** 9
    for w in list_regular_windows(own_pid):
        wcx = (w["left"] + w["right"]) // 2
        wcy = (w["top"] + w["bottom"]) // 2
        d = (wcx - cx) ** 2 + (wcy - cy) ** 2
        if d < best_d:
            best_d, best = d, w
    return best if best and best_d ** 0.5 <= max_dist else None


def window_relative_side(pet_cx: int, pet_top: int, pet_bottom: int, w: dict, margin: int = 40) -> Optional[str]:
    """判定窗口相对桌宠方位: left/right/below/above/None（不相邻）。"""
    if pet_bottom <= w["top"]:
        return "above"  # 窗口在头顶
    if pet_top >= w["bottom"]:
        return "below"  # 窗口在脚下
    if pet_cx < w["left"] and w["left"] - pet_cx <= margin:
        return "left"
    if pet_cx > w["right"] and pet_cx - w["right"] <= margin:
        return "right"
    if w["left"] <= pet_cx <= w["right"]:
        return "overlap"
    return None


def animate_move_window(hwnd: int, dx: int, dy: int, steps: int = 8, interval_ms: float = 18):
    """平滑移动窗口（dx/dy 为逻辑像素增量，内部换算物理）。"""
    try:
        import time as _t
        rect = _window_rect_physical(hwnd)
        if not rect:
            return
        scale = dpi_scale_for_hwnd(hwnd)
        start_x, start_y = rect[0], rect[1]
        dx_p = int(round(dx * scale))
        dy_p = int(round(dy * scale))
        SetWindowPos = ctypes.windll.user32.SetWindowPos
        SWP_NOSIZE = 0x1
        SWP_NOZORDER = 0x4
        SWP_NOACTIVATE = 0x10
        for i in range(1, steps + 1):
            nx = start_x + int(dx_p * i / steps)
            ny = start_y + int(dy_p * i / steps)
            SetWindowPos(hwnd, 0, nx, ny, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
            _t.sleep(interval_ms / 1000.0)
    except Exception as e:
        print(f"[windows] animate_move_window error: {e}")


def move_window_instantly(hwnd: int, x: int, y: int):
    """把窗口移动到逻辑坐标 (x,y)（内部换算物理坐标）。"""
    try:
        scale = dpi_scale_for_hwnd(hwnd)
        px = int(round(x * scale))
        py = int(round(y * scale))
        # SWP_NOSIZE 必须带上，否则 cx/cy=0 会把窗口缩成 0×0
        ctypes.windll.user32.SetWindowPos(hwnd, 0, px, py, 0, 0, 0x1 | 0x4 | 0x10)
    except Exception:
        pass

def trash_files(paths: List[str]) -> bool:
    """把文件/文件夹送进回收站（可恢复）。返回是否全部成功。"""
    if not paths:
        return True
    try:
        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [
                ("hwnd", wt.HWND),
                ("wFunc", wt.UINT),
                ("pFrom", wt.LPCWSTR),
                ("pTo", wt.LPCWSTR),
                ("fFlags", wt.USHORT),
                ("fAnyOperationsAborted", wt.BOOL),
                ("hNameMappings", wt.LPVOID),
                ("lpszProgressTitle", wt.LPCWSTR),
            ]

        FO_DELETE = 3
        FOF_ALLOWUNDO = 0x40
        FOF_NOCONFIRMATION = 0x10
        FOF_SILENT = 0x4
        FOF_NOERRORUI = 0x400
        # pFrom 需要双 \0 结尾的宽字符串
        buf = "\0".join(os.path.abspath(p) for p in paths) + "\0\0"

        op = SHFILEOPSTRUCTW()
        op.wFunc = FO_DELETE
        op.pFrom = buf
        op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI

        shell32 = ctypes.windll.shell32
        shell32.SHFileOperationW.argtypes = [ctypes.POINTER(SHFILEOPSTRUCTW)]
        shell32.SHFileOperationW.restype = ctypes.c_int
        result = shell32.SHFileOperationW(ctypes.byref(op))
        return result == 0
    except Exception as e:
        print(f"[windows] trash_files error: {e}")
        return False


# ---------------- 全局热键 ----------------

WM_HOTKEY = 0x0312
MOD_ALT = 0x1
MOD_CONTROL = 0x2
MOD_SHIFT = 0x4
MOD_NOREPEAT = 0x4000

_VK_MAP = {
    "m": ord("M"), "z": ord("Z"), "space": 0x20, "c": ord("C"),
    "t": ord("T"), "p": ord("P"),
}


def hotkey_to_vk_mods(combo: str):
    """'ctrl+alt+m' -> (vk, mods)；识别不了返回 None。"""
    parts = [p.strip().lower() for p in combo.replace(" ", "").split("+")]
    mods = 0
    key = ""
    for p in parts:
        if p in ("ctrl", "control"):
            mods |= MOD_CONTROL
        elif p == "alt":
            mods |= MOD_ALT
        elif p == "shift":
            mods |= MOD_SHIFT
        elif p in _VK_MAP:
            key = p
        else:
            return None
    if not key or not mods:
        return None
    return _VK_MAP[key], mods | MOD_NOREPEAT


def register_hotkey(hwnd: int, hotkey_id: int, combo: str) -> bool:
    vk_mods = hotkey_to_vk_mods(combo)
    if not vk_mods:
        return False
    vk, mods = vk_mods
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.RegisterHotKey.restype = ctypes.c_bool
    user32.RegisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
    ok = bool(user32.RegisterHotKey(hwnd, hotkey_id, mods, vk))
    if not ok:
        err = ctypes.get_last_error()
        print(f"[windows] RegisterHotKey failed combo={combo} err={err}")
    return ok


def unregister_hotkey(hwnd: int, hotkey_id: int):
    try:
        ctypes.windll.user32.UnregisterHotKey(hwnd, hotkey_id)
    except Exception:
        pass
