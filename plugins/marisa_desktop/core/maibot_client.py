# -*- coding: utf-8 -*-
"""MaiBot 桌面客户端（P2）：Qt 桌宠与 MaiBot marisa-desktop 网关的长连接。

设计：
- 专用守护线程负责 WS 连接与重连；
- Qt 侧通过信号接收魔理沙回复/连接状态；
- 聊天文本经线程安全队列上行，保持 UI 不阻塞。
"""
import json
import queue
import threading

from PyQt6.QtCore import QThread, pyqtSignal

try:
    from websockets.sync.client import connect as ws_connect

    WS_AVAILABLE = True
except Exception:  # pragma: no cover
    WS_AVAILABLE = False


class MaiBotClientWorker(QThread):
    """后台连接线程：接收魔理沙回复/执行指令，发送桌宠消息与结果。"""

    reply_received = pyqtSignal(str)          # 魔理沙文本回复
    action_requested = pyqtSignal(object)     # 大脑下发执行动作 dict
    status_changed = pyqtSignal(str)          # 连接状态文本
    connected = pyqtSignal(bool)
    confirm_requested = pyqtSignal(object)    # 清理方案确认弹窗请求
    confirm_closed = pyqtSignal(str)          # 指定 req_id 的弹窗应关闭

    def __init__(self, url: str = "ws://127.0.0.1:8400", parent=None):
        super().__init__(parent)
        self.url = url
        self._send_queue: "queue.Queue[dict]" = queue.Queue()
        self._stop = threading.Event()
        self._conn = None

    def send_text(self, text: str) -> None:
        """上行聊天消息（线程安全）。"""
        self._send_queue.put({"type": "chat", "text": text})

    def send_voice(self, audio_base64: str, seconds: float = 0.0) -> None:
        """上行语音：交宿主识别后按普通消息注入主链。"""
        self._send_queue.put(
            {
                "type": "voice",
                "audio_base64": audio_base64,
                "format": "wav",
                "seconds": float(seconds),
            }
        )

    def send_favor(self, delta: int = 1, reason: str = "") -> None:
        """上行互动事件，由宿主统一记账好感度。"""
        self._send_queue.put({"type": "favor", "delta": int(delta), "reason": reason})

    def send_action_result(self, req_id: str, ok: bool, text: str, image_b64: str = "") -> None:
        """回传执行结果（线程安全）。"""
        frame: dict = {"type": "action_result", "req_id": req_id, "ok": ok, "text": text}
        if image_b64:
            frame["image_b64"] = image_b64
        self._send_queue.put(frame)

    def send_confirm_result(
        self,
        req_id: str,
        approved: bool,
        selected: list,
        permanent: bool = False,
    ) -> None:
        """回传清理方案的勾选确认结果（线程安全）。"""
        self._send_queue.put(
            {
                "type": "confirm_result",
                "req_id": str(req_id or ""),
                "approved": bool(approved),
                "selected": [int(item) for item in selected],
                "permanent": bool(permanent),
            }
        )

    def stop(self) -> None:
        """请求停止。"""
        self._stop.set()
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass

    def run(self) -> None:
        """连接循环（自动重连）。"""
        self._reconnect_loop()

    def _reconnect_loop(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self.status_changed.emit("正在连接魔理沙大脑 (MaiBot)...")
                with ws_connect(self.url, open_timeout=8, close_timeout=4) as conn:
                    self._conn = conn
                    self.connected.emit(True)
                    self.status_changed.emit("已连接 MaiBot 大脑 ✓")
                    backoff = 1.0
                    self._handle_session(conn)
            except Exception as exc:
                if self._stop.is_set():
                    break
                self.connected.emit(False)
                self.status_changed.emit(f"大脑连接断开（{str(exc)[:40]}），重试中...")
                self._sleep(backoff)
                backoff = min(backoff * 2, 15.0)

    def _handle_session(self, conn) -> None:
        """单次会话：读下行 + 写上行（小超时轮询，避免误判断连）。"""
        while not self._stop.is_set():
            # 上行：把队列里的帧发出去
            while not self._send_queue.empty():
                try:
                    frame = self._send_queue.get_nowait()
                    if frame:
                        conn.send(json.dumps(frame, ensure_ascii=False))
                except Exception:
                    return
            # 下行：短超时读一条；超时属正常，循环继续
            try:
                raw = conn.recv(timeout=0.3)
            except TimeoutError:
                continue
            except Exception:
                return
            if raw:
                self._dispatch(raw)

    def _dispatch(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except Exception:
            return
        kind = data.get("type")
        if kind == "bot_message":
            text = str(data.get("text") or "")
            if text:
                self.reply_received.emit(text)
        elif kind == "action":
            self.action_requested.emit(data)
        elif kind == "confirm_request":
            self.confirm_requested.emit(data)
        elif kind == "confirm_closed":
            self.confirm_closed.emit(str(data.get("req_id") or ""))

    def _sleep(self, sec: float) -> None:
        self._stop.wait(sec)


class MaiBotClient:
    """桌宠侧的 MaiBot 客户端门面（Qt 线程内使用）。"""

    def __init__(self, url: str = "ws://127.0.0.1:8400"):
        self.url = url
        self.worker = None

    @property
    def is_connected(self) -> bool:
        return bool(self.worker and self.worker.isRunning())

    def start(
        self,
        on_reply=None,
        on_status=None,
        on_connected=None,
        on_action=None,
        on_confirm=None,
        on_confirm_closed=None,
    ) -> None:
        """启动连接线程（幂等）。"""
        if self.worker and self.worker.isRunning():
            return
        if not WS_AVAILABLE:
            if on_status:
                on_status("环境缺少 websockets 库，无法连接 MaiBot")
            return
        self.worker = MaiBotClientWorker(self.url)
        if on_reply:
            self.worker.reply_received.connect(on_reply)
        if on_status:
            self.worker.status_changed.connect(on_status)
        if on_connected:
            self.worker.connected.connect(on_connected)
        if on_action:
            self.worker.action_requested.connect(on_action)
        if on_confirm:
            self.worker.confirm_requested.connect(on_confirm)
        if on_confirm_closed:
            self.worker.confirm_closed.connect(on_confirm_closed)
        self.worker.start()

    def send(self, text: str) -> None:
        if self.worker and self.worker.isRunning():
            self.worker.send_text(text)

    def send_voice(self, audio_base64: str, seconds: float = 0.0) -> None:
        if self.worker and self.worker.isRunning():
            self.worker.send_voice(audio_base64, seconds)

    def send_favor(self, delta: int = 1, reason: str = "") -> None:
        if self.worker and self.worker.isRunning():
            self.worker.send_favor(delta, reason)

    def send_action_result(self, req_id: str, ok: bool, text: str, image_b64: str = "") -> None:
        if self.worker and self.worker.isRunning():
            self.worker.send_action_result(req_id, ok, text, image_b64)

    def send_confirm_result(
        self,
        req_id: str,
        approved: bool,
        selected: list,
        permanent: bool = False,
    ) -> None:
        """上行清理方案勾选结果。"""
        if self.worker and self.worker.isRunning():
            self.worker.send_confirm_result(req_id, approved, selected, permanent)

    def stop(self) -> None:
        if self.worker:
            self.worker.stop()
            self.worker.wait(3000)
            self.worker = None
