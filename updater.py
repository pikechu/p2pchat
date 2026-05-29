"""
Self-update logic for BeamChat.

Flow:
  1. check_update()  →  (new_version_str, download_url) or (None, None)
  2. UpdateDownloader(url)  →  QThread that emits progress + finished(tmp_path)
  3. apply_update(tmp_path) →  launch swap batch file + quit
"""

import json
import pathlib
import subprocess
import sys
import urllib.request

from PyQt6.QtCore import QThread, pyqtSignal

from version import __version__

GITHUB_API = "https://api.github.com/repos/pikechu/p2pchat/releases/latest"
ASSET_NAME = "BeamChat.exe"


# ── Version comparison ────────────────────────────────────────────────────────

def _parse(v: str) -> tuple:
    try:
        return tuple(int(x) for x in v.strip().lstrip("v").split("."))
    except Exception:
        return (0,)


def is_newer(remote: str, local: str = __version__) -> bool:
    return _parse(remote) > _parse(local)


# ── GitHub release check ──────────────────────────────────────────────────────

def check_update(timeout: int = 10) -> tuple[str | None, str | None]:
    """
    Return (version_string, download_url) if a newer release exists,
    otherwise (None, None).
    """
    try:
        req = urllib.request.Request(
            GITHUB_API,
            headers={"User-Agent": f"BeamChat/{__version__}"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        tag = data.get("tag_name", "")
        if not is_newer(tag):
            return None, None
        for asset in data.get("assets", []):
            if asset.get("name", "").lower() == ASSET_NAME.lower():
                return tag.lstrip("v"), asset["browser_download_url"]
    except Exception:
        pass
    return None, None


# ── Download thread ───────────────────────────────────────────────────────────

class UpdateDownloader(QThread):
    progress  = pyqtSignal(int)        # 0–100
    finished  = pyqtSignal(str)        # tmp EXE path on success
    failed    = pyqtSignal(str)        # error message

    def __init__(self, url: str, parent=None):
        super().__init__(parent)
        self._url = url

    def run(self):
        try:
            current = pathlib.Path(sys.executable)
            tmp = current.with_name("_BeamChat_update.exe")
            req = urllib.request.Request(
                self._url,
                headers={"User-Agent": f"BeamChat/{__version__}"},
            )
            with urllib.request.urlopen(req) as resp:
                total = int(resp.headers.get("Content-Length", 0))
                downloaded = 0
                with open(tmp, "wb") as f:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            self.progress.emit(int(downloaded * 100 / total))
            self.finished.emit(str(tmp))
        except Exception as exc:
            self.failed.emit(str(exc))


# ── Apply update ──────────────────────────────────────────────────────────────

def apply_update(tmp_path: str) -> None:
    """
    Write a batch file that (after we exit) moves the downloaded EXE
    over the current one and relaunches it, then exits the app.

    Only works when running as a frozen PyInstaller EXE.
    """
    current = pathlib.Path(sys.executable)
    tmp     = pathlib.Path(tmp_path)
    bat     = current.with_name("_update.bat")

    bat.write_text(
        "@echo off\r\n"
        "timeout /t 2 /nobreak > nul\r\n"
        f"move /y \"{tmp}\" \"{current}\"\r\n"
        f"start \"\" \"{current}\"\r\n"
        "del \"%~f0\"\r\n",
        encoding="mbcs",
    )
    subprocess.Popen(
        ["cmd", "/c", str(bat)],
        creationflags=(subprocess.DETACHED_PROCESS |
                       subprocess.CREATE_NEW_PROCESS_GROUP),
        close_fds=True,
    )
