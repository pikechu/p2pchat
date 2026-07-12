import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from build import load_build_config, write_client_build_config
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
