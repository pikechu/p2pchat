"""验证瘦身后的构建入口与官方 Qt 依赖收集仍保留客户端所需功能。"""

from pathlib import Path
import runpy
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import build


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_build_command_uses_project_hooks_and_retains_runtime_dependencies(tmp_path, monkeypatch):
    """从真实构建入口捕获命令，验证用户 hook 会被交给 PyInstaller。"""
    project = tmp_path / "project"
    project.mkdir()
    output = tmp_path / "assembly"
    output.mkdir()
    copied = tmp_path / "published"
    monkeypatch.setattr(build, "ROOT", project)
    monkeypatch.setattr(build, "DIST", project / "dist")
    monkeypatch.setenv("BEAM_PYINSTALLER_DIST_DIR", str(output))
    monkeypatch.setenv("BEAM_BUILD_DIR", str(copied))
    monkeypatch.setattr(build, "_ensure_pyinstaller", lambda: None)
    monkeypatch.setattr(build, "verify_windows_executable", lambda path: None)
    calls = []

    def assemble(cmd, **kwargs):
        calls.append((cmd, kwargs))
        (output / build.expected_executable_path().name).write_bytes(b"fixture-package")

    monkeypatch.setattr(build, "_run", assemble)
    build.build()

    assert len(calls) == 1
    cmd, kwargs = calls[0]
    assert kwargs["cwd"] == project
    assert f"--additional-hooks-dir={project / 'build_hooks'}" in cmd
    assert "--collect-all=PyQt6" not in cmd
    if sys.platform == "win32":
        assert cmd[:2] == [sys.executable, str(project / "build_windows.py")]
    hidden = {cmd[index + 1] for index, arg in enumerate(cmd) if arg == "--hidden-import"}
    assert {"PyQt6.QtCore", "PyQt6.QtGui", "PyQt6.QtWidgets", "numpy", "sounddevice", "_sounddevice_data"} <= hidden
    excluded = {cmd[index + 1] for index, arg in enumerate(cmd) if arg == "--exclude-module"}
    assert {"numpy.testing", "numpy.f2py"} <= excluded
    runtime_modules = {
        "numpy", "numpy._core", "numpy.linalg", "numpy.fft", "numpy.random",
        "av", "av.audio", "av.video", "aiortc", "aiortc.rtcpeerconnection",
        "sounddevice", "_sounddevice_data", "cryptography",
    }
    for module in runtime_modules:
        assert not any(module == prefix or module.startswith(prefix + ".") for prefix in excluded)
    assert build.expected_executable_path().read_bytes() == b"fixture-package"
    if sys.platform == "win32":
        assert (copied / "BeamChat.exe").read_bytes() == b"fixture-package"


@pytest.fixture
def qt_hooks(monkeypatch):
    pytest.importorskip("PyInstaller")
    pytest.importorskip("pefile")
    pytest.importorskip("PyQt6.QtCore")
    from PyInstaller.config import CONF
    from PyInstaller.utils.hooks import qt

    if qt.pyqt6_library_info.version is None:
        pytest.skip("当前工具链无法收集 PyQt6 运行库")
    monkeypatch.setitem(CONF, "_seen_qt_bindings", "PyQt6")
    return qt


@pytest.mark.skipif(sys.platform != "win32", reason="验证 Windows 实际平台插件与 DLL")
@pytest.mark.parametrize("module", ["QtCore", "QtGui", "QtWidgets"])
def test_real_qt_hooks_preserve_dependencies_and_image_plugins(qt_hooks, module):
    """对真实官方依赖收集结果执行用户 hook，避免只测试虚构资源列表。"""
    hook = PROJECT_ROOT / "build_hooks" / f"hook-PyQt6.{module}.py"
    official_hidden, official_binaries, official_datas = qt_hooks.add_qt6_dependencies(str(hook))
    collected = runpy.run_path(str(hook))
    binaries = collected["binaries"]
    datas = collected["datas"]

    assert set(collected["hiddenimports"]) == set(official_hidden)
    assert set(binaries) <= set(official_binaries)
    removed_names = {Path(source).name.lower() for source, _ in set(official_binaries) - set(binaries)}
    assert removed_names <= {"opengl32sw.dll", "qpdf.dll", "qtuiotouchplugin.dll"}
    remaining_names = {Path(source).name.lower() for source, _ in binaries}
    assert not remaining_names & {"qpdf.dll", "qtuiotouchplugin.dll", "opengl32sw.dll"}
    if module == "QtGui":
        assert {"qwindows.dll", "qoffscreen.dll", "qgif.dll", "qico.dll", "qjpeg.dll", "qsvg.dll", "qwebp.dll"} <= remaining_names
    if module == "QtWidgets":
        assert "qmodernwindowsstyle.dll" in remaining_names
    assert set(datas) <= set(official_datas)
    non_translation_datas = {entry for entry in official_datas if not Path(entry[0]).name.lower().endswith(".qm")}
    assert non_translation_datas <= set(datas)
    supported_translations = {
        entry for entry in official_datas
        if Path(entry[0]).name.lower().endswith(("_zh_cn.qm", "_zh_tw.qm", "_en.qm", "_en_us.qm", "_en_gb.qm"))
    }
    assert supported_translations <= set(datas)


@pytest.mark.skipif(sys.platform != "win32", reason="验证 Windows 实际额外 Qt 运行库")
def test_base_qt_hook_keeps_official_imports_and_non_opengl_libraries(qt_hooks):
    import PyInstaller

    official_hook = Path(PyInstaller.__file__).parent / "hooks" / "hook-PyQt6.py"
    official = runpy.run_path(str(official_hook))
    project = runpy.run_path(str(PROJECT_ROOT / "build_hooks" / "hook-PyQt6.py"))
    assert set(project["hiddenimports"]) == set(official["hiddenimports"])
    required = {entry for entry in official["binaries"] if Path(entry[0]).name.lower() != "opengl32sw.dll"}
    assert set(project["binaries"]) == required
