"""加密私聊在中继、持久化与离线同步中的端到端测试。"""

import asyncio
import socket
import sqlite3

import pytest
import websockets
import websockets.legacy.client as ws_connect
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from identity import DeviceIdentity, TrustStore, sign_key_bundle
from protocol import CLIENT_CAPABILITIES, CLIENT_VERSION, PROTOCOL_VERSION, T, pack, unpack
from secure_session import SecureSessionManager
from server import ChatServer


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _identity() -> DeviceIdentity:
    return DeviceIdentity(Ed25519PrivateKey.generate(), X25519PrivateKey.generate())


def _bundle(identity: DeviceIdentity) -> dict:
    ephemeral = X25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return identity.public_bundle(
        ephemeral,
        sign_key_bundle(identity, ephemeral, PROTOCOL_VERSION),
        PROTOCOL_VERSION,
    )


async def _ready(port: int, name: str, identity: DeviceIdentity):
    ws = await ws_connect.connect(f"ws://127.0.0.1:{port}", open_timeout=2, close_timeout=0.1)
    await ws.send(pack(
        T.CLIENT_HELLO,
        client_version=CLIENT_VERSION,
        protocol_version=PROTOCOL_VERSION,
        capabilities=CLIENT_CAPABILITIES,
        key_bundle=_bundle(identity),
    ))
    assert unpack(await ws.recv())["type"] == T.SERVER_HELLO
    await ws.send(pack(T.SET_NAME, name=name))
    assert unpack(await ws.recv())["type"] == T.READY
    return ws


async def _recv(ws, timeout=2):
    return unpack(await asyncio.wait_for(ws.recv(), timeout=timeout))


@pytest.fixture()
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


def test_encrypted_dm_is_relayed_persisted_and_only_participants_can_decrypt(tmp_path, event_loop):
    async def run():
        alice_identity, bob_identity, charlie_identity = _identity(), _identity(), _identity()
        alice = SecureSessionManager(alice_identity, TrustStore(tmp_path / "alice-trust.json"), "alice")
        bob = SecureSessionManager(bob_identity, TrustStore(tmp_path / "bob-trust.json"), "bob")
        charlie = SecureSessionManager(charlie_identity, TrustStore(tmp_path / "charlie-trust.json"), "charlie")
        alice.cache_peer_bundle("bob", _bundle(bob_identity))
        secret = "私聊明文绝不能出现在中继或数据库"
        server = ChatServer(message_db_path=tmp_path / "beam.db")
        async with websockets.serve(server.handle, "127.0.0.1", 0) as listening:
            port = listening.sockets[0].getsockname()[1]
            alice_ws = await _ready(port, "alice", alice_identity)
            bob_ws = await _ready(port, "bob", bob_identity)
            charlie_ws = await _ready(port, "charlie", charlie_identity)
            try:
                outbound = alice.encrypt_dm("bob", secret, "dm-1")
                await alice_ws.send(pack(T.SEND_ENCRYPTED_MSG, **outbound))
                received = await _recv(bob_ws)
                assert received["type"] == T.NEW_ENCRYPTED_MSG
                assert secret not in str(received)
                assert bob.decrypt_dm(received["payload"]) == secret
                ack = await _recv(alice_ws)
                assert ack["type"] == T.SEND_ACK

                with sqlite3.connect(tmp_path / "beam.db") as db:
                    stored = db.execute("SELECT ciphertext, crypto_meta FROM messages").fetchone()
                assert secret not in "".join(stored)

                await bob_ws.close()
                bob_ws = await _ready(port, "bob", bob_identity)
                await bob_ws.send(pack(T.SYNC_MESSAGES, scopes=[{
                    "scope_type": "dm", "scope_id": outbound["scope_id"], "after_message_id": 0,
                }], limit=20))
                synced = await _recv(bob_ws)
                assert synced["type"] == T.SYNC_MESSAGES_RESULT
                assert bob.decrypt_dm(synced["payload"]["messages"][0]) == secret

                await alice_ws.send(pack(T.SYNC_MESSAGES, scopes=[{
                    "scope_type": "dm", "scope_id": outbound["scope_id"], "after_message_id": 0,
                }], limit=20))
                own_sync = await _recv(alice_ws)
                assert own_sync["type"] == T.SYNC_MESSAGES_RESULT
                assert alice.decrypt_dm(own_sync["payload"]["messages"][0]) == secret

                await charlie_ws.send(pack(T.SYNC_MESSAGES, scopes=[{
                    "scope_type": "dm", "scope_id": outbound["scope_id"], "after_message_id": 0,
                }], limit=20))
                foreign_sync = await _recv(charlie_ws)
                assert foreign_sync["payload"]["messages"] == []
                assert charlie.ensure_peer("alice").value == "unavailable"
            finally:
                await alice_ws.close()
                await bob_ws.close()
                await charlie_ws.close()

    event_loop.run_until_complete(run())
