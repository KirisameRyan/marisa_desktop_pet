import sys
from pathlib import Path

# 优先绑定当前工程根目录与插件目录（隔离外部冲突）
_ROOT_DIR = Path(__file__).resolve().parent
_PLUGIN_DIR = _ROOT_DIR / "plugins" / "marisa_desktop"
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))

from PyQt6.QtWidgets import QApplication  # noqa: E402

from pet.pet_window import MarisaPetWindow  # noqa: E402


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    pet = MarisaPetWindow()
    pet.show()

    # 桌宠不再有本地设置面板：全部配置见 WebUI 桌宠页，
    # 入口在托盘/右键菜单的「打开 WebUI 设置」，因此启动时不主动弹窗。
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
