import os
import sys
from pathlib import Path

import os
import sys
from pathlib import Path

# 优先绑定当前工程根目录与插件目录（隔离外部冲突）
_ROOT_DIR = Path(__file__).resolve().parent
_PLUGIN_DIR = _ROOT_DIR / "plugins" / "marisa_desktop"
sys.path = [p for p in sys.path if not ("MaiBot-1.2.4" in p and "src" in p)]
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))

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
