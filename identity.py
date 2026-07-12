import base64
import binascii
import hashlib
import hmac
import json
import os
import struct
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat


KEY_BUNDLE_PROTOCOL_VERSION = 1
_PREKEY_PURPOSE = b"prekey"
_EPHEMERAL_PURPOSE = b"ephemeral"
_SIGNATURE_CONTEXT = b"BeamChat/key-bundle"


class TrustDecision(Enum):
    NEW = "new"
    TRUSTED = "trusted"
    CHANGED = "changed"


@dataclass
class DeviceIdentity:
    identity_private: Ed25519PrivateKey
    prekey_private: X25519PrivateKey

    @property
    def identity_public(self) -> bytes:
        return self.identity_private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    @property
    def prekey_public(self) -> bytes:
        return self.prekey_private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    def public_bundle(
        self,
        ephemeral_public: bytes,
        signature: bytes,
        protocol_version: int = KEY_BUNDLE_PROTOCOL_VERSION,
    ) -> dict:
        """返回仅含公开密钥材料和签名的密钥包。"""
        _require_x25519_public(ephemeral_public)
        if not isinstance(signature, bytes) or len(signature) != 64:
            raise ValueError("临时密钥签名无效")
        version = _require_protocol_version(protocol_version)
        prekey_signature = self.identity_private.sign(
            _signature_payload(_PREKEY_PURPOSE, self.identity_public, self.prekey_public, version)
        )
        return {
            "identity_public": _encode(self.identity_public),
            "prekey_public": _encode(self.prekey_public),
            "prekey_signature": _encode(prekey_signature),
            "ephemeral_public": _encode(ephemeral_public),
            "ephemeral_signature": _encode(signature),
        }


class IdentityStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load_or_create(self) -> DeviceIdentity:
        if not self.path.exists():
            identity = DeviceIdentity(Ed25519PrivateKey.generate(), X25519PrivateKey.generate())
            self._write(identity)
            return identity
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
            identity_private = Ed25519PrivateKey.from_private_bytes(
                _decode(stored["identity_private"], "身份文件")
            )
            prekey_private = X25519PrivateKey.from_private_bytes(
                _decode(stored["prekey_private"], "身份文件")
            )
        except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError, binascii.Error) as exc:
            raise ValueError("身份文件格式无效") from exc
        return DeviceIdentity(identity_private, prekey_private)

    def _write(self, identity: DeviceIdentity) -> None:
        _atomic_write_json(
            self.path,
            {
                "identity_private": _encode(
                    identity.identity_private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
                ),
                "prekey_private": _encode(
                    identity.prekey_private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
                ),
            },
        )


def fingerprint(public_key: bytes) -> str:
    """将完整 Ed25519 公钥摘要格式化为人工可核验的指纹。"""
    digest = hashlib.sha256(public_key).hexdigest().upper()
    return " ".join(digest[index:index + 4] for index in range(0, len(digest), 4))


def sign_key_bundle(identity: DeviceIdentity, ephemeral_public: bytes, protocol_version: int) -> bytes:
    _require_x25519_public(ephemeral_public)
    version = _require_protocol_version(protocol_version)
    return identity.identity_private.sign(
        _signature_payload(_EPHEMERAL_PURPOSE, identity.identity_public, ephemeral_public, version)
    )


def verify_key_bundle(bundle: dict, protocol_version: int) -> bool:
    try:
        version = _require_protocol_version(protocol_version)
        if set(bundle) != {
            "identity_public",
            "prekey_public",
            "prekey_signature",
            "ephemeral_public",
            "ephemeral_signature",
        }:
            return False
        identity_public = _decode(bundle["identity_public"], "密钥包")
        prekey_public = _decode(bundle["prekey_public"], "密钥包")
        prekey_signature = _decode(bundle["prekey_signature"], "密钥包")
        ephemeral_public = _decode(bundle["ephemeral_public"], "密钥包")
        ephemeral_signature = _decode(bundle["ephemeral_signature"], "密钥包")
        if len(identity_public) != 32 or len(prekey_public) != 32 or len(ephemeral_public) != 32:
            return False
        if len(prekey_signature) != 64 or len(ephemeral_signature) != 64:
            return False
        verifier = Ed25519PublicKey.from_public_bytes(identity_public)
        verifier.verify(
            prekey_signature,
            _signature_payload(_PREKEY_PURPOSE, identity_public, prekey_public, version),
        )
        verifier.verify(
            ephemeral_signature,
            _signature_payload(_EPHEMERAL_PURPOSE, identity_public, ephemeral_public, version),
        )
    except (InvalidSignature, ValueError, TypeError, KeyError, binascii.Error):
        return False
    return True


class TrustStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def observe(self, peer_name: str, identity_public: bytes) -> TrustDecision:
        records = self._load()
        _require_identity_public(identity_public)
        encoded = _encode(identity_public)
        previous = records.get(peer_name)
        if previous is None:
            records[peer_name] = encoded
            _atomic_write_json(self.path, records)
            return TrustDecision.NEW
        try:
            previous_key = _decode(previous, "信任库")
        except (TypeError, ValueError, binascii.Error) as exc:
            raise ValueError("信任库文件格式无效") from exc
        if hmac.compare_digest(previous_key, identity_public):
            return TrustDecision.TRUSTED
        return TrustDecision.CHANGED

    def accept(self, peer_name: str, identity_public: bytes) -> None:
        records = self._load()
        _require_identity_public(identity_public)
        records[peer_name] = _encode(identity_public)
        _atomic_write_json(self.path, records)

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            records = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("信任库文件已损坏") from exc
        if not isinstance(records, dict) or not all(
            isinstance(name, str) and isinstance(value, str) for name, value in records.items()
        ):
            raise ValueError("信任库文件格式无效")
        try:
            if any(len(_decode(value, "信任库")) != 32 for value in records.values()):
                raise ValueError("信任库文件格式无效")
        except (ValueError, binascii.Error) as exc:
            raise ValueError("信任库文件格式无效") from exc
        return records


def _signature_payload(
    purpose: bytes,
    identity_public: bytes,
    signed_public_key: bytes,
    protocol_version: int,
) -> bytes:
    return b"".join(
        (
            _SIGNATURE_CONTEXT,
            struct.pack(">H", len(purpose)),
            purpose,
            struct.pack(">I", protocol_version),
            struct.pack(">I", len(identity_public)),
            identity_public,
            struct.pack(">I", len(signed_public_key)),
            signed_public_key,
        )
    )


def _require_protocol_version(protocol_version: int) -> int:
    if not isinstance(protocol_version, int) or not 0 <= protocol_version <= 0xFFFFFFFF:
        raise ValueError("协议版本无效")
    return protocol_version


def _require_x25519_public(public_key: bytes) -> None:
    if not isinstance(public_key, bytes) or len(public_key) != 32:
        raise ValueError("X25519 公钥长度无效")


def _require_identity_public(public_key: bytes) -> None:
    if not isinstance(public_key, bytes) or len(public_key) != 32:
        raise ValueError("Ed25519 公钥长度无效")


def _encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _decode(value: str, source: str) -> bytes:
    if not isinstance(value, str):
        raise ValueError(f"{source}编码无效")
    return base64.b64decode(value, validate=True)


def _atomic_write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(value, sort_keys=True, separators=(",", ":")))
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "posix":
            os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
        if os.name == "posix":
            os.chmod(path, 0o600)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
