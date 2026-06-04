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


def build(debug: bool = False):
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

    icon_ico = ROOT / "assets" / "icon.ico"
    icon_png = ROOT / "assets" / "icon.png"

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        f"--name={APP_NAME}",
    ]

    if not debug:
        cmd.append("--windowed")   # hide console in release

    if icon_ico.exists():
        cmd += [f"--icon={icon_ico}"]

    if icon_png.exists():
        cmd += [f"--add-data={icon_png}{os.pathsep}assets"]

    for imp in HIDDEN_IMPORTS:
        cmd += ["--hidden-import", imp]

    for mod in EXCLUDES:
        cmd += ["--exclude-module", mod]

    cmd.append(str(ROOT / "gui_client.py"))

    t0 = time.time()
    _run(cmd, cwd=ROOT)
    elapsed = time.time() - t0

    exe = DIST / f"{APP_NAME}.exe"
    if exe.exists():
        mb = exe.stat().st_size / 1024 / 1024
        print(f"\nOK: {exe.name}   {mb:.0f} MB   built in {elapsed:.0f}s")
        print(f"    {exe}\n")
        if debug:
            print("Debug build: run the exe from a terminal to see crash output.")
    else:
        print(f"\nFAIL: {exe} not found")
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description="Build BeamChat.exe")
    ap.add_argument("--debug",     action="store_true",
                    help="Build with console window (see runtime errors)")
    ap.add_argument("--hook",      action="store_true",
                    help="Install git post-commit hook, then build")
    ap.add_argument("--hook-only", action="store_true",
                    help="Install git post-commit hook without building")
    args = ap.parse_args()

    if args.hook or args.hook_only:
        install_hook()

    if not args.hook_only:
        build(debug=args.debug)


if __name__ == "__main__":
    main()
