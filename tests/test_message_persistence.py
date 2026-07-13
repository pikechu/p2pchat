import asyncio
import os
import socket
import sqlite3
import subprocess
import sys
import time
import uuid

import pytest
import websockets
import websockets.legacy.client as ws_connect
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from protocol import CLIENT_CAPABILITIES, CLIENT_VERSION, PROTOCOL_VERSION, T, TTL_VALUES, pack, unpack
from identity import DeviceIdentity, sign_key_bundle
from server import ChatServer
from crypto import create_room_access_metadata, encode_room_envelope, encrypt_room_message


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


async def _connect(port: int, name: str):
    ws = await ws_connect.connect(f"ws://127.0.0.1:{port}")
    identity = DeviceIdentity(Ed25519PrivateKey.generate(), X25519PrivateKey.generate())
    ephemeral = X25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    await ws.send(pack(
        T.CLIENT_HELLO,
        client_version=CLIENT_VERSION,
        protocol_version=PROTOCOL_VERSION,
        capabilities=CLIENT_CAPABILITIES,
        key_bundle=identity.public_bundle(
            ephemeral, sign_key_bundle(identity, ephemeral, PROTOCOL_VERSION), PROTOCOL_VERSION
        ),
    ))
    assert unpack(await ws.recv())["type"] == T.SERVER_HELLO
    await ws.send(pack(T.SET_NAME, name=name))
    assert unpack(await ws.recv())["type"] == T.READY
    return ws


async def _recv(ws, timeout=3) -> dict:
    return unpack(await asyncio.wait_for(ws.recv(), timeout=timeout))


async def _create_room(ws, name: str, password: str = "持久化密码") -> tuple[str, object]:
    room_id = uuid.uuid4().hex.upper().translate(str.maketrans({"0": "A", "1": "B", "I": "C", "L": "D", "O": "E"}))[:6]
    metadata = create_room_access_metadata(room_id, password)
    await ws.send(pack(T.CREATE_ROOM, room_id=room_id, name=name, **dict(metadata)))
    frame = await _recv(ws)
    assert frame["type"] == T.ROOM_CREATED
    return room_id, metadata


async def _join_room(ws, room_id: str, metadata, password: str = "持久化密码") -> None:
    from crypto import decrypt_room_access_token
    await ws.send(pack(T.JOIN_ROOM, room_id=room_id,
                       access_token=decrypt_room_access_token(room_id, password, metadata)))


async def _send_room_message(ws, room_id: str, metadata, plaintext: str, client_msg_id: str) -> None:
    ciphertext = encode_room_envelope(encrypt_room_message(
        room_id, "持久化密码", plaintext, client_msg_id, metadata["salt"]
    ))
    await ws.send(pack(T.SEND_ENCRYPTED_MSG, scope_type="room", scope_id=room_id,
                       ciphertext=ciphertext, client_msg_id=client_msg_id,
                       crypto_meta={"alg": "ChaCha20-Poly1305", "version": 1}))


def test_store_encrypted_message_assigns_id_and_honors_ttl(tmp_path):
    server = ChatServer(message_db_path=tmp_path / "beam.db", min_message_ttl_seconds=1)
    msg = server._store_encrypted_message(
        scope_type="room",
        scope_id="ABC123",
        sender_name="alice",
        client_msg_id="m1",
        msg_type="text",
        ciphertext="ciphertext-only",
        crypto_meta={"alg": "Fernet"},
        ttl_seconds=10,
        now=1000,
    )

    assert msg["message_id"] == 1
    assert msg["expires_at"] == 1010

    with sqlite3.connect(tmp_path / "beam.db") as db:
        row = db.execute(
            "SELECT scope_type, scope_id, sender_name, ciphertext, crypto_meta, expires_at FROM messages"
        ).fetchone()
    assert row[0:4] == ("room", "ABC123", "alice", "ciphertext-only")
    assert "Fernet" in row[4]
    assert row[5] == 1010


def test_scope_ttl_settings_support_year_and_permanent(tmp_path):
    server = ChatServer(message_db_path=tmp_path / "beam.db", min_message_ttl_seconds=1)
    year = TTL_VALUES["year"]

    assert server._set_scope_ttl("room", "ROOM01", year) == year
    assert server._scope_ttl("room", "ROOM01") == year

    dm_id = server._dm_scope_id("alice", "bob")
    assert server._set_scope_ttl("dm", dm_id, 0, username="alice") == TTL_VALUES["week"]
    assert server._set_scope_ttl("dm", dm_id, 0, username="bob") == 0
    assert server._scope_ttl("dm", dm_id) == 0

    permanent = server._store_encrypted_message(
        scope_type="dm",
        scope_id=dm_id,
        sender_name="alice",
        recipient_name="bob",
        client_msg_id="forever",
        msg_type="text",
        ciphertext="ciphertext-only",
        crypto_meta={},
        ttl_seconds=server._scope_ttl("dm", dm_id),
        now=1000,
    )
    assert permanent["expires_at"] is None


def test_ttl_policy_accepts_only_five_values(tmp_path):
    server = ChatServer(message_db_path=tmp_path / "beam.db", min_message_ttl_seconds=1)
    for ttl in TTL_VALUES.values():
        assert server._set_scope_ttl("room", "ROOM01", ttl) == ttl

    with pytest.raises(ValueError):
        server._set_scope_ttl("room", "ROOM01", 123)


def test_dm_ttl_uses_shorter_request_and_permanent_requires_both(tmp_path):
    server = ChatServer(message_db_path=tmp_path / "beam.db", min_message_ttl_seconds=1)
    dm_id = server._dm_scope_id("alice", "bob")

    assert server._set_scope_ttl("dm", dm_id, TTL_VALUES["year"], username="alice") == TTL_VALUES["week"]
    assert server._set_scope_ttl("dm", dm_id, TTL_VALUES["month"], username="bob") == TTL_VALUES["month"]
    assert server._set_scope_ttl("dm", dm_id, TTL_VALUES["day"], username="alice") == TTL_VALUES["day"]
    assert server._set_scope_ttl("dm", dm_id, TTL_VALUES["permanent"], username="alice") == TTL_VALUES["month"]
    assert server._set_scope_ttl("dm", dm_id, TTL_VALUES["permanent"], username="bob") == TTL_VALUES["permanent"]


def test_shrinking_ttl_marks_old_messages_deleted(tmp_path):
    server = ChatServer(message_db_path=tmp_path / "beam.db", min_message_ttl_seconds=1)
    server._store_encrypted_message(
        scope_type="room",
        scope_id="ROOM01",
        sender_name="alice",
        client_msg_id="old",
        msg_type="text",
        ciphertext="old-cipher",
        crypto_meta={},
        ttl_seconds=TTL_VALUES["year"],
        now=1000,
    )

    server._set_scope_ttl("room", "ROOM01", TTL_VALUES["day"], now=1000 + 2 * TTL_VALUES["day"])

    assert server._load_messages_for_sync(
        [{"scope_type": "room", "scope_id": "ROOM01", "after_message_id": 0}],
        now=1000 + 2 * TTL_VALUES["day"],
    ) == []
    with sqlite3.connect(tmp_path / "beam.db") as db:
        deleted_at = db.execute("SELECT deleted_at FROM messages WHERE client_msg_id = 'old'").fetchone()[0]
    assert deleted_at == 1000 + 2 * TTL_VALUES["day"]


def test_unencrypted_messages_are_not_persisted(tmp_path):
    server = ChatServer(message_db_path=tmp_path / "beam.db", min_message_ttl_seconds=1)
    assert server._maybe_persist_room_message(
        room_id="ABC123",
        sender_name="alice",
        payload={"text": "plain text", "encrypted": False},
        now=1000,
    ) is None

    with sqlite3.connect(tmp_path / "beam.db") as db:
        count = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert count == 0


def test_expired_messages_are_not_synced(tmp_path):
    server = ChatServer(message_db_path=tmp_path / "beam.db", min_message_ttl_seconds=1)
    server._store_encrypted_message(
        scope_type="room",
        scope_id="ABC123",
        sender_name="alice",
        client_msg_id="expired",
        msg_type="text",
        ciphertext="old",
        crypto_meta={},
        ttl_seconds=1,
        now=1000,
    )
    server._store_encrypted_message(
        scope_type="room",
        scope_id="ABC123",
        sender_name="alice",
        client_msg_id="fresh",
        msg_type="text",
        ciphertext="new",
        crypto_meta={},
        ttl_seconds=100,
        now=1000,
    )

    messages = server._load_messages_for_sync(
        [{"scope_type": "room", "scope_id": "ABC123", "after_message_id": 0}],
        limit=100,
        now=1002,
    )

    assert [m["client_msg_id"] for m in messages] == ["fresh"]


def test_initial_room_history_sync_returns_latest_limited_messages(tmp_path):
    server = ChatServer(message_db_path=tmp_path / "beam.db", min_message_ttl_seconds=1)
    for index in range(3):
        server._store_encrypted_message(
            scope_type="room",
            scope_id="ROOM01",
            sender_name="alice",
            client_msg_id=f"m{index}",
            msg_type="text",
            ciphertext=f"cipher-{index}",
            crypto_meta={},
            ttl_seconds=1000,
            now=100 + index,
        )

    messages = server._load_messages_for_sync(
        [{"scope_type": "room", "scope_id": "ROOM01", "after_message_id": 0}],
        limit=2,
        now=200,
    )

    assert [m["client_msg_id"] for m in messages] == ["m1", "m2"]


def test_dm_sync_only_returns_messages_for_requester(tmp_path):
    server = ChatServer(message_db_path=tmp_path / "beam.db")
    dm_id = server._dm_scope_id("alice", "bob")
    server._store_encrypted_message(
        scope_type="dm",
        scope_id=dm_id,
        sender_name="alice",
        recipient_name="bob",
        client_msg_id="dm1",
        msg_type="text",
        ciphertext="dm-cipher",
        crypto_meta={},
        ttl_seconds=100,
        now=1000,
    )

    scope = [{"scope_type": "dm", "scope_id": dm_id, "after_message_id": 0}]

    assert [m["client_msg_id"] for m in server._load_messages_for_sync(scope, requester="bob", now=1001)] == ["dm1"]
    assert server._load_messages_for_sync(scope, requester="mallory", now=1001) == []


def test_encrypted_dm_rejects_mismatched_scope(tmp_path, event_loop):
    async def run():
        port = _free_port()
        proc = subprocess.Popen(
            [
                sys.executable,
                "server.py",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--message-db",
                str(tmp_path / "beam.db"),
            ],
            cwd=os.path.join(os.path.dirname(__file__), ".."),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        ws = None
        try:
            time.sleep(0.8)
            ws = await _connect(port, "mallory")
            await ws.send(pack(
                T.SEND_ENCRYPTED_MSG,
                scope_type="dm",
                scope_id=ChatServer._dm_scope_id("alice", "bob"),
                to="bob",
                ciphertext="cipher",
                crypto_meta={"alg": "test"},
            ))
            frame = await _recv(ws)
            assert frame["type"] == T.ERROR
            assert "scope_id" in frame["payload"]["message"]
        finally:
            if ws is not None:
                await ws.close()
            proc.terminate()
            proc.wait()

    event_loop.run_until_complete(run())


def test_room_ttl_only_creator_can_update_and_invalid_is_rejected(tmp_path, event_loop):
    async def run():
        port = _free_port()
        proc = subprocess.Popen(
            [
                sys.executable,
                "server.py",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--message-db",
                str(tmp_path / "beam.db"),
            ],
            cwd=os.path.join(os.path.dirname(__file__), ".."),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        alice = bob = None
        try:
            time.sleep(0.8)
            alice = await _connect(port, "ttl_alice")
            bob = await _connect(port, "ttl_bob")
            room_id, metadata = await _create_room(alice, "ttl-room")
            await _join_room(bob, room_id, metadata)
            assert (await _recv(bob))["type"] == T.ROOM_JOINED
            assert (await _recv(alice))["type"] == T.USER_JOINED

            await bob.send(pack(T.SET_MESSAGE_TTL, scope_type="room", scope_id=room_id,
                                ttl_seconds=TTL_VALUES["day"]))
            frame = await _recv(bob)
            assert frame["type"] == T.ERROR
            assert frame["payload"]["code"] == "FORBIDDEN"

            await alice.send(pack(T.SET_MESSAGE_TTL, scope_type="room", scope_id=room_id,
                                  ttl_seconds=123))
            frame = await _recv(alice)
            assert frame["type"] == T.ERROR
            assert frame["payload"]["code"] == "INVALID_TTL"

            await alice.send(pack(T.SET_MESSAGE_TTL, scope_type="room", scope_id=room_id,
                                  ttl_seconds=TTL_VALUES["day"]))
            frame = await _recv(alice)
            assert frame["type"] == T.MESSAGE_TTL_UPDATED
            assert frame["payload"]["ttl_seconds"] == TTL_VALUES["day"]
            frame = await _recv(bob)
            assert frame["type"] == T.MESSAGE_TTL_UPDATED
            assert frame["payload"]["ttl_seconds"] == TTL_VALUES["day"]
        finally:
            for ws in (alice, bob):
                if ws is not None:
                    await ws.close()
            proc.terminate()
            proc.wait()

    event_loop.run_until_complete(run())


def test_ttl_setting_reports_persistence_disabled(tmp_path, event_loop):
    async def run():
        port = _free_port()
        proc = subprocess.Popen(
            [
                sys.executable,
                "server.py",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--no-message-persistence",
            ],
            cwd=os.path.join(os.path.dirname(__file__), ".."),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        alice = None
        try:
            time.sleep(0.8)
            alice = await _connect(port, "ttl_disabled")
            room_id, _metadata = await _create_room(alice, "ttl-disabled")
            await alice.send(pack(T.GET_MESSAGE_TTL, scope_type="room", scope_id=room_id))
            frame = await _recv(alice)
            assert frame["type"] == T.ERROR
            assert frame["payload"]["code"] == "PERSISTENCE_DISABLED"
        finally:
            if alice is not None:
                await alice.close()
            proc.terminate()
            proc.wait()

    event_loop.run_until_complete(run())


def test_plaintext_dm_is_rejected_with_nonrecoverable_error(tmp_path, event_loop):
    async def run():
        server = ChatServer(message_db_path=tmp_path / "beam.db")
        async with websockets.serve(server.handle, "127.0.0.1", 0) as listening:
            ws = await _connect(listening.sockets[0].getsockname()[1], "plaintext_sender")
            try:
                await ws.send(pack(T.SEND_DM, to="nobody", text="不能发送明文", client_mid="plain-1"))
                frame = await _recv(ws)
                assert frame["type"] == T.ERROR
                assert frame["payload"]["code"] == "PLAINTEXT_FORBIDDEN"
                assert frame["payload"]["recoverable"] is False
            finally:
                await ws.close()

    event_loop.run_until_complete(run())


def test_protocol_incompatible_returns_structured_error(tmp_path, event_loop):
    async def run():
        port = _free_port()
        proc = subprocess.Popen(
            [
                sys.executable,
                "server.py",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--message-db",
                str(tmp_path / "beam.db"),
            ],
            cwd=os.path.join(os.path.dirname(__file__), ".."),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        ws = None
        try:
            time.sleep(0.8)
            ws = await ws_connect.connect(f"ws://127.0.0.1:{port}")
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(ws.recv(), timeout=0.1)
            await ws.send(pack(
                T.CLIENT_HELLO,
                client_version="99.0.0",
                protocol_version=PROTOCOL_VERSION + 1,
                capabilities=[],
            ))
            frame = await _recv(ws)
            assert frame["type"] == T.ERROR
            assert frame["payload"]["code"] == "PROTOCOL_INCOMPATIBLE"
            assert frame["payload"]["recoverable"] is False
        finally:
            if ws is not None:
                await ws.close()
            proc.terminate()
            proc.wait()

    event_loop.run_until_complete(run())


def test_sender_ack_offset_prevents_own_message_resync(tmp_path, event_loop):
    async def run():
        port = _free_port()
        db_path = tmp_path / "beam.db"
        proc = subprocess.Popen(
            [
                sys.executable,
                "server.py",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--message-db",
                str(db_path),
            ],
            cwd=os.path.join(os.path.dirname(__file__), ".."),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        alice = None
        try:
            time.sleep(0.8)
            alice = await _connect(port, "sender_alice")
            room_id, metadata = await _create_room(alice, "Sender Offset")
            await _send_room_message(alice, room_id, metadata, "cipher-from-sender", "sender-mid-1")
            ack = await _recv(alice)
            assert ack["type"] == T.SEND_ACK
            message_id = ack["payload"]["message_id"]

            await alice.send(pack(
                T.SYNC_MESSAGES,
                scopes=[{
                    "scope_type": "room",
                    "scope_id": room_id,
                    "after_message_id": message_id,
                }],
                limit=20,
            ))
            synced = await _recv(alice)
            assert synced["type"] == T.SYNC_MESSAGES_RESULT
            assert synced["payload"]["messages"] == []
        finally:
            if alice is not None:
                await alice.close()
            proc.terminate()
            proc.wait()

    event_loop.run_until_complete(run())


def test_room_message_sync_after_receiver_rejoins(tmp_path, event_loop):
    async def run():
        port = _free_port()
        db_path = tmp_path / "beam.db"
        alice = None
        bob = None
        bob2 = None
        proc = subprocess.Popen(
            [
                sys.executable,
                "server.py",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--message-db",
                str(db_path),
            ],
            cwd=os.path.join(os.path.dirname(__file__), ".."),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            time.sleep(0.8)
            alice = await _connect(port, "persist_alice")
            room_id, metadata = await _create_room(alice, "Persisted")

            bob = await _connect(port, "persist_bob")
            await _join_room(bob, room_id, metadata)
            await _recv(bob)
            await _recv(alice)
            await bob.close()
            await _recv(alice)

            await _send_room_message(alice, room_id, metadata, "ciphertext-value", "offline-1")
            ack = await _recv(alice)
            assert ack["type"] == T.SEND_ACK
            assert ack["payload"]["message_id"] >= 1

            bob2 = await _connect(port, "persist_bob")
            await _join_room(bob2, room_id, metadata)
            await _recv(bob2)
            await _recv(alice)

            await bob2.send(pack(
                T.SYNC_MESSAGES,
                scopes=[{"scope_type": "room", "scope_id": room_id, "after_message_id": 0}],
                limit=20,
            ))
            synced = await _recv(bob2)
            assert synced["type"] == T.SYNC_MESSAGES_RESULT
            assert "ciphertext-value" not in synced["payload"]["messages"][0]["ciphertext"]
            assert synced["payload"]["messages"][0]["sender_name"] == "persist_alice"
        finally:
            for ws in (alice, bob, bob2):
                if ws is not None:
                    await ws.close()
            proc.terminate()
            proc.wait()

    event_loop.run_until_complete(run())
