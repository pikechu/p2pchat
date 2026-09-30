"""用设备身份保护本地群凭证、消息缓存和同步游标。"""

import hashlib
import json
import os
import pathlib
import tempfile
import time

from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

from e2e_crypto import CryptoError, decrypt_envelope, derive_scope_keys, encrypt_envelope
from identity import DeviceIdentity
from protocol import TTL_VALUES


MAX_CACHED_ROOM_MESSAGES = 1000
MAX_CACHE_BYTES = 32 * 1024 * 1024


class EncryptedRoomState:
    """不同设备与服务器的缓存分别认证，文件内不保存明文聊天内容或密码。"""

    def __init__(self, directory: pathlib.Path, identity: DeviceIdentity, server_url: str):
        server_id = hashlib.sha256(server_url.encode("utf-8")).hexdigest()
        self.path = pathlib.Path(directory) / f"rooms-{server_id[:16]}.json"
        self._context = {"purpose": "BeamChat/local-room-state/v1", "server_id": server_id}
        private_seed = identity.identity_private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        self._key = derive_scope_keys(private_seed, "local-cache", server_id).message_key

    def load(self) -> dict:
        """不存在或认证失败的缓存按空状态处理，不使用未验证的游标。"""
        try:
            if self.path.stat().st_size > MAX_CACHE_BYTES:
                return {}
            envelope = json.loads(self.path.read_text(encoding="utf-8"))
            state = json.loads(decrypt_envelope(self._key, envelope, self._context))
            return state if isinstance(state, dict) else {}
        except (OSError, ValueError, TypeError, CryptoError):
            return {}

    def save(self, state: dict) -> None:
        """以原子替换保存认证密文，避免中断写入损坏历史和游标。"""
        encoded = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_CACHE_BYTES * 3 // 4 - 1024:
            raise ValueError("群缓存超过文件大小上限")
        envelope = encrypt_envelope(self._key, encoded, self._context)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile("w", dir=self.path.parent, encoding="utf-8", delete=False) as stream:
                temporary_path = pathlib.Path(stream.name)
                json.dump(envelope, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


def retained_room_messages(messages: list[dict], ttl_seconds: int, now: float | None = None) -> list[dict]:
    """本地仅保留有效期内的最近消息，按服务端序号排列并限制缓存数量。"""
    now = time.time() if now is None else now
    retained = [
        message for message in messages
        if isinstance(message, dict)
        and (ttl_seconds == TTL_VALUES["permanent"] or float(message.get("ts", 0)) + ttl_seconds > now)
    ]
    retained.sort(key=lambda message: (
        int(message.get("message_id", 0)) if int(message.get("message_id", 0)) > 0 else float("inf"),
        float(message.get("ts", 0)),
    ))
    return retained[-MAX_CACHED_ROOM_MESSAGES:]
