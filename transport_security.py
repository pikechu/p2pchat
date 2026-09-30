"""WebSocket 传输地址的生产安全校验。"""

from urllib.parse import urlparse


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def validate_server_url(url: str) -> str:
    """公网仅允许 WSS；本机开发地址可显式使用 WS。"""
    value = str(url).strip()
    parsed = urlparse(value)
    if parsed.scheme == "wss" and parsed.hostname:
        return value
    if parsed.scheme == "ws" and parsed.hostname in _LOCAL_HOSTS:
        return value
    if parsed.scheme == "ws":
        raise ValueError("公网服务器必须使用 wss:// 加密连接；ws:// 仅允许本机开发环境")
    raise ValueError("服务器地址必须是有效的 wss:// 地址（本机开发可使用 ws://）")
