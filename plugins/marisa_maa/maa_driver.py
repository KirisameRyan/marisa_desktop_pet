"""MaaCore 驱动器：把明日方舟 PC 客户端交给 MarisaBot 控制。

为什么需要这一层：MAA 官方的 Python 绑定（`MAA/Python/asst/asst.py`）**只包装了 ADB 连接**
（`AsstConnect`），而 PC 客户端走的是另一条 API —— `AsstAttachWindow`，绑定里没有。
这里是照 asst.py 的风格补上的 ctypes 绑定 + 任务队列 + 回调转事件。

实测确认的关键事实（都是踩出来的，不要凭直觉改）：

1. **截图方式必须用 FramePool(2)**
   实测对比：GDI(1) 抓到的是压在游戏上面的别的窗口；DXGI_DesktopDup_Window(8) 抓桌面区域，
   被遮挡的部分会串进别的窗口；PrintWindow(16) 被游戏拒绝（error 5，附着直接失败）。
   FramePool（Windows.Graphics.Capture）完整干净，而且**不怕窗口被遮挡**。

2. **输入方式必须用 Seize(1)**
   实测：用 SendMessage(2) 时 MAA 日志显示点击任务全部"成功"（MatchTemplate + ClickSelf
   + SubTaskCompleted），但游戏纹丝不动——因为方舟 PC 端是 Unity/DirectX，
   **不读 WM_LBUTTONDOWN 这类窗口消息**，只认真实输入。
   代价：Seize 会接管鼠标键盘，挂机期间电脑不能正常操作。

3. **AsstGetImage 取的是"上次截图"**
   没截图过就返回哨兵值 (uint64)-1。必须先 AsstAsyncScreencap 再取。

4. **AsstStart 阻塞到全部任务跑完** → 必须放独立线程，不能占事件循环。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import json
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# Win32 截图方式（见 MAA include/AsstCaller.h 的 AsstWin32ScreencapMethodEnum）
SCREENCAP_NONE = 0
SCREENCAP_GDI = 1
SCREENCAP_FRAME_POOL = 2
SCREENCAP_DXGI_DESKTOP_DUP = 4
SCREENCAP_DXGI_DESKTOP_DUP_WINDOW = 8
SCREENCAP_PRINT_WINDOW = 16
SCREENCAP_SCREEN_DC = 32

# Win32 输入方式（AsstWin32InputMethodEnum）
INPUT_NONE = 0
INPUT_SEIZE = 1
INPUT_SEND_MESSAGE = 2
INPUT_POST_MESSAGE = 4

NULL_SIZE = (1 << 64) - 1   # AsstGetNullSize()，也是"取图失败"的哨兵


class MaaDriver:
    """持有 MaaCore 实例，负责附着窗口、下发任务、把回调转成事件。"""

    def __init__(
        self,
        maa_dir: Path,
        window_title: str,
        screencap_method: int = SCREENCAP_FRAME_POOL,
        input_method: int = INPUT_SEIZE,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self.maa_dir = Path(maa_dir)
        self.window_title = window_title
        self.screencap_method = screencap_method
        self.input_method = input_method
        self.on_event = on_event

        self._asst: Any = None
        self._lib: Any = None
        self._ptr: Any = None
        self._hwnd: int = 0
        self._worker: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._running = False

    # ─────────────────── 生命周期 ───────────────────

    def load(self) -> str:
        """加载 MaaCore 与资源，返回核心版本。"""

        python_dir = self.maa_dir / "Python"
        if not python_dir.exists():
            raise FileNotFoundError(f"MAA 的 Python 绑定目录不存在：{python_dir}")
        if str(python_dir) not in sys.path:
            sys.path.insert(0, str(python_dir))

        from asst.asst import Asst   # 延迟导入：插件加载时不该拖慢启动

        user32 = ctypes.windll.user32
        self._hwnd = int(user32.FindWindowW(None, self.window_title))
        if not self._hwnd:
            raise RuntimeError(f"没找到窗口「{self.window_title}」——游戏客户端没开？")

        if not Asst.load(path=self.maa_dir):
            raise RuntimeError(f"MaaCore 资源加载失败：{self.maa_dir}")

        # ctypes 回调必须是 CFUNCTYPE 实例（asst.py 的示例用 @Asst.CallBackType 装饰），
        # 直接把普通方法传进 AsstCreateEx 会失败。而且**必须持有引用**——
        # CFUNCTYPE 对象被 GC 回收后核心再回调就是野指针（直接崩进程）。
        self._callback_wrapper = Asst.CallBackType(self._raw_callback)
        self._asst = Asst(callback=self._callback_wrapper)
        self._lib = Asst._Asst__lib          # ctypes 库句柄（私有属性，绑定里没有公开访问器）
        self._ptr = self._asst._Asst__ptr    # AsstHandle
        self._bind()

        # 附着窗口：这一条 asst.py 没包装，是我们自己补的
        ok = self._lib.AsstAttachWindow(
            self._ptr, ctypes.c_void_p(self._hwnd),
            self.screencap_method, self.input_method, self.input_method,
        )
        if not ok:
            raise RuntimeError(
                f"附着窗口失败（截图方式={self.screencap_method}）——"
                "PrintWindow 会被游戏拒绝(error 5)，建议用 FramePool=2"
            )
        version = self._asst.get_version()
        self._emit({"type": "connected", "version": version, "hwnd": self._hwnd})
        return version

    def _bind(self) -> None:
        """给 ctypes 补上 asst.py 没包装的 Win32 接口。"""

        lib = self._lib
        lib.AsstAttachWindow.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint64, ctypes.c_uint64, ctypes.c_uint64
        ]
        lib.AsstAttachWindow.restype = ctypes.c_uint8
        lib.AsstAsyncScreencap.argtypes = [ctypes.c_void_p, ctypes.c_uint8]
        lib.AsstAsyncScreencap.restype = ctypes.c_int32
        lib.AsstGetImage.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint64]
        lib.AsstGetImage.restype = ctypes.c_uint64
        lib.AsstConnected.argtypes = [ctypes.c_void_p]
        lib.AsstConnected.restype = ctypes.c_uint8
        lib.AsstRunning.argtypes = [ctypes.c_void_p]
        lib.AsstRunning.restype = ctypes.c_uint8
        lib.AsstStop.argtypes = [ctypes.c_void_p]
        lib.AsstStop.restype = ctypes.c_uint8

    def close(self) -> None:
        """停止任务并释放实例。"""

        with self._lock:
            if self._asst is None:
                return
            try:
                if self._ptr:
                    self._lib.AsstStop(self._ptr)
            finally:
                self._asst = None
                self._ptr = None
                self._hwnd = 0

    # ─────────────────── 回调 → 事件 ───────────────────

    def _raw_callback(self, msg: int, details: bytes, _arg: Any) -> None:
        """MaaCore 的回调（跑在核心自己的线程上）。

        Message 枚举（见 asst/utils.py）：
            1 InitFailed · 2 ConnectionInfo · 3 AllTasksCompleted · 10000 TaskChainStart…
            20000 SubTaskStart… · 30000 ReportRequest
        这里只做"翻译成事件"，不做任何耗时/阻塞的事——回调里堵住会拖慢整个核心。
        """

        try:
            data = json.loads(details.decode("utf-8")) if details else {}
        except Exception:
            data = {}
        self._emit({"type": "maa_message", "msg": msg, "data": data})

    def _emit(self, event: Dict[str, Any]) -> None:
        if self.on_event is not None:
            try:
                self.on_event(event)
            except Exception:
                pass   # 消费端的问题不该影响核心回调线程

    # ─────────────────── 任务 ───────────────────

    def append_task(self, type_name: str, params: Dict[str, Any] | None = None) -> int:
        """排队一个任务，返回 task id。"""

        if self._asst is None:
            raise RuntimeError("还没加载：先 load()")
        return int(self._asst.append_task(type_name, params or {}))

    def start(self) -> bool:
        """启动任务队列。

        **注意：AsstStart 不阻塞。** MaaCore 的实现是
            if (running()) return false;
            m_thread = std::thread(&Assistant::working_proc, this);
            return true;                    // ← 立即返回
        所以不能靠"这个调用返回了"判断任务跑完，必须轮询 `is_running`（AsstRunning）
        或等 AllTasksCompleted 回调。

        踩过：把它当成阻塞调用 → 线程秒退 → 外层以为干完了就退出进程 →
        AsstStop 把刚起的任务掐死，表现为"入了队、返回 True、什么都没发生"。
        """

        if self._asst is None:
            raise RuntimeError("还没加载：先 load()")
        self._running = True
        return bool(self._asst.start())

    def stop(self) -> bool:
        if self._asst is None:
            return False
        return bool(self._asst.stop())

    @property
    def is_running(self) -> bool:
        return bool(self._running) or (
            self._asst is not None and bool(self._lib.AsstRunning(self._ptr))
        )

    def connected(self) -> bool:
        return self._asst is not None and bool(self._lib.AsstConnected(self._ptr))

    # ─────────────────── 截图 ───────────────────

    def screenshot(self, window_size: tuple[int, int] | None = None) -> Optional[bytes]:
        """截一张游戏画面（PNG 字节）。

        两步：先 AsstAsyncScreencap 触发，再 AsstGetImage 取——
        后者取的是"上次截图"，没截过会返回哨兵值。
        """

        if self._asst is None or self._ptr is None:
            return None
        self._lib.AsstAsyncScreencap(self._ptr, 1)   # block=1，等截完
        time.sleep(0.15)
        width, height = window_size or self.client_size()
        size = max(width, 1) * max(height, 1) * 4
        buffer = ctypes.create_string_buffer(size)
        got = int(self._lib.AsstGetImage(self._ptr, buffer, size))
        if not got or got >= NULL_SIZE:
            return None
        return buffer.raw[:got]

    def client_size(self) -> tuple[int, int]:
        """游戏窗口客户区尺寸（用于给截图分配缓冲）。"""

        rect = wintypes.RECT()
        ctypes.windll.user32.GetClientRect(self._hwnd, ctypes.byref(rect))
        return (max(rect.right - rect.left, 1), max(rect.bottom - rect.top, 1))
