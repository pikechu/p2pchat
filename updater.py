"""
Self-update logic for BeamChat.

Flow:
  1. check_update()  →  (new_version_str, download_url, error_msg)
  2. UpdateDownloader(url, checksum_url)  →  下载并校验后发出 finished(tmp_path)
  3. apply_update(tmp_path) →  launch swap batch file + quit
"""

import json
import hashlib
import pathlib
import re
import subprocess
import urllib.error
import sys
import urllib.request

from PyQt6.QtCore import QThread, pyqtSignal

from version import __version__

GITHUB_API = "https://api.github.com/repos/pikechu/p2pchat/releases/latest"
ASSET_NAME = "BeamChat.exe"
CHECKSUM_ASSET_NAME = f"{ASSET_NAME}.sha256"
_SHA256_RE = re.compile(r"^(?P<digest>[0-9a-fA-F]{64})(?:\s+\*?(?P<name>\S+))?\s*$")


# ── Version comparison ────────────────────────────────────────────────────────

def _parse(v: str) -> tuple:
    try:
        return tuple(int(x) for x in v.strip().lstrip("v").split("."))
    except Exception:
        return (0,)


def is_newer(remote: str, local: str = __version__) -> bool:
    return _parse(remote) > _parse(local)


# ── GitHub release check ──────────────────────────────────────────────────────

import time as _time
_cache: tuple | None = None          # (result_tuple, timestamp)
_CACHE_TTL = 600                     # 10 minutes
_checksum_urls: dict[str, str] = {}

def check_update(timeout: int = 10) -> tuple[str | None, str | None, str | None]:
    """
    Return (version, download_url, error_msg).
    - version is set only when a newer release with the EXE asset exists.
    - error_msg is set when the check itself failed (network / rate-limit).
    - Both None means "already latest".
    """
    global _cache
    if _cache is not None:
        result, ts = _cache
        if _time.time() - ts < _CACHE_TTL:
            return result

    try:
        req = urllib.request.Request(
            GITHUB_API,
            headers={"User-Agent": f"BeamChat/{__version__}"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        # GitHub returns {"message": "..."} when rate-limited or errored
        if "message" in data and "tag_name" not in data:
            msg: str = data["message"]
            if "rate limit" in msg.lower():
                return None, None, "GitHub 访问次数超限，请稍后再试（每小时60次）"
            return None, None, f"GitHub: {msg}"
        tag = data.get("tag_name", "")
        if not is_newer(tag):
            result = (None, None, None)       # genuinely up to date
            _cache = (result, _time.time())
            return result
        assets = {
            str(asset.get("name", "")).lower(): str(asset.get("browser_download_url", ""))
            for asset in data.get("assets", [])
            if asset.get("name") and asset.get("browser_download_url")
        }
        download_url = assets.get(ASSET_NAME.lower())
        checksum_url = assets.get(CHECKSUM_ASSET_NAME.lower())
        if download_url and checksum_url:
            _checksum_urls[download_url] = checksum_url
            result = (tag.lstrip("v"), download_url, None)
            _cache = (result, _time.time())
            return result
        if download_url:
            return None, None, f"v{tag.lstrip('v')} 缺少更新完整性清单，请稍后重试"
        # Tag is newer but Actions hasn't uploaded the EXE yet — don't cache
        return None, None, f"v{tag.lstrip('v')} 正在构建中，请稍后重试"
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            return None, None, "GitHub 访问次数超限，请稍后再试（每小时60次）"
        return None, None, f"网络错误 {exc.code}"
    except urllib.error.URLError as exc:
        return None, None, f"网络连接失败: {exc.reason}"
    except Exception as exc:
        return None, None, str(exc)


# ── Download thread ───────────────────────────────────────────────────────────

class UpdateDownloader(QThread):
    progress  = pyqtSignal(int)        # 0–100
    finished  = pyqtSignal(str)        # tmp EXE path on success
    failed    = pyqtSignal(str)        # error message

    def __init__(self, url: str, parent=None, checksum_url: str | None = None):
        super().__init__(parent)
        self._url = url
        self._checksum_url = checksum_url or _checksum_urls.get(url) or f"{url}.sha256"

    def run(self):
        tmp: pathlib.Path | None = None
        try:
            current = pathlib.Path(sys.executable)
            tmp = current.with_name("_BeamChat_update.exe")
            expected_sha256 = _download_checksum(self._checksum_url)
            req = urllib.request.Request(
                self._url,
                headers={"User-Agent": f"BeamChat/{__version__}"},
            )
            with urllib.request.urlopen(req) as resp:
                total = int(resp.headers.get("Content-Length", 0))
                downloaded = 0
                digest = hashlib.sha256()
                with open(tmp, "wb") as f:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                        digest.update(chunk)
                        downloaded += len(chunk)
                        if total:
                            self.progress.emit(int(downloaded * 100 / total))
            if total and downloaded != total:
                raise ValueError("更新包下载不完整")
            if digest.hexdigest() != expected_sha256:
                raise ValueError("更新包 SHA-256 校验失败，已拒绝安装")
            self.finished.emit(str(tmp))
        except Exception as exc:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
            self.failed.emit(str(exc))


def _download_checksum(url: str, timeout: int = 10) -> str:
    """下载并严格解析发布资产的 SHA-256 清单。"""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": f"BeamChat/{__version__}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(4096)
        if resp.read(1):
            raise ValueError("更新完整性清单过大")
    try:
        line = raw.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise ValueError("更新完整性清单格式无效") from exc
    match = _SHA256_RE.fullmatch(line)
    if match is None:
        raise ValueError("更新完整性清单格式无效")
    filename = match.group("name")
    if filename and pathlib.PurePath(filename).name.lower() != ASSET_NAME.lower():
        raise ValueError("更新完整性清单与安装包不匹配")
    return match.group("digest").lower()


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
