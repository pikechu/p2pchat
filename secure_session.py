"""私聊端到端加密会话与 TOFU 信任状态。"""

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from enum import Enum

from e2e_crypto import CryptoError, decrypt_dm_envelope, derive_scope_keys, derive_x25519_root, encrypt_dm_for_participants
from identity import (
    DeviceIdentity,
    TrustDecision,
    TrustStore,
    fingerprint,
    verify_key_bundle,
)
from protocol import PROTOCOL_VERSION


class SessionState(Enum):
    """私聊会话当前是否可用于加密发送。"""

    READY = "ready"
    FROZEN = "frozen"
    UNAVAILABLE = "unavailable"


class SecureSessionError(Exception):
    """表示私聊会话不可用，调用方不得回退到明文路径。"""

    def __init__(self, state: SessionState, message: str):
        super().__init__(message)
        self.state = state


@dataclass(frozen=True)
class PeerIdentityChange:
    """用于界面显示的已检测身份变更。"""

    peer: str
    old_fingerprint: str
    new_fingerprint: str


class SecureSessionManager:
    """验证对端密钥包、维护 TOFU 信任并构造私聊加密帧。"""

    def __init__(self, identity: DeviceIdentity, trust_store: TrustStore, own_name: str = ""):
        self._identity = identity
        self._trust_store = trust_store
        self._own_name = own_name
        self._bundles: dict[str, dict] = {}
        self._states: dict[str, SessionState] = {}
        self._changes: dict[str, PeerIdentityChange] = {}

    def cache_peer_bundle(self, peer: str, key_bundle: dict) -> SessionState:
        """缓存已签名的对端公开密钥包并更新 TOFU 状态。"""
        peer = self._require_peer(peer)
        if not verify_key_bundle(key_bundle, PROTOCOL_VERSION):
            self._states[peer] = SessionState.UNAVAILABLE
            raise SecureSessionError(SessionState.UNAVAILABLE, "对端公开密钥包无效")
        identity_public = self._bundle_identity_public(key_bundle)
        decision = self._trust_store.observe(peer, identity_public)
        self._bundles[peer] = key_bundle
        if decision is TrustDecision.CHANGED:
            self._states[peer] = SessionState.FROZEN
            self._changes[peer] = PeerIdentityChange(
                peer=peer,
                old_fingerprint=self._trusted_fingerprint(peer),
                new_fingerprint=fingerprint(identity_public),
            )
        else:
            self._states[peer] = SessionState.READY
            self._changes.pop(peer, None)
        return self._states[peer]

    def ensure_peer(self, peer: str) -> SessionState:
        """返回对端会话状态；未取得密钥包时不可发送。"""
        peer = self._require_peer(peer)
        return self._states.get(peer, SessionState.UNAVAILABLE)

    def identity_change(self, peer: str) -> PeerIdentityChange | None:
        """返回待用户确认的身份变更信息。"""
        return self._changes.get(peer)

    def accept_peer(self, peer: str) -> SessionState:
        """显式接受已变化的身份密钥并恢复发送。"""
        peer = self._require_peer(peer)
        bundle = self._bundles.get(peer)
        if bundle is None:
            return SessionState.UNAVAILABLE
        self._trust_store.accept(peer, self._bundle_identity_public(bundle))
        self._states[peer] = SessionState.READY
        self._changes.pop(peer, None)
        return SessionState.READY

    def reject_peer(self, peer: str) -> SessionState:
        """拒绝身份变更并保持该对端会话冻结。"""
        peer = self._require_peer(peer)
        self._states[peer] = SessionState.FROZEN
        return SessionState.FROZEN

    def encrypt_dm(self, peer: str, text: str, client_msg_id: str) -> dict:
        """为私聊双方构造仅含密文和公开验证材料的发送载荷。"""
        peer = self._require_peer(peer)
        if self.ensure_peer(peer) is not SessionState.READY:
            raise SecureSessionError(self.ensure_peer(peer), "加密私聊密钥不可用")
        if not isinstance(text, str) or not text or not isinstance(client_msg_id, str) or not client_msg_id:
            raise SecureSessionError(SessionState.UNAVAILABLE, "加密私聊内容或消息标识无效")
        bundle = self._bundles[peer]
        peer_identity = self._bundle_identity_public(bundle)
        peer_prekey = self._decode_public(bundle, "prekey_public")
        scope_id = self.dm_scope_id(self._own_name, peer)
        envelope = encrypt_dm_for_participants(
            text.encode("utf-8"),
            sender_identity_public=self._identity.identity_public,
            recipient_identity_public=peer_identity,
            sender_prekey_private=self._identity.prekey_private,
            sender_prekey_public=self._identity.prekey_public,
            recipient_prekey_public=peer_prekey,
            scope_type="dm",
            scope_id=scope_id,
            message_id=client_msg_id,
        )
        return {
            "scope_type": "dm",
            "scope_id": scope_id,
            "to": peer,
            "client_msg_id": client_msg_id,
            "ciphertext": json.dumps(envelope, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
            "crypto_meta": {
                "alg": "DM-AEAD-v1",
                "sender_key_bundle": bundle_for_sender(self._identity, self._identity.prekey_public),
            },
        }

    def file_key(self, peer: str) -> tuple[bytes, str]:
        """返回与指定私聊对端绑定的文件 AEAD 密钥和 scope_id。"""
        peer = self._require_peer(peer)
        if self.ensure_peer(peer) is not SessionState.READY:
            raise SecureSessionError(self.ensure_peer(peer), "加密文件密钥不可用")
        bundle = self._bundles[peer]
        peer_prekey = self._decode_public(bundle, "prekey_public")
        scope_id = self.dm_scope_id(self._own_name, peer)
        root_key = derive_x25519_root(self._identity.prekey_private, peer_prekey, b"BeamChat/dm/file/v1")
        return derive_scope_keys(root_key, "dm", scope_id).file_key, scope_id

    def voice_key(self, peer: str) -> tuple[bytes, str]:
        """返回与指定私聊对端绑定的语音 AEAD 密钥和 scope_id。"""
        peer = self._require_peer(peer)
        if self.ensure_peer(peer) is not SessionState.READY:
            raise SecureSessionError(self.ensure_peer(peer), "加密语音密钥不可用")
        bundle = self._bundles[peer]
        peer_prekey = self._decode_public(bundle, "prekey_public")
        scope_id = self.dm_scope_id(self._own_name, peer)
        root_key = derive_x25519_root(self._identity.prekey_private, peer_prekey, b"BeamChat/dm/voice/v1")
        return derive_scope_keys(root_key, "dm", scope_id).voice_key, scope_id

    def decrypt_dm(self, message: dict) -> str:
        """验证并解密本人参与的私聊密文，失败时不返回未经认证内容。"""
        try:
            sender = str(message.get("sender_name") or message.get("from") or "")
            recipient = str(message.get("recipient_name") or message.get("to") or "")
            peer_name = recipient if sender == self._own_name else sender
            peer = self._require_peer(peer_name)
            client_msg_id = str(message["client_msg_id"])
            scope_id = str(message["scope_id"])
            metadata = message["crypto_meta"]
            bundle = metadata["sender_key_bundle"]
            envelope = json.loads(message["ciphertext"])
            peer_identity_public: bytes | None = None
            if sender != self._own_name:
                bundle_identity = self._bundle_identity_public(bundle)
                if not verify_key_bundle(bundle, PROTOCOL_VERSION):
                    raise SecureSessionError(SessionState.UNAVAILABLE, "加密私聊密钥不可用")
                if self._sender_prekey_from_envelope(envelope) != self._decode_public(bundle, "prekey_public"):
                    raise SecureSessionError(SessionState.UNAVAILABLE, "加密私聊密钥不可用")
                self.cache_peer_bundle(peer, bundle)
                peer_identity_public = bundle_identity
            if sender != self._own_name and self.ensure_peer(peer) is not SessionState.READY:
                raise SecureSessionError(SessionState.FROZEN, "加密私聊密钥不可用")
            if peer_identity_public is None:
                peer_identity_public = self._peer_identity_from_envelope(envelope)
            plaintext = decrypt_dm_envelope(
                self._identity.prekey_private,
                self._identity.identity_public,
                peer_identity_public,
                envelope,
                scope_type="dm",
                scope_id=scope_id,
                message_id=client_msg_id,
            )
            return plaintext.decode("utf-8")
        except SecureSessionError:
            raise
        except (CryptoError, KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise SecureSessionError(SessionState.UNAVAILABLE, "加密私聊密钥不可用") from exc

    @staticmethod
    def dm_scope_id(first: str, second: str) -> str:
        pair = "\x00".join(sorted([str(first), str(second)]))
        return hashlib.sha256(pair.encode("utf-8")).hexdigest()

    def _trusted_fingerprint(self, peer: str) -> str:
        try:
            records = self._trust_store._load()
            return fingerprint(base64.b64decode(records[peer], validate=True))
        except (AttributeError, KeyError, ValueError):
            return "未知"

    @staticmethod
    def _require_peer(peer: str) -> str:
        peer = str(peer).strip()
        if not peer:
            raise SecureSessionError(SessionState.UNAVAILABLE, "对端名称无效")
        return peer

    @staticmethod
    def _decode_public(bundle: dict, field: str) -> bytes:
        try:
            value = base64.b64decode(bundle[field], validate=True)
        except (KeyError, TypeError, ValueError) as exc:
            raise SecureSessionError(SessionState.UNAVAILABLE, "对端公开密钥包无效") from exc
        if len(value) != 32:
            raise SecureSessionError(SessionState.UNAVAILABLE, "对端公开密钥包无效")
        return value

    def _bundle_identity_public(self, bundle: dict) -> bytes:
        return self._decode_public(bundle, "identity_public")

    def _peer_identity_from_envelope(self, envelope: dict) -> bytes:
        try:
            for item in envelope["key_wraps"]:
                candidate = base64.b64decode(item["recipient_identity_public"], validate=True)
                if candidate != self._identity.identity_public:
                    if len(candidate) != 32:
                        raise ValueError
                    return candidate
        except (KeyError, TypeError, ValueError, binascii.Error) as exc:
            raise SecureSessionError(SessionState.UNAVAILABLE, "加密私聊密钥不可用") from exc
        raise SecureSessionError(SessionState.UNAVAILABLE, "加密私聊密钥不可用")

    @staticmethod
    def _sender_prekey_from_envelope(envelope: dict) -> bytes:
        try:
            values = {
                base64.b64decode(item["sender_prekey_public"], validate=True)
                for item in envelope["key_wraps"]
            }
        except (KeyError, TypeError, ValueError, binascii.Error) as exc:
            raise SecureSessionError(SessionState.UNAVAILABLE, "加密私聊密钥不可用") from exc
        if len(values) != 1:
            raise SecureSessionError(SessionState.UNAVAILABLE, "加密私聊密钥不可用")
        value = next(iter(values))
        if len(value) != 32:
            raise SecureSessionError(SessionState.UNAVAILABLE, "加密私聊密钥不可用")
        return value


def bundle_for_sender(identity: DeviceIdentity, ephemeral_public: bytes) -> dict:
    """构造随私聊携带、可离线验证的发送方公开密钥包。"""
    from identity import sign_key_bundle

    return identity.public_bundle(
        ephemeral_public,
        sign_key_bundle(identity, ephemeral_public, PROTOCOL_VERSION),
        PROTOCOL_VERSION,
    )
