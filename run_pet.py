import sys
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QTimer
from pet.pet_window import MarisaPetWindow
from core.config_manager import APP_CONFIG

def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    pet = MarisaPetWindow()
    pet.show()

    # 首次使用引导：未检测到有效 DeepSeek Key 时自动弹出魔法配置室
    if not APP_CONFIG.is_deepseek_configured():
        QTimer.singleShot(900, pet.open_settings_dialog)

    sys.exit(app.exec())

if __name__ == "__main__":
    main()
