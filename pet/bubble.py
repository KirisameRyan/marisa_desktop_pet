from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QPoint
from PyQt6.QtGui import QPainter, QColor, QPen, QBrush, QPolygon, QFont
from PyQt6.QtWidgets import QWidget, QLabel, QVBoxLayout
from .config import CONFIG

class ComicBubble(QWidget):
    clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        # 独立悬浮工具窗口，透明无边框，置顶
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)

        self.full_text: str = ""
        self.current_display_len: int = 0
        
        # 打字机定时器
        self.type_timer = QTimer(self)
        self.type_timer.timeout.connect(self._type_step)
        
        # 自动隐藏定时器
        self.dismiss_timer = QTimer(self)
        self.dismiss_timer.setSingleShot(True)
        self.dismiss_timer.timeout.connect(self.hide_bubble)

        # 内部内容展示 Label
        self.label = QLabel(self)
        self.label.setWordWrap(True)
        font = QFont("Microsoft YaHei", 10, QFont.Weight.Medium)
        self.label.setFont(font)
        self.label.setStyleSheet("color: #222222; background: transparent;")
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 22) # 底部留白给气泡尾巴
        layout.addWidget(self.label)
        self.setLayout(layout)

        self.max_bubble_width = 280
        self.min_bubble_width = 110

    def say(self, text: str, auto_dismiss: bool = True):
        self.full_text = text.strip()
        self.current_display_len = 0
        self.label.setText("")
        self.dismiss_timer.stop()
        
        # 根据文字大致估算尺寸
        self.adjust_size(self.full_text)
        self.show()
        
        # 启动打字机
        self.type_timer.start(CONFIG.typewriter_speed_ms)

    def adjust_size(self, sample_text: str):
        self.label.setFixedWidth(min(self.max_bubble_width - 32, max(self.min_bubble_width, len(sample_text) * 16)))
        self.label.setText(sample_text)
        self.label.adjustSize()
        w = max(self.min_bubble_width, self.label.width() + 32)
        h = max(55, self.label.height() + 34)
        self.resize(w, h)
        self.label.setText("")

    def _type_step(self):
        if self.current_display_len < len(self.full_text):
            self.current_display_len += 1
            self.label.setText(self.full_text[:self.current_display_len])
        else:
            self.type_timer.stop()
            self.dismiss_timer.start(int(CONFIG.bubble_auto_dismiss_s * 1000))

    def hide_bubble(self):
        self.type_timer.stop()
        self.dismiss_timer.stop()
        self.hide()

    def mousePressEvent(self, event):
        # 如果还在打字，点击立即全部展开；如果已经打完，点击关闭
        if self.type_timer.isActive():
            self.type_timer.stop()
            self.current_display_len = len(self.full_text)
            self.label.setText(self.full_text)
            self.dismiss_timer.start(int(CONFIG.bubble_auto_dismiss_s * 1000))
        else:
            self.hide_bubble()
        self.clicked.emit()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # 气泡背景框
        rect_w = self.width() - 4
        rect_h = self.height() - 16
        pen = QPen(QColor("#2d2d2d"), 2)
        brush = QBrush(QColor("#ffffff"))
        painter.setPen(pen)
        painter.setBrush(brush)

        # 绘制主圆角矩形
        painter.drawRoundedRect(2, 2, rect_w, rect_h, 12, 12)

        # 绘制底部指向小角（漫画气泡尾巴）
        tail = QPolygon([
            QPoint(self.width() // 2 - 8, rect_h + 1),
            QPoint(self.width() // 2 + 8, rect_h + 1),
            QPoint(self.width() // 2, self.height() - 2)
        ])
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPolygon(tail)
        
        # 补齐尾巴两侧黑线
        painter.setPen(pen)
        painter.drawLine(self.width() // 2 - 8, rect_h + 1, self.width() // 2, self.height() - 2)
        painter.drawLine(self.width() // 2 + 8, rect_h + 1, self.width() // 2, self.height() - 2)
