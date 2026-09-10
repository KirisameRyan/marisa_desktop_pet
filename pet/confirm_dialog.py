# -*- coding: utf-8 -*-
"""清理方案确认弹窗：条目勾选 + 合计 + 倒计时，结果回传宿主。

与 QQ 端的卡片确认共享同一份待确认方案：弹窗确认/取消都会立即同步到宿主，
弹窗倒计时结束只关窗（QQ 端仍可在方案有效期内确认）。
"""
from __future__ import annotations

from typing import Any, Dict, List

from PyQt6.QtCore import Qt, QPoint, QTimer, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)


class CleanupConfirmDialog(QWidget):
    """魔理沙的清理方案确认框。"""

    confirmed = pyqtSignal(list, bool)   # (选中的条目编号, 是否彻底删除)
    cancelled = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self._req_id: str = ""
        self._plan_code: int = 0
        self._checks: Dict[int, QCheckBox] = {}
        self._cache_ids: List[int] = []
        self._remaining: int = 0
        self._allow_permanent: bool = False

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

        self._build_ui()

    # ---------------- UI ----------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self.frame = QFrame(self)
        self.frame.setObjectName("card")
        outer.addWidget(self.frame)

        layout = QVBoxLayout(self.frame)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)

        self.title_label = QLabel("清理方案", self.frame)
        title_font = QFont("Microsoft YaHei", 12)
        title_font.setBold(True)
        self.title_label.setFont(title_font)

        self.countdown_label = QLabel("", self.frame)
        self.countdown_label.setObjectName("countdown")

        header = QHBoxLayout()
        header.addWidget(self.title_label)
        header.addStretch(1)
        header.addWidget(self.countdown_label)
        layout.addLayout(header)

        hint = QLabel("勾选要执行的项，取消勾选的会原样保留。", self.frame)
        hint.setObjectName("hint")
        layout.addWidget(hint)

        self.scroll = QScrollArea(self.frame)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._items_host = QWidget()
        self._items_layout = QVBoxLayout(self._items_host)
        self._items_layout.setContentsMargins(0, 0, 6, 0)
        self._items_layout.setSpacing(6)
        self._items_layout.addStretch(1)
        self.scroll.setWidget(self._items_host)
        layout.addWidget(self.scroll, 1)

        self.permanent_check = QCheckBox("彻底删除（不经回收站，无法恢复）", self.frame)
        self.permanent_check.setObjectName("permanent")
        self.permanent_check.stateChanged.connect(lambda _state: self._refresh_total())
        self.permanent_check.setVisible(False)
        layout.addWidget(self.permanent_check)

        self.total_label = QLabel("", self.frame)
        self.total_label.setObjectName("total")
        layout.addWidget(self.total_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_btn = QPushButton("取消", self.frame)
        self.cancel_btn.clicked.connect(self._on_cancel)
        self.confirm_btn = QPushButton("执行清理", self.frame)
        self.confirm_btn.clicked.connect(self._on_confirm)
        buttons.addWidget(self.cancel_btn)
        buttons.addWidget(self.confirm_btn)
        layout.addLayout(buttons)

        self.setStyleSheet(
            """
            QFrame#card {
                background-color: rgba(255, 253, 245, 0.98);
                border: 2px solid #333333;
                border-radius: 12px;
            }
            QLabel { color: #2b2418; font-family: "Microsoft YaHei"; font-size: 12px; }
            QLabel#countdown { color: #a97c00; font-size: 12px; }
            QLabel#hint { color: #8a7c5f; font-size: 11px; }
            QLabel#total { color: #b07200; font-size: 12px; font-weight: bold; }
            QCheckBox { color: #2b2418; font-family: "Microsoft YaHei"; font-size: 12px; spacing: 6px; }
            QCheckBox#permanent { color: #a33; font-size: 11px; }
            QPushButton {
                background-color: #ffcc00;
                border: 1px solid #d4a000;
                border-radius: 6px;
                padding: 5px 16px;
                font-family: "Microsoft YaHei";
                font-size: 12px;
                font-weight: bold;
                color: #333333;
            }
            QPushButton:hover { background-color: #ffe066; }
            QPushButton:pressed { background-color: #e6b800; }
            QScrollBar:vertical { width: 8px; background: transparent; }
            QScrollBar::handle:vertical { background: #e0c46a; border-radius: 4px; }
            """
        )
        self.resize(430, 520)

    # ---------------- 数据填充 ----------------

    def show_request(self, payload: Dict[str, Any], anchor: QPoint | None = None) -> None:
        """根据宿主下发的 confirm_request 帧展示弹窗。"""

        self._req_id = str(payload.get("req_id") or "")
        try:
            self._plan_code = int(payload.get("plan_code") or 0)
        except (TypeError, ValueError):
            self._plan_code = 0
        try:
            self._remaining = max(0, int(payload.get("expires_in") or 0))
        except (TypeError, ValueError):
            self._remaining = 0
        self._allow_permanent = bool(payload.get("allow_permanent"))
        self._cache_ids = []
        self.title_label.setText(f"🧹 {payload.get('title') or f'清理方案 P{self._plan_code}'}")
        self.permanent_check.setChecked(False)

        # 清空旧条目
        while self._items_layout.count() > 1:
            item = self._items_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._checks.clear()

        items = payload.get("items") if isinstance(payload.get("items"), list) else []
        for entry in items:
            try:
                item_id = int(entry.get("id") or 0)
            except (TypeError, ValueError):
                continue
            if item_id <= 0:
                continue
            row = QWidget(self._items_host)
            row_layout = QVBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(1)

            check = QCheckBox(f"#{item_id} {entry.get('label') or ''} · {entry.get('size_text') or ''}", row)
            check.setChecked(True)
            check.stateChanged.connect(lambda _state: self._refresh_total())
            row_layout.addWidget(check)

            reason = QLabel(str(entry.get("reason") or ""), row)
            reason.setObjectName("hint")
            reason.setWordWrap(True)
            row_layout.addWidget(reason)

            self._items_layout.insertWidget(self._items_layout.count() - 1, row)
            self._checks[item_id] = check
            if str(entry.get("kind") or "") == "clean_dir":
                self._cache_ids.append(item_id)

        show_permanent = self._allow_permanent and bool(self._cache_ids)
        self.permanent_check.setVisible(show_permanent)
        self._refresh_total()
        self._refresh_countdown()

        if anchor is not None:
            self.move(anchor)
        self._timer.start(1000)
        self.show()
        self.raise_()
        self.activateWindow()

    @property
    def req_id(self) -> str:
        """当前弹窗对应的确认请求编号。"""

        return self._req_id

    def close_request(self, req_id: str = "") -> None:
        """宿主通知关闭（QQ 端已确认/取消）；req_id 为空或匹配时关闭。"""

        if req_id and req_id != self._req_id:
            return
        self._timer.stop()
        self.hide()

    # ---------------- 交互 ----------------

    def _selected_ids(self) -> List[int]:
        return [item_id for item_id, check in self._checks.items() if check.isChecked()]

    def _refresh_total(self) -> None:
        selected = len(self._selected_ids())
        permanent = self.permanent_check.isVisible() and self.permanent_check.isChecked()
        mode = "彻底删除" if permanent else "送回收站"
        self.total_label.setText(f"已选 {selected}/{len(self._checks)} 项 · 缓存类执行方式：{mode}")

    def _refresh_countdown(self) -> None:
        minutes, seconds = divmod(max(0, self._remaining), 60)
        self.countdown_label.setText(f"剩余 {minutes:02d}:{seconds:02d}")

    def _tick(self) -> None:
        self._remaining -= 1
        if self._remaining <= 0:
            # 倒计时结束只关窗：方案在有效期内仍可由 QQ 端确认
            self._timer.stop()
            self.hide()
            return
        self._refresh_countdown()

    def _on_confirm(self) -> None:
        selected = self._selected_ids()
        if not selected:
            self.total_label.setText("至少勾选一项再执行だぜ！")
            return
        permanent = self.permanent_check.isVisible() and self.permanent_check.isChecked()
        self._timer.stop()
        self.hide()
        self.confirmed.emit(selected, permanent)

    def _on_cancel(self) -> None:
        self._timer.stop()
        self.hide()
        self.cancelled.emit()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self._on_cancel()
        else:
            super().keyPressEvent(event)
