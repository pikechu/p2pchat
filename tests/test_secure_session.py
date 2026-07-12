import asyncio

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from unittest.mock import AsyncMock, MagicMock

from client import ChatClient
from identity import DeviceIdentity, TrustStore, sign_key_bundle
from protocol import T
from protocol import PROTOCOL_VERSION
from secure_session import SecureSessionError, SecureSessionManager, SessionState


def _bundle(identity: DeviceIdentity) -> dict:
    ephemeral = X25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return identity.public_bundle(
        ephemeral,
        sign_key_bundle(identity, ephemeral, PROTOCOL_VERSION),
        PROTOCOL_VERSION,
    )


def _identity() -> DeviceIdentity:
    return DeviceIdentity(Ed25519PrivateKey.generate(), X25519PrivateKey.generate())


def test_first_peer_key_uses_tofu_and_encrypts_for_both_participants(tmp_path):
    alice = _identity()
    bob = _identity()
    manager = SecureSessionManager(alice, TrustStore(tmp_path / "trust.json"))

    manager.cache_peer_bundle("bob", _bundle(bob))

    assert manager.ensure_peer("bob") is SessionState.READY
    message = manager.encrypt_dm("bob", "只给鲍勃的消息", "m-1")

    assert message["scope_type"] == "dm"
    assert message["to"] == "bob"
    assert "只给鲍勃的消息" not in str(message)


def test_sender_can_decrypt_own_synced_dm(tmp_path):
    alice = _identity()
    bob = _identity()
    alice_manager = SecureSessionManager(alice, TrustStore(tmp_path / "alice-trust.json"), "alice")
    alice_manager.cache_peer_bundle("bob", _bundle(bob))

    outbound = alice_manager.encrypt_dm("bob", "自己同步也能解密", "m-own")
    stored = {
        "sender_name": "alice",
        "recipient_name": "bob",
        "client_msg_id": outbound["client_msg_id"],
        "scope_id": outbound["scope_id"],
        "ciphertext": outbound["ciphertext"],
        "crypto_meta": outbound["crypto_meta"],
    }

    assert alice_manager.decrypt_dm(stored) == "自己同步也能解密"


def test_inbound_dm_rejects_sender_bundle_mismatch(tmp_path):
    alice = _identity()
    bob = _identity()
    mallory = _identity()
    alice_manager = SecureSessionManager(alice, TrustStore(tmp_path / "alice-trust.json"), "alice")
    bob_trust_path = tmp_path / "bob-trust.json"
    bob_manager = SecureSessionManager(bob, TrustStore(bob_trust_path), "bob")
    alice_manager.cache_peer_bundle("bob", _bundle(bob))
    outbound = alice_manager.encrypt_dm("bob", "身份错绑不得解密", "m-bad")
    stored = {
        "sender_name": "alice",
        "recipient_name": "bob",
        "client_msg_id": outbound["client_msg_id"],
        "scope_id": outbound["scope_id"],
        "ciphertext": outbound["ciphertext"],
        "crypto_meta": {**outbound["crypto_meta"], "sender_key_bundle": _bundle(mallory)},
    }

    try:
        bob_manager.decrypt_dm(stored)
    except SecureSessionError as exc:
        assert exc.state is SessionState.UNAVAILABLE
    else:
        raise AssertionError("错绑 sender_key_bundle 的私聊不得解密")
    assert bob_manager.ensure_peer("alice") is SessionState.UNAVAILABLE
    assert not bob_trust_path.exists()


def test_changed_peer_key_freezes_until_explicitly_accepted(tmp_path):
    alice = _identity()
    original_bob = _identity()
    replacement_bob = _identity()
    manager = SecureSessionManager(alice, TrustStore(tmp_path / "trust.json"))

    manager.cache_peer_bundle("bob", _bundle(original_bob))
    assert manager.ensure_peer("bob") is SessionState.READY

    manager.cache_peer_bundle("bob", _bundle(replacement_bob))
    assert manager.ensure_peer("bob") is SessionState.FROZEN
    try:
        manager.encrypt_dm("bob", "不得发送", "m-2")
    except SecureSessionError as exc:
        assert exc.state is SessionState.FROZEN
    else:
        raise AssertionError("冻结会话不得生成私聊帧")

    assert manager.accept_peer("bob") is SessionState.READY
    assert manager.encrypt_dm("bob", "恢复发送", "m-3") is not None


def test_rejecting_changed_peer_key_keeps_session_frozen(tmp_path):
    alice = _identity()
    original_bob = _identity()
    replacement_bob = _identity()
    manager = SecureSessionManager(alice, TrustStore(tmp_path / "trust.json"))

    manager.cache_peer_bundle("bob", _bundle(original_bob))
    manager.ensure_peer("bob")
    manager.cache_peer_bundle("bob", _bundle(replacement_bob))
    assert manager.ensure_peer("bob") is SessionState.FROZEN

    assert manager.reject_peer("bob") is SessionState.FROZEN
    try:
        manager.encrypt_dm("bob", "仍不得发送", "m-4")
    except SecureSessionError as exc:
        assert exc.state is SessionState.FROZEN
    else:
        raise AssertionError("拒绝后的会话不得生成私聊帧")


def test_terminal_client_syncs_known_dm_peers():
    client = ChatClient.__new__(ChatClient)
    client._dm_peers = {"bob"}
    client._username = "alice"
    client._offsets = {"dm:scope-alice-bob": 7}
    client._secure_sessions = MagicMock()
    client._secure_sessions.dm_scope_id.return_value = "scope-alice-bob"
    client._offset_key = ChatClient._offset_key.__get__(client, ChatClient)
    client._send = AsyncMock()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(ChatClient._sync_dm_messages(client))
    finally:
        loop.close()

    client._send.assert_awaited_once_with(
        T.SYNC_MESSAGES,
        scopes=[{"scope_type": "dm", "scope_id": "scope-alice-bob", "after_message_id": 7}],
        limit=200,
    )
