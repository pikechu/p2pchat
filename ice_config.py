"""ICE server configuration for WebRTC connections."""

from __future__ import annotations

import json
import os
from typing import Any

DEFAULT_ICE_SERVERS = [{"urls": ["stun:stun.l.google.com:19302"]}]


def load_ice_servers(env: dict[str, str] | None = None) -> list[dict[str, Any]]:
    env = os.environ if env is None else env
    raw = str(env.get("BEAM_ICE_SERVERS", "")).strip()
    if not raw:
        return list(DEFAULT_ICE_SERVERS)

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = raw

    if isinstance(parsed, str):
        parsed = [{"urls": [parsed]}]
    elif isinstance(parsed, dict):
        parsed = [parsed]

    if not isinstance(parsed, list):
        return list(DEFAULT_ICE_SERVERS)

    servers = [_normalize_ice_server(item) for item in parsed]
    servers = [server for server in servers if server is not None]
    return servers or list(DEFAULT_ICE_SERVERS)


def _normalize_ice_server(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    urls = value.get("urls")
    if isinstance(urls, str):
        urls = [urls]
    if not isinstance(urls, list):
        return None
    urls = [str(url).strip() for url in urls if str(url).strip()]
    if not urls:
        return None

    server: dict[str, Any] = {"urls": urls}
    if "username" in value:
        server["username"] = str(value["username"])
    if "credential" in value:
        server["credential"] = str(value["credential"])
    return server
