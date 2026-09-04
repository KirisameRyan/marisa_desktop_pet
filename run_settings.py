import sys
from PyQt6.QtWidgets import QApplication
from ui.settings_dialog import SettingsDialog

def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    dlg = SettingsDialog()
    dlg.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
