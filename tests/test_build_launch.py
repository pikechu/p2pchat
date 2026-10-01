"""验证构建候选启动检查失败时不会覆盖已有正式产物。"""

from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import build


@pytest.fixture
def isolated_build(tmp_path, monkeypatch):
    """使用独立暂存目录和两份旧产物验证发布顺序。"""
    project = tmp_path / "project"
    project_dist = project / "dist"
    stage = tmp_path / "stage"
    published = tmp_path / "beam-build"
    project_dist.mkdir(parents=True)
    stage.mkdir()
    published.mkdir()
    old_project = project_dist / "BeamChat.exe"
    old_published = published / "BeamChat.exe"
    old_project.write_bytes(b"previous-project-executable")
    old_published.write_bytes(b"previous-published-executable")
    new_content = b"new-verified-executable"
    candidate = stage / "BeamChat.exe"
    monkeypatch.setattr(build, "ROOT", project)
    monkeypatch.setattr(build, "DIST", project_dist)
    monkeypatch.setattr(
        build, "sys", SimpleNamespace(platform="win32", executable=sys.executable)
    )
    monkeypatch.setenv("BEAM_PYINSTALLER_DIST_DIR", str(stage))
    monkeypatch.setenv("BEAM_BUILD_DIR", str(published))
    monkeypatch.setattr(build, "_ensure_pyinstaller", lambda: None)

    def compile_candidate(command, **kwargs):
        candidate.write_bytes(new_content)

    monkeypatch.setattr(build, "_run", compile_candidate)
    copies = []
    real_copy = shutil.copy2

    def record_copy(source, target, **kwargs):
        copies.append((Path(source), Path(target)))
        return real_copy(source, target, **kwargs)

    monkeypatch.setattr(build.shutil, "copy2", record_copy)
    return SimpleNamespace(
        project=project,
        project_dist=project_dist,
        stage=stage,
        candidate=candidate,
        old_project=old_project,
        old_published=old_published,
        new_content=new_content,
        copies=copies,
    )


@pytest.mark.parametrize("failure", ["nonzero", "timeout"])
def test_failed_launch_preserves_both_previous_artifacts(isolated_build, monkeypatch, failure):
    setup = isolated_build
    launches = []

    def failed_launch(command, **kwargs):
        launches.append(command)
        assert setup.candidate.read_bytes() == setup.new_content
        assert setup.old_project.read_bytes() == b"previous-project-executable"
        assert setup.old_published.read_bytes() == b"previous-published-executable"
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return subprocess.CompletedProcess(
            command, 1, stdout="", stderr="QtWidgets 依赖加载失败"
        )

    monkeypatch.setattr(
        build, "subprocess",
        SimpleNamespace(run=failed_launch, CREATE_NO_WINDOW=0x08000000),
    )
    if failure == "timeout":
        error = subprocess.TimeoutExpired
    else:
        error = RuntimeError
    with pytest.raises(error):
        build.build()

    assert launches == [[str(setup.candidate), "--help"]]
    assert setup.copies == []
    assert setup.old_project.read_bytes() == b"previous-project-executable"
    assert setup.old_published.read_bytes() == b"previous-published-executable"


def test_successful_launch_allows_both_artifact_copies(isolated_build, monkeypatch):
    setup = isolated_build
    launches = []

    def successful_launch(command, **kwargs):
        launches.append(command)
        assert setup.copies == []
        assert setup.old_project.read_bytes() == b"previous-project-executable"
        assert setup.old_published.read_bytes() == b"previous-published-executable"
        return subprocess.CompletedProcess(command, 0, stdout="BeamChat --help", stderr="")

    monkeypatch.setattr(
        build, "subprocess",
        SimpleNamespace(run=successful_launch, CREATE_NO_WINDOW=0x08000000),
    )

    build.build()

    assert launches == [[str(setup.candidate), "--help"]]
    assert setup.old_project.read_bytes() == setup.new_content
    assert setup.old_published.read_bytes() == setup.new_content
    assert len(setup.copies) == 2
    assert {target for _, target in setup.copies} == {
        setup.old_project, setup.old_published,
    }


def test_linux_build_does_not_launch_windows_smoke_check(isolated_build, monkeypatch):
    setup = isolated_build
    linux_candidate = setup.stage / "BeamChat"
    linux_target = setup.project_dist / "BeamChat"
    monkeypatch.setattr(
        build, "sys", SimpleNamespace(platform="linux", executable=sys.executable)
    )

    def compile_linux_candidate(command, **kwargs):
        linux_candidate.write_bytes(b"linux-application")

    def forbidden_launch(command, **kwargs):
        pytest.fail("Linux 构建不应执行 Windows EXE 启动检查")

    monkeypatch.setattr(build, "_run", compile_linux_candidate)
    monkeypatch.setattr(build, "subprocess", SimpleNamespace(run=forbidden_launch))

    build.build()

    assert linux_target.read_bytes() == b"linux-application"
    assert setup.old_project.read_bytes() == b"previous-project-executable"
    assert setup.old_published.read_bytes() == b"previous-published-executable"
    assert setup.copies == [(linux_candidate, linux_target)]
