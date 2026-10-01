"""Windows 构建入口：保留 PE 校验，并兼容生成文件的短暂共享锁。"""

from __future__ import annotations

import errno
import logging
import os
from pathlib import Path
import struct
import sys
import time

logger = logging.getLogger(__name__)


def _write_fields(path: str, source: bytes, fields: list[tuple[int, bytes]], max_wait: float = 30) -> None:
    """只写入等长头字段；共享锁超时或文件已变更时明确失败。"""
    changes = []
    for offset, replacement in fields:
        if offset < 0 or offset + len(replacement) > len(source):
            raise ValueError("PE 头字段超出文件范围")
        previous = source[offset:offset + len(replacement)]
        if previous != replacement:
            changes.append((offset, previous, replacement))
    if not changes:
        return

    started = time.monotonic()
    last_error = None
    announced = False
    while True:
        try:
            stream = open(path, "r+b")
            break
        except OSError as exc:
            if exc.errno != errno.EACCES and getattr(exc, "winerror", None) not in (32, 33):
                raise
            elapsed = time.monotonic() - started
            if elapsed >= max_wait:
                last_error = exc
                break
            if not announced:
                logger.warning("EXE 暂时无法写入，等待共享锁释放：%s", path)
                announced = True
            time.sleep(min(1, max_wait - elapsed))

    if last_error is not None:
        # 在异常处理块外抛出，避免 PyInstaller 检查 __context__ 后重复等待。
        raise RuntimeError(f"EXE 写入等待 {max_wait:g} 秒仍被拒绝：{path}；{last_error}")

    with stream:
        if os.fstat(stream.fileno()).st_size != len(source):
            raise RuntimeError("EXE 长度在更新头字段前发生变化")
        # 先校验全部字段，再写入，避免校验失败留下部分更新。
        for offset, previous, _ in changes:
            stream.seek(offset)
            if stream.read(len(previous)) != previous:
                raise RuntimeError("EXE 头字段已被其他操作修改")
        for offset, _, replacement in changes:
            stream.seek(offset)
            if stream.write(replacement) != len(replacement):
                raise OSError("EXE 头字段未完整写入")
        stream.flush()
        os.fsync(stream.fileno())


def set_exe_build_timestamp(exe_path: str, timestamp: int) -> None:
    """更新文件头和已有调试目录的时间戳，保留附加归档。"""
    import pefile

    source = Path(exe_path).read_bytes()
    value = struct.pack("<I", int(timestamp))
    with pefile.PE(data=source, fast_load=True) as pe:
        pe.full_load()
        fields = [(pe.FILE_HEADER.get_field_absolute_offset("TimeDateStamp"), value)]
        for entry in getattr(pe, "DIRECTORY_ENTRY_DEBUG", []):
            if entry.struct.TimeDateStamp:
                fields.append((entry.struct.get_field_absolute_offset("TimeDateStamp"), value))
    _write_fields(exe_path, source, fields)


def update_exe_pe_checksum(exe_path: str) -> None:
    """使用 PyInstaller 的系统算法计算整个 EXE 的校验和并写回头字段。"""
    import pefile
    from PyInstaller.utils.win32 import winutils

    source = Path(exe_path).read_bytes()
    checksum = winutils.compute_exe_pe_checksum(exe_path)
    with pefile.PE(data=source, fast_load=True) as pe:
        offset = pe.OPTIONAL_HEADER.get_field_absolute_offset("CheckSum")
    _write_fields(exe_path, source, [(offset, struct.pack("<I", checksum))])


def main() -> None:
    """兼容处理仅作用于本次 Windows 打包进程，不修改已安装的工具。"""
    from PyInstaller.__main__ import run
    from PyInstaller.utils.win32 import winutils

    original_timestamp = winutils.set_exe_build_timestamp
    original_checksum = winutils.update_exe_pe_checksum
    original_path = os.environ.get("PATH")
    python_root = Path(sys.executable).parent
    windows_root = Path(os.environ["SystemRoot"])
    try:
        # 排除外部应用的同名 DLL；Windows 原生依赖使用系统目录。
        os.environ["PATH"] = os.pathsep.join(map(str, [
            python_root, python_root / "DLLs", windows_root / "System32", windows_root,
        ]))
        winutils.set_exe_build_timestamp = set_exe_build_timestamp
        winutils.update_exe_pe_checksum = update_exe_pe_checksum
        run()
    finally:
        winutils.set_exe_build_timestamp = original_timestamp
        winutils.update_exe_pe_checksum = original_checksum
        if original_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = original_path


if __name__ == "__main__":
    main()
