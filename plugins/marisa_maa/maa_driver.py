"""MaaCore 驱动器：把明日方舟 PC 客户端交给 MarisaBot 控制。

为什么需要这一层：MAA 官方的 Python 绑定（`MAA/Python/asst/asst.py`）**只包装了 ADB 连接**
（`AsstConnect`），而 PC 客户端走的是另一条 API —— `AsstAttachWindow`，绑定里没有。
这里是照 asst.py 的风格补上的 ctypes 绑定 + 任务队列 + 回调转事件。

实测确认的关键事实（都是踩出来的，不要凭直觉改）：

1. **截图方式当前用 PrintWindow(16)，必须提权**
   走过两轮结论：第一轮选的是 FramePool(2)（干净、不怕遮挡，但会话会失效返回全黑图，
   3000+ 字节的全黑 PNG，需要重新附着）；后来实测（data/maa_buf_probe.log）提权后
   PrintWindow(16) 完整干净且稳定，与 MAA 官方推荐一致。GDI(1) 抓到的是压在游戏上的
   别的窗口；DXGI_DesktopDup_Window(8) 被遮挡的部分会串进别的窗口。

2. **鼠标输入用 Seize(1)（SendInput 真实输入）—— 2026-10-12 起，实测逼出来的结论**
   踩坑历程：SendMessage(2)/PostMessage(4) 这类纯消息投递对方舟 PC 端（Unity/DirectX）
   无效——MAA 日志显示点击"成功"但游戏纹丝不动；
   PostMessageWithCursorPos(64) 大部分时候有效，但**偶发点击不被游戏接收**——
   实跑黑流肉鸽时卡在"确认放弃/分队确认"这类按钮上（MAA 日志匹配分 0.98、
   点击"完成"，但游戏弹窗不消失，重试 20 次全在同一"无效窗口期"内耗尽），
   两轮挂机各卡一次。Seize 用 SendInput 把真实输入发到**前台窗口**，
   点击 100% 可靠；代价是**要求游戏窗口保持前台**（否则输入会发到别的窗口！）
   且挂机期间电脑没法正常用鼠标键盘。
   → 配套：ensure_foreground() 负责拉前台；插件 heartbeat 定期复查；
   挂机前必须先拉前台，拉不到就如实报错，绝不带着错误状态开跑。

3. **AsstGetImage 取的是"上次截图"**
   没截图过就返回哨兵值 (uint64)-1。必须先 AsstAsyncScreencap 再取。
   返回的就是 PNG 编码字节（实测直接写 .png 可打开，1280x720）。

4. **AsstStart 不阻塞** —— 见 start() 的注释。

5. **销毁必须显式做** —— AsstStop 是异步的；只要实例活着，Win32 控制器就活着，
   游戏窗口和光标都会被它拿捏。close() 会等待停止完成再让 __del__ 销毁。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import json
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
INPUT_SEND_MESSAGE_WITH_CURSOR_POS = 32
INPUT_POST_MESSAGE_WITH_CURSOR_POS = 64
INPUT_SEND_MESSAGE_WITH_WINDOW_POS = 128
INPUT_POST_MESSAGE_WITH_WINDOW_POS = 256

NULL_SIZE = (1 << 64) - 1   # AsstGetNullSize()，也是"取图失败"的哨兵

# close() 等 AsstStop 生效的超时。超时后再销毁会阻塞在核心自己的收尾上，
# 但比"实例泄漏、控制器一直霸占窗口/光标"要可接受——这里只是给个上限警示。
STOP_WAIT_SEC = 8.0


class MaaDriver:
    """持有 MaaCore 实例，负责附着窗口、下发任务、把回调转成事件。"""

    def __init__(
        self,
        maa_dir: Path,
        window_title: str,
        screencap_method: int = SCREENCAP_PRINT_WINDOW,
        input_method: int = INPUT_SEIZE,
        keyboard_method: int = INPUT_SEND_MESSAGE,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self.maa_dir = Path(maa_dir)
        self.window_title = window_title
        self.screencap_method = screencap_method
        self.input_method = input_method
        # 键盘方式与鼠标分开：鼠标用 Seize（点击可靠优先），键盘保持 SendMessage
        # （游戏里极少用键盘，不需要接管）。
        self.keyboard_method = keyboard_method
        self.on_event = on_event

        self._asst: Any = None
        self._lib: Any = None
        self._ptr: Any = None
        self._hwnd: int = 0
        self._worker: Optional[threading.Thread] = None
        self._lock = threading.Lock()

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
        # 追加两层资源：与 MAA GUI 完全一致（见 _load_extra_resources 的说明），
        # 必须在附着/启动任务之前完成加载。
        self._load_extra_resources()

        # 附着窗口：这一条 asst.py 没包装，是我们自己补的
        ok = self._lib.AsstAttachWindow(
            self._ptr, ctypes.c_void_p(self._hwnd),
            self.screencap_method, self.input_method, self.keyboard_method,
        )
        if not ok:
            # 别急着甩锅给截图方式：AsstAttachWindow 返回 0 的原因有好几种，
            # MAA 会把真正的原因写进它自己的日志（D:\MAA\debug\asst.log）。
            # 实测踩过的两种：
            #   1) 未提权 → UIPI 拦掉 PrintWindow/PostMessage（日志里是 error 5）
            #   2) 窗口分辨率不是标准 16:9 → MAA 日志报
            #      "Cannot create controller proxy: Unsupported resolution"
            #      （官方要求：游戏分辨率需为 16:9，推荐 1280x720 或 1920x1080）
            raise RuntimeError(
                "附着窗口失败。请检查 D:\\MAA\\debug\\asst.log 里的真实原因，常见两种：\n"
                "  ① 未以管理员身份运行（UIPI 会拦掉窗口消息，日志里 error=5）\n"
                "  ② 游戏窗口不是标准 16:9 分辨率（官方要求 1280x720 / 1920x1080；\n"
                "     日志里会写 'Unsupported resolution'。改过系统缩放后需要重启游戏让窗口重建）"
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

    def _load_extra_resources(self) -> None:
        """加载 cache 热更层与 PC 平台差异资源 —— 与 MAA GUI 的加载顺序完全一致。

        MAA GUI（AsstProxy.LoadResource）在基础 resource 之外还会追加两层：
          ① <MAA>/resource                        —— 基础资源（Asst.load 已加载）
          ② <MAA>/cache/resource                  —— **资源热更新层**：
             新活动关卡（如 YW-4）、限时玩法任务（如肉鸽"树海"）都下发在这里。
             只加载基础层时：GUI 能跑的内容，直连插件会报 "Cannot set stage xxx"
             或找不到任务（实测踩过：YW-4 入队失败，而用户 GUI 正常）。
          ③ <MAA>/resource/platform_diff/PC/resource —— PC 端（窗口绑定）差异资源。
        AsstLoadResource 需要传「resource 的上级目录」（MaaCore 自己拼接 resource 子目录）；
        目录不存在时跳过（与 GUI 的 LoadResIfExists 行为一致）。
        """

        candidates = [
            self.maa_dir / "cache",                                  # → cache/resource
            self.maa_dir / "resource" / "platform_diff" / "PC",      # → platform_diff/PC/resource
        ]
        for parent in candidates:
            resource_dir = parent / "resource"
            if not resource_dir.is_dir():
                continue
            ok = bool(self._lib.AsstLoadResource(str(parent).encode("utf-8")))
            self._emit({
                "type": "log",
                "level": "info" if ok else "warning",
                "msg": f"额外资源加载{'成功' if ok else '**失败**'}：{resource_dir}",
            })

    def close(self) -> None:
        """停止任务并彻底销毁实例（把游戏窗口和光标交还主人）。

        **不能"丢引用靠 __del__"**：AsstStop 只发停止信号（异步生效），立刻丢引用时
        核心可能仍在跑；而只要实例活着，Win32 控制器就一直拿捏游戏窗口和光标——
        实测现象：任务卡死重试期间鼠标一直被占用、挂机结束后游戏窗口被拿住。

        顺序：
            1. AsstStop（通知核心停下）
            2. 轮询 AsstRunning 归零（最多 STOP_WAIT_SEC，正常 1-2 秒内完成）
            3. 局部引用 del → CPython 引用计数归零 → Asst.__del__ → AsstDestroy
               （走官方销毁路径，**只销毁一次**；自己再调 AsstDestroy 就是 double free）
            4. 释放 CFUNCTYPE 回调引用（销毁前必须持有，防核心回调进野指针）

        调用方注意：本函数会阻塞到任务真正停下，插件侧必须放到线程里执行。
        """

        with self._lock:
            asst = self._asst
            if asst is None:
                return
            self._asst = None
            try:
                if self._ptr:
                    self._lib.AsstStop(self._ptr)
                    deadline = time.monotonic() + STOP_WAIT_SEC
                    while time.monotonic() < deadline and self._lib.AsstRunning(self._ptr):
                        time.sleep(0.1)
                    if self._lib.AsstRunning(self._ptr):
                        # 超时不代表失败：后面 AsstDestroy 自己会等核心收尾，
                        # 但要把这件事显式记下来，方便日志里追。
                        self._emit({"type": "log", "level": "warning",
                                    "msg": f"AsstStop 等待超过 {STOP_WAIT_SEC} 秒，强制销毁实例"})
            finally:
                self._ptr = None
                self._hwnd = 0
                del asst                   # → AsstDestroy（官方析构，唯一一次）
                self._callback_wrapper = None

    # ─────────────────── 回调 → 事件 ───────────────────

    def _raw_callback(self, msg: int, details: bytes, _arg: Any) -> None:
        """MaaCore 的回调（跑在核心自己的线程上）。

        Message 枚举（见 asst/utils.py，v6.18.0）：
            0 InternalError · 1 InitFailed · 2 ConnectionInfo · 3 AllTasksCompleted ·
            4 AsyncCallInfo · 5 Destroyed
            10000 TaskChainError · 10001 TaskChainStart · 10002 TaskChainCompleted ·
            10003 TaskChainExtraInfo · 10004 TaskChainStopped
            20000 SubTaskError · 20001 SubTaskStart · 20002 SubTaskCompleted ·
            20003 SubTaskExtraInfo · 20004 SubTaskStopped · 30000 ReportRequest
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
        return bool(self._asst.start())

    def stop(self) -> bool:
        if self._asst is None:
            return False
        return bool(self._asst.stop())

    @property
    def is_running(self) -> bool:
        """是否正在跑任务（以核心的 AsstRunning 为准，不自己维护影子状态）。"""

        return self._asst is not None and bool(self._lib.AsstRunning(self._ptr))

    def connected(self) -> bool:
        return self._asst is not None and bool(self._lib.AsstConnected(self._ptr))

    # ─────────────────── 截图 ───────────────────

    def screenshot(self, window_size: tuple[int, int] | None = None) -> Optional[bytes]:
        """截一张游戏画面（PNG 字节，实测 1280x720）。

        两步：先 AsstAsyncScreencap 触发，再 AsstGetImage 取——
        后者取的是"上次截图"，没截过会返回哨兵值。
        block=1 时 AsstAsyncScreencap 会等截图完成再返回，不需要额外 sleep。
        """

        if self._asst is None or self._ptr is None:
            return None
        self._lib.AsstAsyncScreencap(self._ptr, 1)   # block=1，等截完
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

    # ─────────────────── 前台窗口（Seize 输入的前提） ───────────────────

    def ensure_foreground(self) -> bool:
        """把游戏窗口拉到前台并返回最终是否在前台。

        **Seize(1) 的输入会发给当前前台窗口**：如果游戏不在前台，点击会落到
        用户正在用的程序上——挂机跑偏事小，误点别人的窗口（浏览器/资源管理器…）
        才是真的危险。所以：
          · 挂机启动前必须先调用本方法，拉不到前台直接失败退出，不许带病开跑；
          · 插件 heartbeat 每 15 秒复查一次，用户中途切走窗口会被自动拉回。

        SetForegroundWindow 有"前台锁"限制：非前台进程直接调用会被系统拒绝
        （返回 0 且不弹窗）。经典的绕法是把当前线程的输入队列临时挂到
        前台窗口线程上（AttachThreadInput），再调 SetForegroundWindow，
        成功率最高且没有副作用。窗口被最小化时先 SW_RESTORE 还原。
        实测偶发失败（如刚关闭 UAC 弹窗、前台正在切换时），因此内部重试 3 次。
        """

        if not self._hwnd:
            return False
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        for _ in range(3):
            if user32.GetForegroundWindow() == self._hwnd:
                return True
            if user32.IsIconic(self._hwnd):
                user32.ShowWindow(self._hwnd, 9)   # SW_RESTORE
                time.sleep(0.2)

            foreground = user32.GetForegroundWindow()
            fg_thread = 0
            if foreground:
                fg_thread = int(user32.GetWindowThreadProcessId(foreground, None))
            my_thread = int(kernel32.GetCurrentThreadId())
            attached = False
            if fg_thread and fg_thread != my_thread:
                attached = bool(user32.AttachThreadInput(fg_thread, my_thread, True))
            try:
                user32.BringWindowToTop(self._hwnd)
                user32.SetForegroundWindow(self._hwnd)
            finally:
                if attached:
                    user32.AttachThreadInput(fg_thread, my_thread, False)
            if user32.GetForegroundWindow() == self._hwnd:
                return True
            time.sleep(0.6)   # 给系统一点时间稳定前台状态再重试
        return False
