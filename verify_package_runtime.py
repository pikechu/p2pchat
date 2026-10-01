"""从可信 EXE 提取依赖，在不使用工具链第三方包的进程中验证运行能力。"""

from __future__ import annotations

import argparse
import importlib.util
import json
import marshal
import os
from pathlib import Path, PureWindowsPath
import shutil
import struct
import subprocess
import sys
import tempfile
import traceback


def _f_path(path: Path, *, allow_root: bool = False) -> Path:
    """限定所有验证产物在 F 盘，并拒绝把盘根作为工作目录。"""
    resolved = path.resolve()
    if resolved.drive.upper() != "F:" or (not allow_root and resolved == Path("F:/")):
        raise ValueError(f"验证路径必须是 F 盘的具体目录或文件：{resolved}")
    return resolved


def _target(root: Path, name: str) -> Path:
    """校验归档条目的相对路径，防止写出本次提取目录。"""
    relative = PureWindowsPath(name)
    if relative.is_absolute() or relative.drive or ".." in relative.parts:
        raise ValueError(f"归档含有非法路径：{name}")
    target = (root / Path(*relative.parts)).resolve()
    if not target.is_relative_to(root):
        raise ValueError(f"归档条目超出提取目录：{name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _write_pyc(target: Path, code) -> None:
    """生成可由标准导入器直接加载的无源代码字节码文件。"""
    target.write_bytes(importlib.util.MAGIC_NUMBER + struct.pack("<III", 0, 0, 0) + marshal.dumps(code))


def _extract(exe: Path, root: Path) -> dict:
    from PyInstaller.archive.readers import CArchiveReader

    archive = CArchiveReader(str(exe))
    binaries = modules = 0
    for name, entry in archive.toc.items():
        kind = entry[-1]
        if kind in {"b", "x", "Z"}:
            _target(root, name).write_bytes(archive.extract(name))
            binaries += kind == "b"
        elif kind in {"m", "M", "s"}:
            filename = name.replace(".", "/") + ("/__init__.pyc" if kind == "M" else ".pyc")
            # 主应用和引导脚本只提取，不在验证进程中执行。
            _write_pyc(_target(root, filename), marshal.loads(archive.extract(name)))
    pyz = archive.open_embedded_archive("PYZ.pyz")
    for name, entry in pyz.toc.items():
        kind = entry[0]
        relative = name.replace(".", "/")
        if kind == 3:
            _target(root, relative + "/.namespace").parent.mkdir(parents=True, exist_ok=True)
        elif kind in {0, 1}:
            relative += "/__init__.pyc" if kind == 1 else ".pyc"
            _write_pyc(_target(root, relative), pyz.extract(name))
            modules += 1
        else:
            raise ValueError(f"不支持的 PYZ 条目类型：{name} ({kind})")
    return {"binary_count": binaries, "module_count": modules}


def _verify_worker(root: Path) -> dict:
    """只允许包内第三方依赖；解释器及标准库来自同一 F 盘 Python。"""
    stdlib = [p for p in sys.path if p and "site-packages" not in p.lower()]
    sys.path[:] = [str(root), str(root / "base_library.zip"), *stdlib]
    sys._MEIPASS = str(root)
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["QT_PLUGIN_PATH"] = str(root / "PyQt6" / "Qt6" / "plugins")
    os.environ.pop("QT_QPA_PLATFORM_PLUGIN_PATH", None)
    os.environ.pop("QML2_IMPORT_PATH", None)
    os.environ["PATH"] = os.pathsep.join([
        str(root), str(root / "PyQt6" / "Qt6" / "bin"),
        str(Path(os.environ["SystemRoot"]) / "System32"), os.environ["SystemRoot"],
    ])
    # 保持目录句柄存活，避免 Windows 撤销 DLL 搜索目录。
    dll_handles = []
    for directory in [root, root / "PyQt6" / "Qt6" / "bin", root / "av.libs", root / "numpy.libs"]:
        if directory.is_dir():
            dll_handles.append(os.add_dll_directory(str(directory)))
    for hook in ("pyi_rth_cryptography_openssl", "pyi_rth_pyqt6"):
        path = root / f"{hook}.pyc"
        if path.is_file():
            code = marshal.loads(path.read_bytes()[16:])
            exec(code, {"__name__": hook, "__file__": str(path)})

    import base64
    import asyncio
    import PyQt6
    import numpy as np
    import av
    import aiortc
    from cryptography.fernet import Fernet
    from PyQt6.QtCore import QByteArray, QBuffer, QIODevice
    from PyQt6.QtGui import QColor, QImage, QImageReader, QPainter, QPixmap
    from PyQt6.QtWidgets import QApplication, QLabel, QPushButton, QVBoxLayout, QWidget

    module_paths = {}
    for module in (PyQt6, np, av, aiortc):
        path = Path(module.__file__).resolve()
        if not path.is_relative_to(root):
            raise AssertionError(f"第三方模块来自包外：{module.__name__} -> {path}")
        module_paths[module.__name__] = str(path.relative_to(root))
    # 任何第三方包回退到工具链 site-packages 都视为验证失败。
    for name, module in list(sys.modules.items()):
        path = getattr(module, "__file__", None)
        if path and "site-packages" in str(path).lower():
            raise AssertionError(f"隔离失效：{name} -> {path}")

    application = QApplication([])
    supported = {bytes(value).decode("ascii").lower() for value in QImageReader.supportedImageFormats()}
    for image_format in ("png", "jpeg", "gif", "webp", "tiff"):
        if image_format not in supported:
            raise AssertionError(f"缺少图片格式插件：{image_format}")
    image = QImage(8, 8, QImage.Format.Format_RGB32)
    image.fill(QColor("#38a169"))
    parsed_formats = []
    for image_format in ("PNG", "JPEG", "WEBP", "TIFF"):
        encoded = QByteArray()
        buffer = QBuffer(encoded)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not image.save(buffer, image_format):
            raise AssertionError(f"图片编码失败：{image_format}")
        buffer.close()
        decoded = QImage.fromData(encoded, image_format)
        if decoded.isNull() or decoded.width() != 8 or decoded.height() != 8:
            raise AssertionError(f"图片解析失败：{image_format}")
        parsed_formats.append(image_format)
    gif = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")
    decoded_gif = QImage.fromData(gif, "GIF")
    if decoded_gif.isNull() or decoded_gif.width() != 1:
        raise AssertionError("GIF 图片解析失败")
    parsed_formats.append("GIF")

    widget = QWidget()
    layout = QVBoxLayout(widget)
    label = QLabel("BeamChat 包内依赖验证")
    layout.addWidget(label)
    layout.addWidget(QPushButton("验证按钮"))
    widget.resize(320, 120)
    application.processEvents()
    rendered = QPixmap(widget.size())
    rendered.fill(QColor("white"))
    widget.render(rendered)
    painter = QPainter(rendered)
    painter.fillRect(0, 0, 4, 4, QColor("red"))
    painter.end()
    if rendered.toImage().pixelColor(1, 1) != QColor("red"):
        raise AssertionError("QPainter / QWidget 光栅渲染失败")
    widget.close()

    samples = np.array([0.25, -0.25, 0.0], dtype=np.float32)
    pcm = (samples * 32768).astype(np.int16).tobytes()
    restored = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    if not np.array_equal(restored, samples):
        raise AssertionError("语音 PCM / float32 转换失败")
    audio = av.AudioFrame.from_ndarray(np.zeros((1, 480), dtype=np.int16), format="s16", layout="mono")
    if audio.samples != 480:
        raise AssertionError("PyAV 音频帧转换失败")
    cipher = Fernet(Fernet.generate_key())
    if cipher.decrypt(cipher.encrypt(b"beamchat-package-validation")) != b"beamchat-package-validation":
        raise AssertionError("加密依赖验证失败")

    async def verify_channel() -> dict:
        import aioice.ice
        from aiortc import RTCConfiguration, RTCPeerConnection

        # 仅收集回环地址，不向 STUN、TURN、局域网或生产服务发送请求。
        original_addresses = aioice.ice.get_host_addresses
        aioice.ice.get_host_addresses = lambda use_ipv4, use_ipv6: ["127.0.0.1"] if use_ipv4 else []
        first = second = None
        try:
            first = RTCPeerConnection(RTCConfiguration(iceServers=[]))
            second = RTCPeerConnection(RTCConfiguration(iceServers=[]))
            channel = first.createDataChannel("package-check")
            received = asyncio.get_running_loop().create_future()

            @second.on("datachannel")
            def on_channel(incoming):
                @incoming.on("message")
                def on_message(message):
                    if not received.done():
                        received.set_result(message)

            @channel.on("open")
            def on_open():
                channel.send("beamchat-local-package-check")

            await first.setLocalDescription(await first.createOffer())
            await second.setRemoteDescription(first.localDescription)
            await second.setLocalDescription(await second.createAnswer())
            await first.setRemoteDescription(second.localDescription)
            for description in (first.localDescription, second.localDescription):
                candidates = [line for line in description.sdp.splitlines() if line.startswith("a=candidate:")]
                if not candidates or any(line.split()[4] != "127.0.0.1" for line in candidates):
                    raise AssertionError("WebRTC 验证出现非回环候选地址")
            message = await asyncio.wait_for(received, timeout=20)
            if message != "beamchat-local-package-check":
                raise AssertionError("WebRTC DataChannel 消息不匹配")
            return {"address": "127.0.0.1", "message_received": True, "stun_turn_used": False}
        finally:
            if first is not None:
                await first.close()
            if second is not None:
                await second.close()
            aioice.ice.get_host_addresses = original_addresses

    channel_result = asyncio.run(asyncio.wait_for(verify_channel(), timeout=30))
    application.quit()
    return {
        "ok": True, "mode": "isolated-extracted-package", "module_paths": module_paths,
        "qt_platform": "offscreen", "widget_rendered": True, "image_formats_decoded": parsed_formats,
        "voice_pcm_conversion": True, "av_audio_frame": True, "fernet": True,
        "webrtc_datachannel": channel_result,
        "limits": ["未运行 EXE 引导器；应另用 EXE --help 检查引导器", "未启动麦克风或连接生产服务"],
    }


def main() -> int:
    # -I 忽略编码环境变量，显式固定 JSON 管道输出为 UTF-8。
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="隔离验证可信 BeamChat EXE 内的运行依赖")
    parser.add_argument("exe", nargs="?", type=Path)
    parser.add_argument("--work-dir", type=Path, default=Path("F:/beam-build/tmp"))
    parser.add_argument("--_worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if sys.platform != "win32":
            raise RuntimeError("本脚本验证 Windows 包，需使用 F 盘 Windows Python")
        _f_path(Path(sys.executable))
        if args._worker is not None:
            report = _verify_worker(_f_path(args._worker))
            print(json.dumps(report, ensure_ascii=False))
            return 0
        if args.exe is None:
            parser.error("请提供本次可信 EXE 的路径")
        exe = _f_path(args.exe)
        base = _f_path(args.work_dir)
        base.mkdir(parents=True, exist_ok=True)
        root = _f_path(Path(tempfile.mkdtemp(prefix="beam-package-check-", dir=base)))
        extracted = _extract(exe, root)
        temp = root / "tmp"
        temp.mkdir()
        environment = dict(os.environ)
        for name in ("TEMP", "TMP", "TMPDIR", "XDG_RUNTIME_DIR", "XDG_CACHE_HOME"):
            environment[name] = str(temp)
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-B", str(Path(__file__).resolve()), "--_worker", str(root)],
            cwd=root, env=environment, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            worker = json.loads(result.stdout.strip())
        except json.JSONDecodeError:
            worker = {"ok": False, "error": "验证子进程没有返回有效 JSON", "stdout": result.stdout}
        report = {"executable": str(exe), "executable_bytes": exe.stat().st_size, "extracted": extracted, **worker}
        if result.returncode or not worker.get("ok"):
            report.update(ok=False, worker_exit_code=result.returncode, worker_stderr=result.stderr, preserved_extract_dir=str(root))
        else:
            # 仅删除由本次调用创建的临时目录；保留 F 盘 JSON 验证记录。
            if not root.is_relative_to(base) or not root.name.startswith("beam-package-check-"):
                raise RuntimeError("拒绝清理非本次验证目录")
            shutil.rmtree(root)
        report_path = base / "beam-package-runtime-report.json"
        report["report_path"] = str(report_path)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report.get("ok") else 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc), "traceback": traceback.format_exc()}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())