import json

from ice_config import load_ice_servers


def test_load_ice_servers_uses_default_stun_when_env_missing(monkeypatch):
    monkeypatch.delenv("BEAM_ICE_SERVERS", raising=False)

    servers = load_ice_servers()

    assert servers == [{"urls": ["stun:stun.l.google.com:19302"]}]


def test_load_ice_servers_parses_json_list(monkeypatch):
    monkeypatch.setenv("BEAM_ICE_SERVERS", json.dumps([
        {"urls": ["stun:stun.example.com:3478"]},
        {"urls": ["turn:turn.example.com:3478"], "username": "u", "credential": "p"},
    ]))

    servers = load_ice_servers()

    assert servers == [
        {"urls": ["stun:stun.example.com:3478"]},
        {"urls": ["turn:turn.example.com:3478"], "username": "u", "credential": "p"},
    ]


def test_load_ice_servers_accepts_single_url_string(monkeypatch):
    monkeypatch.setenv("BEAM_ICE_SERVERS", "stun:stun.example.com:3478")

    servers = load_ice_servers()

    assert servers == [{"urls": ["stun:stun.example.com:3478"]}]


def test_load_ice_servers_ignores_invalid_entries(monkeypatch):
    monkeypatch.setenv("BEAM_ICE_SERVERS", json.dumps([
        {"username": "missing-url"},
        {"urls": "turn:turn.example.com:3478", "username": "u", "credential": "p"},
        123,
    ]))

    servers = load_ice_servers()

    assert servers == [
        {"urls": ["turn:turn.example.com:3478"], "username": "u", "credential": "p"},
    ]
