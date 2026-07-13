#!/usr/bin/env python3
"""
Pack gui_client.py -> dist/BeamChat.exe using PyInstaller.

Usage:
    python build.py              # release build (no console window)
    python build.py --debug      # debug build (console window, see crash output)
    python build.py --hook       # install git post-commit hook, then build
    python build.py --hook-only  # install hook without building
"""

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

ROOT     = pathlib.Path(__file__).resolve().parent
DIST     = ROOT / "dist"
APP_NAME = "BeamChat"
HOOK_SRC = ROOT / ".git" / "hooks" / "post-commit"

HOOK_CONTENT = """\
#!/bin/sh
# Auto-build BeamChat.exe after every git commit
python "$(git rev-parse --show-toplevel)/build.py"
"""

DEFAULT_CLIENT_CONFIG = {
    "server_url": "ws://106.55.8.122:8765",
    "anonymous_mode": True,
    "allow_custom_server": True,
    "enable_room_password": True,
    "enable_message_persistence": True,
    "default_room_message_ttl_seconds": 604800,
    "default_dm_message_ttl_seconds": 604800,
    "max_file_mb": 500,
    "rooms_persist_when_empty": True,
}


def expected_executable_path(platform: str | None = None) -> pathlib.Path:
    platform_name = platform or sys.platform
    suffix = ".exe" if platform_name.startswith("win") else ""
    return DIST / f"{APP_NAME}{suffix}"


def pyinstaller_dist_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("BEAM_PYINSTALLER_DIST_DIR") or DIST)


def default_artifact_copy_dir(platform: str | None = None) -> pathlib.Path:
    platform_name = platform or sys.platform
    if platform_name.startswith("win"):
        return pathlib.Path("F:/beam-build")
    return pathlib.Path("/mnt/f/beam-build")


def copy_build_artifact(exe: pathlib.Path, target_dir: pathlib.Path | None = None) -> pathlib.Path | None:
    if exe.suffix.lower() != ".exe":
        return None
    target_root = pathlib.Path(
        os.environ.get("BEAM_BUILD_DIR") or target_dir or default_artifact_copy_dir()
    )
    target_root.mkdir(parents=True, exist_ok=True)
    target = target_root / exe.name
    shutil.copy2(exe, target)
    return target

# Qt modules we actually import (checked via grep on the codebase)
# Do NOT add --collect-all=PyQt6 — that pulls in WebEngine/3D/QML (~150 MB wasted)
HIDDEN_IMPORTS = [
    "PyQt6.sip",
    "PyQt6.QtCore",
    "PyQt6.QtGui",
    "PyQt6.QtWidgets",
    "websockets.legacy",
    "websockets.legacy.client",
    "websockets.legacy.server",
    "cryptography.hazmat.primitives.kdf.pbkdf2",
    "cryptography.fernet",
    "sounddevice",
    "numpy",
    "_sounddevice_data",
]

# Explicitly exclude heavy Qt sub-packages that are auto-discovered
# via PyQt6's __init__ but are never used by this app.
EXCLUDES = [
    "PyQt6.QtWebEngineCore",
    "PyQt6.QtWebEngineWidgets",
    "PyQt6.QtWebEngineQuick",
    "PyQt6.QtQml",
    "PyQt6.QtQuick",
    "PyQt6.QtQuick3D",
    "PyQt6.QtQuickWidgets",
    "PyQt6.Qt3DCore",
    "PyQt6.Qt3DRender",
    "PyQt6.Qt3DInput",
    "PyQt6.Qt3DAnimation",
    "PyQt6.Qt3DExtras",
    "PyQt6.QtMultimedia",
    "PyQt6.QtMultimediaWidgets",
    "PyQt6.QtBluetooth",
    "PyQt6.QtNfc",
    "PyQt6.QtSensors",
    "PyQt6.QtSerialPort",
    "PyQt6.QtSpatialAudio",
    "PyQt6.QtRemoteObjects",
    "PyQt6.QtStateMachine",
    "PyQt6.QtTextToSpeech",
    "PyQt6.QtDesigner",
    "PyQt6.QtHelp",
    "PyQt6.QtSql",
    "tkinter",
    "matplotlib",
    "PIL",
    "scipy",
    "pandas",
]


def _run(cmd: list, **kw):
    subprocess.check_call([str(c) for c in cmd], **kw)


def _ensure_pyinstaller():
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller not found - installing...")
        _run([sys.executable, "-m", "pip", "install", "pyinstaller", "-q"])


def load_build_config(path: pathlib.Path | str | None = None, **overrides) -> dict:
    config = dict(DEFAULT_CLIENT_CONFIG)
    if path:
        raw = pathlib.Path(path).read_text(encoding="utf-8")
        loaded = json.loads(raw)
        if not isinstance(loaded, dict):
            raise ValueError("build config must be a JSON object")
        config.update(loaded)
    for key, value in overrides.items():
        if value is not None:
            config[key] = value
    if not str(config.get("server_url", "")).strip():
        raise ValueError("server_url cannot be empty")
    return config


def write_client_build_config(config: dict, path: pathlib.Path | str | None = None) -> pathlib.Path:
    target = pathlib.Path(path) if path else ROOT / "build" / "beam_config.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    return target


def install_hook():
    hook_dir = ROOT / ".git" / "hooks"
    if not hook_dir.exists():
        print("No .git/hooks directory - skipping hook install.")
        return
    HOOK_SRC.write_text(HOOK_CONTENT)
    try:
        HOOK_SRC.chmod(0o755)
    except Exception:
        pass
    print(f"Hook installed: {HOOK_SRC}")


def build(debug: bool = False, config: dict | None = None):
    _ensure_pyinstaller()

    mode = "DEBUG" if debug else "RELEASE"
    print(f"\n{'-'*54}")
    print(f"  Building {APP_NAME}.exe  [{mode}]")
    print(f"{'-'*54}\n")

    for d in [ROOT / "build", ROOT / f"{APP_NAME}.spec"]:
        if d.is_dir():
            shutil.rmtree(d)
        elif d.is_file():
            d.unlink()

    config_path = write_client_build_config(config or DEFAULT_CLIENT_CONFIG)

    icon_ico = ROOT / "assets" / "icon.ico"
    icon_png = ROOT / "assets" / "icon.png"

    build_dist = pyinstaller_dist_dir()

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        f"--name={APP_NAME}",
        f"--distpath={build_dist}",
    ]

    if not debug:
        cmd.append("--windowed")   # hide console in release

    if icon_ico.exists():
        cmd += [f"--icon={icon_ico}"]

    if icon_png.exists():
        cmd += [f"--add-data={icon_png}{os.pathsep}assets"]

    if config_path.exists():
        cmd += [f"--add-data={config_path}{os.pathsep}."]

    for imp in HIDDEN_IMPORTS:
        cmd += ["--hidden-import", imp]

    for mod in EXCLUDES:
        cmd += ["--exclude-module", mod]

    cmd.append(str(ROOT / "gui_client.py"))

    t0 = time.time()
    _run(cmd, cwd=ROOT)
    elapsed = time.time() - t0

    built_exe = build_dist / expected_executable_path().name
    if built_exe.exists():
        exe = expected_executable_path()
        if built_exe.resolve() != exe.resolve():
            exe.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(built_exe, exe)
            except PermissionError as exc:
                print(f"WARN: cannot update {exe}: {exc}")
                exe = built_exe
        mb = exe.stat().st_size / 1024 / 1024
        print(f"\nOK: {exe.name}   {mb:.0f} MB   built in {elapsed:.0f}s")
        print(f"    {exe}\n")
        copied = copy_build_artifact(exe)
        if copied is not None:
            print(f"Copied: {copied}")
        if debug:
            print("Debug build: run the exe from a terminal to see crash output.")
    else:
        print(f"\nFAIL: {exe} not found")
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description="Build BeamChat.exe")
    ap.add_argument("--debug",     action="store_true",
                    help="Build with console window (see runtime errors)")
    ap.add_argument("--config", type=pathlib.Path,
                    help="JSON build config, e.g. {'server_url': 'wss://example.com'}")
    ap.add_argument("--server-url",
                    help="Default server WebSocket URL baked into the packaged client")
    ap.add_argument("--anonymous", action="store_true",
                    help="Record anonymous_mode=true in the packaged config")
    ap.add_argument("--no-custom-server", action="store_true",
                    help="Record allow_custom_server=false in the packaged config")
    ap.add_argument("--hook",      action="store_true",
                    help="Install git post-commit hook, then build")
    ap.add_argument("--hook-only", action="store_true",
                    help="Install git post-commit hook without building")
    args = ap.parse_args()

    if args.hook or args.hook_only:
        install_hook()

    if not args.hook_only:
        config = load_build_config(
            args.config,
            server_url=args.server_url,
            anonymous_mode=True if args.anonymous else None,
            allow_custom_server=False if args.no_custom_server else None,
        )
        build(debug=args.debug, config=config)


if __name__ == "__main__":
    main()
