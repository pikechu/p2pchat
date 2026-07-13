import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from build import (
    copy_build_artifact,
    default_artifact_copy_dir,
    expected_executable_path,
    load_build_config,
    write_client_build_config,
)
from gui_client import load_default_server_url


def test_build_config_accepts_server_url(tmp_path):
    config_path = tmp_path / "config.prod.json"
    config_path.write_text(
        json.dumps({"server_url": "wss://example.com:8765"}),
        encoding="utf-8",
    )

    config = load_build_config(config_path)

    assert config["server_url"] == "wss://example.com:8765"


def test_gui_client_reads_packaged_default_server_url(tmp_path, monkeypatch):
    config_path = tmp_path / "beam_config.json"
    write_client_build_config(
        {"server_url": "wss://prod.example/ws", "allow_custom_server": False},
        config_path,
    )
    monkeypatch.setattr("gui_client._resource", lambda relative: str(config_path))

    assert load_default_server_url() == "wss://prod.example/ws"


def test_build_output_path_uses_platform_extension():
    assert expected_executable_path("win32").name == "BeamChat.exe"
    assert expected_executable_path("linux").name == "BeamChat"


def test_default_artifact_copy_dir_matches_beam_build():
    assert default_artifact_copy_dir("win32").as_posix() == "F:/beam-build"
    assert default_artifact_copy_dir("linux").as_posix() == "/mnt/f/beam-build"


def test_copy_build_artifact_copies_only_exe(tmp_path):
    exe = tmp_path / "BeamChat.exe"
    exe.write_bytes(b"exe")
    linux_binary = tmp_path / "BeamChat"
    linux_binary.write_bytes(b"elf")
    target_dir = tmp_path / "beam-build"

    copied = copy_build_artifact(exe, target_dir)

    assert copied == target_dir / "BeamChat.exe"
    assert copied.read_bytes() == b"exe"
    assert copy_build_artifact(linux_binary, target_dir) is None
