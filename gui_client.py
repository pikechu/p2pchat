"""
Beam — P2P Chat GUI client

Usage:
  python gui_client.py
  python gui_client.py --server wss://your-app.railway.app
  python gui_client.py --name Alice --theme dark
"""

import argparse
import os
import sys

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QIcon


def _resource(relative: str) -> str:
    """Resolve path for both dev and PyInstaller packaged builds."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative)

# Windows: force UTF-8 so chat messages with any charset render correctly
# sys.stdout/stderr are None in --windowed (no console) packaged builds
if sys.platform == "win32":
    if sys.stdout:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if sys.stderr:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def main():
    parser = argparse.ArgumentParser(description="Beam P2P Chat — GUI client")
    parser.add_argument("--server", default="ws://106.55.8.122:8765",
                        help="Server WebSocket URL")
    parser.add_argument("--name",   default="",
                        help="Your display name")
    parser.add_argument("--theme",  default="light", choices=["light", "dark"],
                        help="UI theme")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    app.setStyle("Fusion")   # ensures QSS fully applies on all platforms incl. Windows
    app.setApplicationName("Beam — P2P Chat")

    icon_path = _resource(os.path.join("assets", "icon.png"))
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    # Prompt for username if not supplied
    username = args.name
    if not username:
        from PyQt6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(
            None, "Beam", "Enter your display name:",
        )
        if not ok or not name.strip():
            sys.exit(0)
        username = name.strip()

    from gui.window import MainWindow
    win = MainWindow(
        server_url=args.server,
        username=username,
        theme=args.theme,
    )
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
