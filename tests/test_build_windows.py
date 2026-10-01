"""验证 Windows PE 头更新的兼容性、共享锁等待和归档完整性。"""

import builtins
import errno
from pathlib import Path
import struct
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import build_windows


class FakeClock:
    """用可控时钟验证等待预算，不让测试实际休眠。"""

    def __init__(self):
        self.value = 0.0
        self.delays = []

    def monotonic(self):
        return self.value

    def sleep(self, delay):
        self.delays.append(delay)
        self.value += delay


def install_clock(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(
        build_windows,
        "time",
        SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep),
    )
    return clock


def sharing_error():
    error = PermissionError(errno.EACCES, "测试中的短暂文件占用")
    error.winerror = 32
    return error


def assert_only_fields_changed(before, after, offsets):
    """确认 PE 头以外的数据及附加归档逐字节保持原样。"""
    assert len(after) == len(before)
    previous_end = 0
    for offset in sorted(set(offsets)):
        assert after[previous_end:offset] == before[previous_end:offset]
        previous_end = offset + 4
    assert after[previous_end:] == before[previous_end:]


@pytest.fixture
def bootloader_data():
    pyinstaller = pytest.importorskip("PyInstaller")
    pefile = pytest.importorskip("pefile")
    path = (
        Path(pyinstaller.__file__).parent
        / "bootloader"
        / "Windows-64bit-intel"
        / "runw.exe"
    )
    if not path.is_file():
        pytest.skip("当前 PyInstaller 安装未提供 Windows 引导程序")
    source = path.read_bytes()
    with pefile.PE(data=source, fast_load=True) as pe:
        pe.full_load()
        debug_offsets = [
            entry.struct.get_field_absolute_offset("TimeDateStamp")
            for entry in getattr(pe, "DIRECTORY_ENTRY_DEBUG", [])
            if entry.struct.TimeDateStamp
        ]
    if not debug_offsets:
        pytest.skip("当前引导程序没有可验证的非零调试时间戳")
    return source, debug_offsets


@pytest.mark.skipif(sys.platform != "win32", reason="系统 PE 校验和计算仅在 Windows 可用")
@pytest.mark.parametrize("zero_debug_timestamp", [False, True])
def test_pe_header_updates_match_pyinstaller_and_preserve_overlay(
    tmp_path, bootloader_data, zero_debug_timestamp
):
    from PyInstaller.building.api import EXE
    from PyInstaller.utils.win32 import winutils
    import pefile

    source, debug_offsets = bootloader_data
    if zero_debug_timestamp:
        source = bytearray(source)
        for offset in debug_offsets:
            source[offset:offset + 4] = b"\0" * 4
        source = bytes(source)
    overlay = b"BeamChat-package-integrity\0\xff" * 4096
    source += overlay
    original = tmp_path / "原实现.exe"
    compatible = tmp_path / "兼容实现.exe"
    original.write_bytes(source)
    compatible.write_bytes(source)
    timestamp = 1600000000

    EXE._retry_operation(winutils.set_exe_build_timestamp, str(original), timestamp)
    EXE._retry_operation(winutils.update_exe_pe_checksum, str(original))
    build_windows.set_exe_build_timestamp(str(compatible), timestamp)
    build_windows.update_exe_pe_checksum(str(compatible))

    result = compatible.read_bytes()
    assert result == original.read_bytes()
    assert result[-len(overlay):] == overlay
    with pefile.PE(data=result, fast_load=True) as pe:
        pe.full_load()
        assert pe.FILE_HEADER.TimeDateStamp == timestamp
        changed_offsets = [
            pe.FILE_HEADER.get_field_absolute_offset("TimeDateStamp"),
            pe.OPTIONAL_HEADER.get_field_absolute_offset("CheckSum"),
        ]
        expected_debug_timestamp = 0 if zero_debug_timestamp else timestamp
        for offset in debug_offsets:
            assert struct.unpack_from("<I", result, offset)[0] == expected_debug_timestamp
        if not zero_debug_timestamp:
            changed_offsets += debug_offsets
        stored_checksum = pe.OPTIONAL_HEADER.CheckSum
    assert stored_checksum == winutils.compute_exe_pe_checksum(str(compatible))
    assert_only_fields_changed(source, result, changed_offsets)


def test_write_fields_waits_for_short_sharing_lock(tmp_path, monkeypatch):
    path = tmp_path / "headers.exe"
    source = b"abcdefgh01234567"
    path.write_bytes(source)
    clock = install_clock(monkeypatch)
    attempts = []

    def temporarily_locked(filename, mode):
        attempts.append((filename, mode))
        if len(attempts) <= 2:
            raise sharing_error()
        return builtins.open(filename, mode)

    monkeypatch.setattr(build_windows, "open", temporarily_locked, raising=False)
    build_windows._write_fields(str(path), source, [(1, b"WXYZ")], max_wait=5)

    assert attempts == [(str(path), "r+b")] * 3
    assert clock.delays == [1, 1]
    assert path.read_bytes() == b"aWXYZfgh01234567"
    assert_only_fields_changed(source, path.read_bytes(), [1])


def test_write_fields_timeout_has_no_retryable_context(tmp_path, monkeypatch):
    path = tmp_path / "headers.exe"
    source = b"abcdefgh"
    path.write_bytes(source)
    clock = install_clock(monkeypatch)
    attempts = []

    def permanently_locked(filename, mode):
        attempts.append((filename, mode))
        raise sharing_error()

    monkeypatch.setattr(build_windows, "open", permanently_locked, raising=False)
    with pytest.raises(RuntimeError, match="等待") as caught:
        build_windows._write_fields(str(path), source, [(0, b"XYZW")], max_wait=2.5)

    assert clock.value == 2.5
    assert clock.delays == [1, 1, 0.5]
    assert len(attempts) == 4
    assert caught.value.__context__ is None
    assert path.read_bytes() == source


def test_timeout_is_not_repeated_by_pyinstaller_outer_retry(tmp_path, monkeypatch):
    pytest.importorskip("PyInstaller")
    from PyInstaller.building import api

    path = tmp_path / "headers.exe"
    source = b"abcdefgh"
    path.write_bytes(source)
    clock = install_clock(monkeypatch)
    monkeypatch.setattr(api, "time", SimpleNamespace(sleep=clock.sleep))
    attempts = []

    def permanently_locked(filename, mode):
        attempts.append((filename, mode))
        raise sharing_error()

    monkeypatch.setattr(build_windows, "open", permanently_locked, raising=False)

    def update_header():
        build_windows._write_fields(str(path), source, [(0, b"XYZW")], max_wait=0)

    with pytest.raises(RuntimeError, match="等待") as caught:
        api.EXE._retry_operation(update_header)

    assert attempts == [(str(path), "r+b")]
    assert caught.value.__context__ is None
    assert clock.delays == []
    assert path.read_bytes() == source


@pytest.mark.parametrize("offset,replacement", [(-1, b"X"), (7, b"XY"), (9, b"X")])
def test_invalid_field_is_rejected_before_any_write(tmp_path, monkeypatch, offset, replacement):
    path = tmp_path / "headers.exe"
    source = b"abcdefgh"
    path.write_bytes(source)
    attempts = []

    def observed_open(filename, mode):
        attempts.append((filename, mode))
        return builtins.open(filename, mode)

    monkeypatch.setattr(build_windows, "open", observed_open, raising=False)
    with pytest.raises(ValueError, match="超出"):
        build_windows._write_fields(str(path), source, [(0, b"XYZW"), (offset, replacement)])

    assert attempts == []
    assert path.read_bytes() == source


@pytest.mark.parametrize(
    "changed_source,error_text",
    [(b"abcdefgh!", "长度"), (b"abcdefg", "长度"), (b"abcd?fgh", "头字段")],
)
def test_changed_file_while_waiting_is_rejected_without_partial_write(
    tmp_path, monkeypatch, changed_source, error_text
):
    path = tmp_path / "headers.exe"
    source = b"abcdefgh"
    path.write_bytes(source)
    clock = install_clock(monkeypatch)
    attempts = []

    def change_while_locked(filename, mode):
        attempts.append((filename, mode))
        if len(attempts) == 1:
            with builtins.open(filename, "wb") as stream:
                stream.write(changed_source)
            raise sharing_error()
        return builtins.open(filename, mode)

    monkeypatch.setattr(build_windows, "open", change_while_locked, raising=False)
    with pytest.raises(RuntimeError, match=error_text):
        build_windows._write_fields(str(path), source, [(0, b"XYZW"), (4, b"1234")], max_wait=5)

    assert len(attempts) == 2
    assert clock.delays == [1]
    assert path.read_bytes() == changed_source
    assert path.read_bytes()[:4] == source[:4]


def test_other_io_errors_fail_immediately(tmp_path, monkeypatch):
    path = tmp_path / "headers.exe"
    source = b"abcdefgh"
    path.write_bytes(source)
    clock = install_clock(monkeypatch)
    attempts = []

    def disk_error(filename, mode):
        attempts.append((filename, mode))
        raise OSError(errno.ENOSPC, "测试中的磁盘空间不足")

    monkeypatch.setattr(build_windows, "open", disk_error, raising=False)
    with pytest.raises(OSError) as caught:
        build_windows._write_fields(str(path), source, [(0, b"XYZW")])

    assert caught.value.errno == errno.ENOSPC
    assert attempts == [(str(path), "r+b")]
    assert clock.delays == []
    assert path.read_bytes() == source


@pytest.mark.parametrize("inherited_path", [r"C:\external-poppler;D:\external-tools", None])
@pytest.mark.parametrize("build_fails", [False, True])
def test_main_isolates_path_and_restores_process_state(
    tmp_path, monkeypatch, inherited_path, build_fails
):
    pytest.importorskip("PyInstaller")
    from PyInstaller import __main__ as pyinstaller_main
    from PyInstaller.utils.win32 import winutils

    previous_timestamp = winutils.set_exe_build_timestamp
    previous_checksum = winutils.update_exe_pe_checksum
    windows_root = tmp_path / "Windows"
    monkeypatch.setenv("SystemRoot", str(windows_root))
    if inherited_path is None:
        monkeypatch.delenv("PATH", raising=False)
    else:
        monkeypatch.setenv("PATH", inherited_path)
    python_root = Path(sys.executable).parent
    expected_path = build_windows.os.pathsep.join(
        map(str, [
            python_root,
            python_root / "DLLs",
            windows_root / "System32",
            windows_root,
        ])
    )

    def simulated_run():
        assert winutils.set_exe_build_timestamp is build_windows.set_exe_build_timestamp
        assert winutils.update_exe_pe_checksum is build_windows.update_exe_pe_checksum
        assert build_windows.os.environ["PATH"] == expected_path
        assert "external-poppler" not in build_windows.os.environ["PATH"]
        assert "external-tools" not in build_windows.os.environ["PATH"]
        if build_fails:
            raise RuntimeError("测试中的构建失败")

    monkeypatch.setattr(pyinstaller_main, "run", simulated_run)
    if build_fails:
        with pytest.raises(RuntimeError, match="测试中的构建失败"):
            build_windows.main()
    else:
        build_windows.main()

    assert winutils.set_exe_build_timestamp is previous_timestamp
    assert winutils.update_exe_pe_checksum is previous_checksum
    if inherited_path is None:
        assert "PATH" not in build_windows.os.environ
    else:
        assert build_windows.os.environ["PATH"] == inherited_path
