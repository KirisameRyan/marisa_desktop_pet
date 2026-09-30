import importlib.util
import sys
from pathlib import Path

# 桌宠输出在宿主自动拉起时会被重定向到文件/管道，Windows 下默认按 GBK 编码，
# 状态文案里的 ✓ 等字符会抛 UnicodeEncodeError；Qt 槽里未处理的异常会让进程直接退出。
# 固定用 UTF-8 写标准输出，两种启动方式（手动 / 宿主拉起）行为一致。
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

# 优先绑定当前工程根目录与插件目录（隔离外部冲突）
_ROOT_DIR = Path(__file__).resolve().parent
_PLUGIN_DIR = _ROOT_DIR / "plugins" / "marisa_desktop"
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))

# 宿主 .venv 的 editable 安装会把项目 src 目录放进 sys.path，其中 src/core 是常规包，
# 会在导入解析时抢走插件目录里的 core（同为常规包时就按路径顺序决胜负的不确定性太大）。
# 这里按文件路径显式把插件的 core 注册进 sys.modules，保证 from core.xxx 只走插件实现。
_CORE_DIR = _PLUGIN_DIR / "core"
if "core" not in sys.modules:
    _core_spec = importlib.util.spec_from_file_location(
        "core", _CORE_DIR / "__init__.py", submodule_search_locations=[str(_CORE_DIR)]
    )
    if _core_spec is None or _core_spec.loader is None:
        raise ImportError(f"无法加载桌宠 core 包: {_CORE_DIR}")
    _core_module = importlib.util.module_from_spec(_core_spec)
    sys.modules["core"] = _core_module
    _core_spec.loader.exec_module(_core_module)

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
