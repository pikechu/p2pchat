"""
Beam — P2P Chat GUI client

Usage:
  python gui_client.py
  python gui_client.py --server wss://your-app.railway.app
  python gui_client.py --name Alice --theme dark
"""

import argparse
import json
import os
import sys

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QIcon
from transport_security import validate_server_url


def _resource(relative: str) -> str:
    """Resolve path for both dev and PyInstaller packaged builds."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative)


def load_client_config() -> dict:
    path = _resource("beam_config.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_default_server_url() -> str:
    config = load_client_config()
    return validate_server_url(config.get("server_url") or "wss://106.55.8.122:8765")

# Windows: force UTF-8 so chat messages with any charset render correctly
# sys.stdout/stderr are None in --windowed (no console) packaged builds
if sys.platform == "win32":
    if sys.stdout:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if sys.stderr:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def main():
    client_config = load_client_config()
    parser = argparse.ArgumentParser(description="Beam P2P Chat — GUI client")
    parser.add_argument("--server", default=load_default_server_url(),
                        help="Server WebSocket URL")
    parser.add_argument("--name",   default="",
                        help="Your display name")
    parser.add_argument("--theme",  default="light", choices=["light", "dark"],
                        help="UI theme")
    args = parser.parse_args()
    try:
        args.server = validate_server_url(args.server)
    except ValueError as exc:
        parser.error(str(exc))

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
        allow_custom_server=bool(client_config.get("allow_custom_server", True)),
    )
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
