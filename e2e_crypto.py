"""端到端加密的密钥派生和版本化 AEAD 信封。"""

import base64
import binascii
import json
import os
from dataclasses import dataclass

from argon2.low_level import Type, hash_secret_raw
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


_ENVELOPE_VERSION = 1
_KEY_LENGTH = 32
_NONCE_LENGTH = 12
_HKDF_SALT = b"BeamChat/e2e/hkdf/v1"
_ARGON2_TIME_COST = 2
_ARGON2_MEMORY_COST = 65_536
_ARGON2_PARALLELISM = 1


class CryptoError(Exception):
    """表示密钥材料、信封格式或认证失败。"""


@dataclass(frozen=True)
class ScopeKeys:
    """同一作用域下按用途隔离的对称密钥。"""

    message_key: bytes
    file_key: bytes
    voice_key: bytes


def derive_x25519_root(
    private_key: X25519PrivateKey,
    peer_public: bytes,
    context: bytes = b"BeamChat/e2e/root/v1",
) -> bytes:
    """以 X25519 共享秘密和显式上下文派生 32 字节根密钥。"""
    if not isinstance(private_key, X25519PrivateKey) or not isinstance(peer_public, bytes):
        raise CryptoError("X25519 密钥材料无效")
    if not isinstance(context, bytes) or not context:
        raise CryptoError("根密钥上下文无效")
    try:
        shared_secret = private_key.exchange(X25519PublicKey.from_public_bytes(peer_public))
    except ValueError as exc:
        raise CryptoError("X25519 公钥无效") from exc
    return _hkdf(shared_secret, b"root\x00" + context)


def derive_scope_keys(root_key: bytes, scope_type: str, scope_id: str) -> ScopeKeys:
    """从根密钥派生消息、文件和语音三个互不相同的作用域密钥。"""
    root_key = _require_key(root_key)
    _require_text(scope_type, "作用域类型")
    _require_text(scope_id, "作用域标识")
    scope = _canonical_json({"scope_type": scope_type, "scope_id": scope_id})
    return ScopeKeys(
        message_key=_hkdf(root_key, b"scope\x00message\x00" + scope),
        file_key=_hkdf(root_key, b"scope\x00file\x00" + scope),
        voice_key=_hkdf(root_key, b"scope\x00voice\x00" + scope),
    )


def encrypt_envelope(key: bytes, plaintext: bytes, context: dict) -> dict:
    """使用 ChaCha20-Poly1305 加密为可 JSON 序列化的版本化信封。"""
    key = _require_key(key)
    if not isinstance(plaintext, bytes):
        raise CryptoError("明文必须是字节串")
    aad = _canonical_json(context)
    nonce = os.urandom(_NONCE_LENGTH)
    ciphertext = ChaCha20Poly1305(key).encrypt(nonce, plaintext, aad)
    return {
        "version": _ENVELOPE_VERSION,
        "nonce": _encode(nonce),
        "ciphertext": _encode(ciphertext),
    }


def decrypt_envelope(key: bytes, envelope: dict, context: dict) -> bytes:
    """验证并解密版本化信封，任何格式或认证错误均抛出 CryptoError。"""
    key = _require_key(key)
    aad = _canonical_json(context)
    try:
        if not isinstance(envelope, dict) or set(envelope) != {"version", "nonce", "ciphertext"}:
            raise ValueError
        if envelope["version"] != _ENVELOPE_VERSION:
            raise ValueError
        nonce = _decode(envelope["nonce"])
        ciphertext = _decode(envelope["ciphertext"])
        if len(nonce) != _NONCE_LENGTH:
            raise ValueError
        return ChaCha20Poly1305(key).decrypt(nonce, ciphertext, aad)
    except (InvalidTag, KeyError, TypeError, ValueError, UnicodeError) as exc:
        raise CryptoError("加密信封认证失败") from exc


def encrypt_dm_for_participants(
    plaintext: bytes,
    *,
    sender_identity_public: bytes,
    recipient_identity_public: bytes,
    sender_prekey_private: X25519PrivateKey,
    sender_prekey_public: bytes,
    recipient_prekey_public: bytes,
    scope_type: str,
    scope_id: str,
    message_id: str,
) -> dict:
    """为私聊双方分别包装随机内容密钥，并加密私聊内容。"""
    participants = _participants(sender_identity_public, recipient_identity_public)
    if not isinstance(sender_prekey_private, X25519PrivateKey):
        raise CryptoError("发送方预密钥无效")
    _require_public(sender_prekey_public, "发送方预密钥")
    if sender_prekey_public != _public_bytes(sender_prekey_private):
        raise CryptoError("发送方预密钥不匹配")
    base_context = _dm_context(participants, scope_type, scope_id, message_id)
    content_key = os.urandom(_KEY_LENGTH)
    content = encrypt_envelope(content_key, plaintext, base_context)
    recipients = (
        (sender_identity_public, sender_prekey_public),
        (recipient_identity_public, recipient_prekey_public),
    )
    wraps = []
    for identity_public, prekey_public in recipients:
        _require_public(identity_public, "身份公钥")
        _require_public(prekey_public, "预密钥")
        wrap_context = {
            **base_context,
            "recipient_identity_public": _encode(identity_public),
            "recipient_prekey_public": _encode(prekey_public),
            "sender_prekey_public": _encode(sender_prekey_public),
        }
        wrapping_key = derive_x25519_root(sender_prekey_private, prekey_public, b"BeamChat/dm/wrap/v1")
        wraps.append(
            {
                "recipient_identity_public": _encode(identity_public),
                "recipient_prekey_public": _encode(prekey_public),
                "sender_prekey_public": _encode(sender_prekey_public),
                "wrapped_key": encrypt_envelope(wrapping_key, content_key, wrap_context),
            }
        )
    return {"version": _ENVELOPE_VERSION, "content": content, "key_wraps": wraps}


def decrypt_dm_envelope(
    participant_prekey_private: X25519PrivateKey,
    participant_identity_public: bytes,
    peer_identity_public: bytes,
    envelope: dict,
    *,
    scope_type: str,
    scope_id: str,
    message_id: str,
) -> bytes:
    """使用参与者预密钥解开其内容密钥并验证私聊内容。"""
    if not isinstance(participant_prekey_private, X25519PrivateKey):
        raise CryptoError("参与者预密钥无效")
    _require_public(participant_identity_public, "身份公钥")
    _require_public(peer_identity_public, "对端身份公钥")
    try:
        if not isinstance(envelope, dict) or set(envelope) != {"version", "content", "key_wraps"}:
            raise ValueError
        if envelope["version"] != _ENVELOPE_VERSION or not isinstance(envelope["key_wraps"], list):
            raise ValueError
        selected = next(
            item
            for item in envelope["key_wraps"]
            if isinstance(item, dict)
            and item.get("recipient_identity_public") == _encode(participant_identity_public)
        )
        if set(selected) != {
            "recipient_identity_public",
            "recipient_prekey_public",
            "sender_prekey_public",
            "wrapped_key",
        }:
            raise ValueError
        recipient_prekey_public = _decode(selected["recipient_prekey_public"])
        sender_prekey_public = _decode(selected["sender_prekey_public"])
        _require_public(recipient_prekey_public, "预密钥")
        _require_public(sender_prekey_public, "发送方预密钥")
        if recipient_prekey_public != _public_bytes(participant_prekey_private):
            raise ValueError
    except (StopIteration, TypeError, ValueError, UnicodeError, binascii.Error) as exc:
        raise CryptoError("私聊密钥包装无效") from exc

    participants = _participants(participant_identity_public, peer_identity_public)
    base_context = _dm_context(participants, scope_type, scope_id, message_id)
    wrap_context = {
        **base_context,
        "recipient_identity_public": _encode(participant_identity_public),
        "recipient_prekey_public": _encode(recipient_prekey_public),
        "sender_prekey_public": _encode(sender_prekey_public),
    }
    wrapping_key = derive_x25519_root(
        participant_prekey_private, sender_prekey_public, b"BeamChat/dm/wrap/v1"
    )
    content_key = decrypt_envelope(wrapping_key, selected["wrapped_key"], wrap_context)
    return decrypt_envelope(content_key, envelope["content"], base_context)


def derive_room_root(room_id: str, password: str, salt: bytes) -> bytes:
    """使用 Argon2id 从房间标识、密码和盐派生房间根密钥。"""
    if not isinstance(room_id, str) or not room_id or not isinstance(password, str):
        raise CryptoError("房间密钥输入无效")
    if not isinstance(salt, bytes) or len(salt) < 8:
        raise CryptoError("房间盐值无效")
    return hash_secret_raw(
        secret=("BeamChat/room/v1\x00" + room_id + "\x00" + password).encode("utf-8"),
        salt=salt,
        time_cost=_ARGON2_TIME_COST,
        memory_cost=_ARGON2_MEMORY_COST,
        parallelism=_ARGON2_PARALLELISM,
        hash_len=_KEY_LENGTH,
        type=Type.ID,
    )


def _dm_context(participants: dict, scope_type: str, scope_id: str, message_id: str) -> dict:
    _require_text(scope_type, "作用域类型")
    _require_text(scope_id, "作用域标识")
    _require_text(message_id, "消息标识")
    return {
        "participants": participants,
        "scope_type": scope_type,
        "scope_id": scope_id,
        "message_id": message_id,
    }


def _participants(first: bytes, second: bytes) -> dict:
    _require_public(first, "身份公钥")
    _require_public(second, "身份公钥")
    encoded = sorted((_encode(first), _encode(second)))
    return {"first_identity_public": encoded[0], "second_identity_public": encoded[1]}


def _hkdf(material: bytes, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=_KEY_LENGTH, salt=_HKDF_SALT, info=info).derive(material)


def _require_key(key: bytes) -> bytes:
    if not isinstance(key, bytes) or len(key) != _KEY_LENGTH:
        raise CryptoError("对称密钥长度无效")
    return key


def _require_public(value: bytes, label: str) -> None:
    if not isinstance(value, bytes) or len(value) != 32:
        raise CryptoError(f"{label}长度无效")


def _require_text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value:
        raise CryptoError(f"{label}无效")


def _public_bytes(private_key: X25519PrivateKey) -> bytes:
    return private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def _canonical_json(value: dict) -> bytes:
    if not isinstance(value, dict):
        raise CryptoError("认证上下文无效")
    try:
        return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CryptoError("认证上下文无效") from exc


def _encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _decode(value: str) -> bytes:
    if not isinstance(value, str):
        raise ValueError
    try:
        return base64.b64decode(value, validate=True)
    except binascii.Error as exc:
        raise ValueError from exc
