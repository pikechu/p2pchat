#!/usr/bin/env python3
"""
Pack gui_client.py → dist/BeamChat.exe using PyInstaller.

Usage:
    python build.py              # build once
    python build.py --hook       # install git post-commit hook, then build
    python build.py --hook-only  # install hook without building
"""

import argparse
import pathlib
import shutil
import subprocess
import sys
import time

ROOT     = pathlib.Path(__file__).resolve().parent
DIST     = ROOT / "dist"
APP_NAME = "BeamChat"
HOOK_SRC = ROOT / ".git" / "hooks" / "post-commit"


# ── helpers ───────────────────────────────────────────────────────────────────

def _run(cmd: list, **kw):
    subprocess.check_call([str(c) for c in cmd], **kw)


def _ensure_pyinstaller():
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller not found — installing…")
        _run([sys.executable, "-m", "pip", "install", "pyinstaller", "-q"])


# ── git hook ──────────────────────────────────────────────────────────────────

HOOK_CONTENT = """\
#!/bin/sh
# Auto-build BeamChat.exe after every git commit
python "$(git rev-parse --show-toplevel)/build.py"
"""


def install_hook():
    hook_dir = ROOT / ".git" / "hooks"
    if not hook_dir.exists():
        print("No .git/hooks directory found — skipping hook install.")
        return
    HOOK_SRC.write_text(HOOK_CONTENT)
    # On Unix git needs the file to be executable; on Windows git bash handles it fine
    try:
        HOOK_SRC.chmod(0o755)
    except Exception:
        pass
    print(f"Hook installed: {HOOK_SRC}")


# ── build ─────────────────────────────────────────────────────────────────────

def build():
    _ensure_pyinstaller()

    print(f"\n{'-'*54}")
    print(f"  Building {APP_NAME}.exe")
    print(f"{'-'*54}\n")

    # Clean stale build artefacts (keeps previous dist/BeamChat.exe until new one appears)
    for d in [ROOT / "build", ROOT / f"{APP_NAME}.spec"]:
        if d.is_dir():
            shutil.rmtree(d)
        elif d.is_file():
            d.unlink()

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--windowed",           # no console window
        f"--name={APP_NAME}",
        "--collect-all=PyQt6",  # bundle all Qt DLLs / plugins
        "--hidden-import=PyQt6.sip",
        "--hidden-import=websockets.legacy",
        "--hidden-import=websockets.legacy.client",
        "--hidden-import=websockets.legacy.server",
        "--hidden-import=cryptography.hazmat.primitives.kdf.pbkdf2",
        "--hidden-import=cryptography.fernet",
        str(ROOT / "gui_client.py"),
    ]

    t0 = time.time()
    _run(cmd, cwd=ROOT)
    elapsed = time.time() - t0

    exe = DIST / f"{APP_NAME}.exe"
    if exe.exists():
        mb = exe.stat().st_size / 1024 / 1024
        print(f"\nOK: {exe.name}   {mb:.0f} MB   built in {elapsed:.0f}s")
        print(f"    {exe}\n")
    else:
        print(f"\nFAIL: {exe} not found")
        sys.exit(1)


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Build BeamChat.exe")
    ap.add_argument("--hook",      action="store_true",
                    help="Install git post-commit hook, then build")
    ap.add_argument("--hook-only", action="store_true",
                    help="Install git post-commit hook without building")
    args = ap.parse_args()

    if args.hook or args.hook_only:
        install_hook()

    if not args.hook_only:
        build()


if __name__ == "__main__":
    main()
