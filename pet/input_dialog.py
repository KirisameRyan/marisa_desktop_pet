from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QWidget, QLineEdit, QPushButton, QHBoxLayout

class ChatInputDialog(QWidget):
    submitted = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(6)

        self.input_edit = QLineEdit(self)
        self.input_edit.setPlaceholderText("跟魔理沙说点什么吧... (回车发送)")
        font = QFont("Microsoft YaHei", 9)
        self.input_edit.setFont(font)
        self.input_edit.returnPressed.connect(self._on_submit)
        
        self.send_btn = QPushButton("发送", self)
        self.send_btn.setFont(font)
        self.send_btn.clicked.connect(self._on_submit)

        layout.addWidget(self.input_edit)
        layout.addWidget(self.send_btn)
        self.setLayout(layout)

        self.setStyleSheet("""
            QWidget {
                background-color: rgba(255, 255, 255, 0.95);
                border: 2px solid #333333;
                border-radius: 10px;
            }
            QLineEdit {
                border: 1px solid #cccccc;
                border-radius: 6px;
                padding: 4px 8px;
                background: #ffffff;
                color: #222222;
            }
            QLineEdit:focus {
                border: 1px solid #ffb100;
            }
            QPushButton {
                background-color: #ffcc00;
                border: 1px solid #d4a000;
                border-radius: 6px;
                padding: 4px 12px;
                font-weight: bold;
                color: #333333;
            }
            QPushButton:hover {
                background-color: #ffe066;
            }
            QPushButton:pressed {
                background-color: #e6b800;
            }
        """)
        self.resize(320, 46)

    def _on_submit(self):
        text = self.input_edit.text().strip()
        if text:
            self.input_edit.clear()
            self.hide()
            self.submitted.emit(text)

    def show_at(self, pos):
        self.move(pos)
        self.show()
        self.input_edit.setFocus()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
        else:
            super().keyPressEvent(event)
