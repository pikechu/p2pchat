"""房间访问令牌、AEAD 消息信封和旧 Fernet 历史读取兼容。"""

import base64
import hashlib
import json
import os
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from e2e_crypto import CryptoError, decrypt_envelope, derive_room_root, derive_scope_keys, encrypt_envelope

_SALT = b"p2pchat::v1::salt"


class RoomAccessMetadata(dict):
    """可发送的房间元数据与仅本地保留的访问令牌。"""

    def __init__(self, payload: dict, access_token: str):
        super().__init__(payload)
        self.access_token = access_token


def create_room_access_metadata(room_id: str, password: str) -> RoomAccessMetadata:
    """创建房间所需的公开元数据，访问令牌只保留在本地对象中。"""
    salt = os.urandom(16)
    root_key = derive_room_root(room_id, password, salt)
    access_token = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
    encrypted_access_token = encrypt_envelope(
        root_key,
        access_token.encode("utf-8"),
        {"scope_type": "room", "scope_id": room_id, "purpose": "access_token"},
    )
    return RoomAccessMetadata(
        {
            "salt": base64.b64encode(salt).decode("ascii"),
            "encrypted_access_token": encrypted_access_token,
            "access_token_hash": hashlib.sha256(access_token.encode("utf-8")).hexdigest(),
        },
        access_token,
    )


def decrypt_room_access_token(room_id: str, password: str, metadata: dict) -> str:
    """使用房间密码解开客户端元数据中的访问令牌。"""
    try:
        salt = base64.b64decode(metadata["salt"].encode("ascii"), validate=True)
        root_key = derive_room_root(room_id, password, salt)
        token = decrypt_envelope(
            root_key,
            metadata["encrypted_access_token"],
            {"scope_type": "room", "scope_id": room_id, "purpose": "access_token"},
        ).decode("utf-8")
        if not token:
            raise ValueError
        return token
    except (CryptoError, KeyError, TypeError, ValueError, UnicodeError) as exc:
        raise CryptoError("房间访问令牌认证失败") from exc


def encrypt_room_message(room_id: str, password: str, plaintext: str, message_id: str, salt: str) -> dict:
    """使用房间消息作用域密钥生成 AEAD 信封。"""
    if not isinstance(plaintext, str):
        raise CryptoError("房间消息无效")
    try:
        root_key = derive_room_root(room_id, password, base64.b64decode(salt.encode("ascii"), validate=True))
    except (ValueError, UnicodeError) as exc:
        raise CryptoError("房间盐值无效") from exc
    key = derive_scope_keys(root_key, "room", room_id).message_key
    return encrypt_envelope(key, plaintext.encode("utf-8"), _room_message_context(room_id, message_id))


def decrypt_room_message(room_id: str, password: str, envelope: dict, message_id: str, salt: str) -> str:
    """验证并解密房间消息；认证失败使用房间专用错误。"""
    try:
        root_key = derive_room_root(room_id, password, base64.b64decode(salt.encode("ascii"), validate=True))
        key = derive_scope_keys(root_key, "room", room_id).message_key
        return decrypt_envelope(key, envelope, _room_message_context(room_id, message_id)).decode("utf-8")
    except (CryptoError, ValueError, UnicodeError) as exc:
        raise CryptoError("房间消息认证失败") from exc


def encode_room_envelope(envelope: dict) -> str:
    """将 AEAD 信封编码为可由中继持久化的 JSON 字符串。"""
    return json.dumps(envelope, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def decode_room_envelope(ciphertext: str) -> dict:
    """解析中继返回的房间 AEAD 信封。"""
    try:
        envelope = json.loads(ciphertext)
    except (TypeError, ValueError) as exc:
        raise CryptoError("房间消息信封无效") from exc
    if not isinstance(envelope, dict):
        raise CryptoError("房间消息信封无效")
    return envelope


def _room_message_context(room_id: str, message_id: str) -> dict:
    return {"scope_type": "room", "scope_id": room_id, "message_id": str(message_id)}


def derive_key(room_id: str, password: str = "") -> bytes:
    """Return a Fernet-ready 32-byte URL-safe base64 key."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_SALT + room_id.encode(),
        iterations=200_000,
    )
    raw = kdf.derive((room_id + password).encode())
    return base64.urlsafe_b64encode(raw)


def encrypt(key: bytes, plaintext: str) -> str:
    return Fernet(key).encrypt(plaintext.encode()).decode()


def decrypt(key: bytes, ciphertext: str) -> str | None:
    """解密旧版 Fernet 历史消息，新消息应使用 encrypt_envelope。"""
    try:
        return Fernet(key).decrypt(ciphertext.encode()).decode()
    except (InvalidToken, Exception):
        return None
