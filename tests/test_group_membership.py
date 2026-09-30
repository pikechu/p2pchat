import asyncio

import pytest
import websockets
import websockets.legacy.client as ws_connect
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

import server as server_module
from crypto import create_room_access_metadata, encode_room_envelope, encrypt_room_message
from file_transfer import EncryptedFileSender
from identity import DeviceIdentity, sign_key_bundle
from protocol import CLIENT_CAPABILITIES, CLIENT_VERSION, PROTOCOL_VERSION, T, pack, unpack
from server import ChatServer


@pytest.fixture
def chat_server(tmp_path, monkeypatch):
    monkeypatch.setattr(server_module, "_ROOMS_FILE", tmp_path / "rooms.json")
    return ChatServer(message_db_path=tmp_path / "messages.db")


def _identity():
    return DeviceIdentity(Ed25519PrivateKey.generate(), X25519PrivateKey.generate())


async def _connect(port, username, identity):
    ws = await ws_connect.connect(f"ws://127.0.0.1:{port}")
    ephemeral = X25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    await ws.send(pack(
        T.CLIENT_HELLO, client_version=CLIENT_VERSION, protocol_version=PROTOCOL_VERSION,
        capabilities=CLIENT_CAPABILITIES,
        key_bundle=identity.public_bundle(
            ephemeral, sign_key_bundle(identity, ephemeral, PROTOCOL_VERSION), PROTOCOL_VERSION,
        ),
    ))
    assert (await _recv(ws))["type"] == T.SERVER_HELLO
    await ws.send(pack(T.SET_NAME, name=username))
    assert (await _recv(ws))["type"] == T.READY
    return ws


async def _recv(ws):
    return unpack(await asyncio.wait_for(ws.recv(), timeout=3))


async def _create(ws, room_id):
    metadata = create_room_access_metadata(room_id, "群测试密码")
    await ws.send(pack(T.CREATE_ROOM, room_id=room_id, name=room_id, **dict(metadata)))
    assert (await _recv(ws))["type"] == T.ROOM_CREATED
    return metadata


async def _join(ws, room_id, metadata):
    await ws.send(pack(T.JOIN_ROOM, room_id=room_id, access_token=metadata.access_token))
    assert (await _recv(ws))["type"] == T.ROOM_JOINED


async def _send(ws, room_id, metadata, client_msg_id):
    ciphertext = encode_room_envelope(encrypt_room_message(
        room_id, "群测试密码", client_msg_id, client_msg_id, metadata["salt"],
    ))
    await ws.send(pack(
        T.SEND_ENCRYPTED_MSG, scope_type="room", scope_id=room_id,
        ciphertext=ciphertext, client_msg_id=client_msg_id,
        crypto_meta={"alg": "ChaCha20-Poly1305", "version": 1},
    ))
    ack = await _recv(ws)
    assert ack["type"] == T.SEND_ACK
    return ack["payload"]["message_id"]


def test_multiple_joined_groups_receive_in_background_and_leave_is_scoped(chat_server):
    async def run():
        async with websockets.serve(chat_server.handle, "127.0.0.1", 0) as listening:
            port = listening.sockets[0].getsockname()[1]
            alice = await _connect(port, "alice", _identity())
            bob = await _connect(port, "bob", _identity())
            try:
                first = await _create(alice, "ABCDEF")
                await _join(bob, "ABCDEF", first)
                assert (await _recv(alice))["type"] == T.USER_JOINED
                second = await _create(alice, "GHJKMN")
                await _join(bob, "GHJKMN", second)
                assert (await _recv(alice))["type"] == T.USER_JOINED
                assert set(chat_server._rooms["ABCDEF"].members) == {"alice", "bob"}
                await _send(alice, "ABCDEF", first, "背景群消息")
                background = await _recv(bob)
                assert background["type"] == T.NEW_ENCRYPTED_MSG
                assert background["payload"]["scope_id"] == "ABCDEF"

                await bob.send(pack(T.LEAVE_ROOM, room_id="ABCDEF"))
                left = await _recv(bob)
                assert left["type"] == T.ROOM_LEFT
                assert left["payload"]["room_id"] == "ABCDEF"
                assert (await _recv(alice))["type"] == T.USER_LEFT
                assert "bob" not in chat_server._rooms["ABCDEF"].member_identities
                assert "bob" in chat_server._rooms["GHJKMN"].members
                await _send(alice, "GHJKMN", second, "保留群消息")
                assert (await _recv(bob))["payload"]["scope_id"] == "GHJKMN"
                await bob.send(pack(T.SYNC_MESSAGES, scopes=[{
                    "scope_type": "room", "scope_id": "ABCDEF", "history_mode": "all",
                }]))
                assert (await _recv(bob))["payload"]["messages"] == []

                await _join(bob, "ABCDEF", first)
                assert (await _recv(alice))["type"] == T.USER_JOINED
                await alice.send(pack(T.DELETE_ROOM, room_id="GHJKMN"))
                for member in [alice, bob]:
                    deleted = [await _recv(member), await _recv(member)]
                    assert {frame["type"] for frame in deleted} == {T.ROOM_LEFT, T.ROOM_DELETED}
                    assert all(frame["payload"]["room_id"] == "GHJKMN" for frame in deleted)
                assert set(chat_server._rooms["ABCDEF"].members) == {"alice", "bob"}
                assert chat_server._user_rooms["bob"] == {"ABCDEF"}
                await _send(alice, "ABCDEF", first, "删群后其他群仍正常")
                assert (await _recv(bob))["payload"]["scope_id"] == "ABCDEF"
            finally:
                await bob.close()
                await alice.close()

    asyncio.run(run())


def test_reconnect_restores_all_groups_and_sync_pages_every_offline_message(chat_server):
    async def run():
        async with websockets.serve(chat_server.handle, "127.0.0.1", 0) as listening:
            port = listening.sockets[0].getsockname()[1]
            bob_identity = _identity()
            alice = await _connect(port, "alice", _identity())
            bob = await _connect(port, "bob", bob_identity)
            try:
                metadata = {}
                for room_id in ["ABCDEF", "GHJKMN"]:
                    metadata[room_id] = await _create(alice, room_id)
                    await _join(bob, room_id, metadata[room_id])
                    assert (await _recv(alice))["type"] == T.USER_JOINED
                await bob.close()
                for _ in metadata:
                    assert (await _recv(alice))["type"] == T.USER_LEFT
                assert all("bob" in room.member_identities for room in chat_server._rooms.values())

                expected = []
                for index in range(5):
                    for room_id, room_metadata in metadata.items():
                        expected.append(await _send(alice, room_id, room_metadata, f"离线{room_id}-{index}"))
                bob = await _connect(port, "bob", bob_identity)
                for _ in metadata:
                    assert (await _recv(alice))["type"] == T.USER_JOINED
                assert all("bob" in room.members for room in chat_server._rooms.values())

                scopes = [{
                    "scope_type": "room", "scope_id": room_id,
                    "after_message_id": 0, "history_mode": "all",
                } for room_id in metadata]
                received = []
                while scopes:
                    await bob.send(pack(T.SYNC_MESSAGES, scopes=scopes, limit=3))
                    response = await _recv(bob)
                    assert response["type"] == T.SYNC_MESSAGES_RESULT
                    page = response["payload"]
                    ids = [message["message_id"] for message in page["messages"]]
                    assert ids == sorted(ids)
                    received.extend(ids)
                    assert page["has_more"] == bool(page["next_scopes"])
                    scopes = page["next_scopes"]
                assert sorted(received) == expected
                assert len(received) == len(set(received))
            finally:
                await bob.close()
                await alice.close()

    asyncio.run(run())


def test_saved_membership_restores_only_same_device_and_leave_survives_restart(chat_server):
    async def run():
        async with websockets.serve(chat_server.handle, "127.0.0.1", 0) as listening:
            port = listening.sockets[0].getsockname()[1]
            identity = _identity()
            alice = await _connect(port, "alice", identity)
            metadata = await _create(alice, "ABCDEF")
            await _send(alice, "ABCDEF", metadata, "只允许原设备补收")
            await _create(alice, "GHJKMN")
            await alice.send(pack(T.LEAVE_ROOM, room_id="GHJKMN"))
            assert (await _recv(alice))["payload"]["room_id"] == "GHJKMN"
            await alice.close()
            restored = ChatServer(message_db_path=chat_server._message_db_path)
            assert restored._user_rooms["alice"] == {"ABCDEF"}
            assert not restored._rooms["GHJKMN"].member_identities

        async with websockets.serve(restored.handle, "127.0.0.1", 0) as listening:
            port = listening.sockets[0].getsockname()[1]
            impostor = await _connect(port, "alice", _identity())
            try:
                assert "alice" not in restored._rooms["ABCDEF"].members
                await impostor.send(pack(T.SYNC_MESSAGES, scopes=[{
                    "scope_type": "room", "scope_id": "ABCDEF", "history_mode": "all",
                }]))
                assert (await _recv(impostor))["payload"]["messages"] == []
            finally:
                await impostor.close()
            alice = await _connect(port, "alice", identity)
            try:
                await alice.send(pack(T.LIST_ROOMS))
                assert (await _recv(alice))["type"] == T.ROOM_LIST
                assert "alice" in restored._rooms["ABCDEF"].members
                assert "alice" not in restored._rooms["GHJKMN"].members
                await alice.send(pack(T.SYNC_MESSAGES, scopes=[{
                    "scope_type": "room", "scope_id": "ABCDEF", "history_mode": "all",
                }]))
                assert len((await _recv(alice))["payload"]["messages"]) == 1
            finally:
                await alice.close()

    asyncio.run(run())


def test_same_device_takeover_keeps_new_connection_in_every_group(chat_server):
    async def run():
        async with websockets.serve(chat_server.handle, "127.0.0.1", 0) as listening:
            port = listening.sockets[0].getsockname()[1]
            bob_identity = _identity()
            alice = await _connect(port, "alice", _identity())
            bob = await _connect(port, "bob", bob_identity)
            bob2 = None
            try:
                metadata = {}
                for room_id in ["ABCDEF", "GHJKMN"]:
                    metadata[room_id] = await _create(alice, room_id)
                    await _join(bob, room_id, metadata[room_id])
                    assert (await _recv(alice))["type"] == T.USER_JOINED
                bob2 = await _connect(port, "bob", bob_identity)
                transitions = [await _recv(alice) for _ in range(4)]
                assert sum(frame["type"] == T.USER_LEFT for frame in transitions) == 2
                assert sum(frame["type"] == T.USER_JOINED for frame in transitions) == 2
                await bob.wait_closed()
                await _join(bob2, "ABCDEF", metadata["ABCDEF"])
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(alice.recv(), timeout=0.05)
                await _send(bob2, "GHJKMN", metadata["GHJKMN"], "新连接保持后台群权限")
                assert (await _recv(alice))["payload"]["scope_id"] == "GHJKMN"
                assert all(room.members["bob"] is chat_server._name_to_ws["bob"]
                           for room in chat_server._rooms.values())
            finally:
                if bob2 is not None:
                    await bob2.close()
                await bob.close()
                await alice.close()

    asyncio.run(run())


def test_time_cursor_includes_equal_second_messages_and_id_cursor_takes_precedence(chat_server):
    for index, created_at in enumerate([1000, 1001, 1001, 1002]):
        chat_server._store_encrypted_message(
            scope_type="room", scope_id="ABCDEF", sender_name="alice",
            client_msg_id=f"m{index}", ciphertext="密文", crypto_meta={},
            now=created_at,
        )
    messages = chat_server._load_messages_for_sync([{
        "scope_type": "room", "scope_id": "ABCDEF", "after_created_at": 1001,
    }], now=1003)
    assert [message["message_id"] for message in messages] == [2, 3, 4]
    messages = chat_server._load_messages_for_sync([{
        "scope_type": "room", "scope_id": "ABCDEF", "after_message_id": 2,
        "after_created_at": 9999,
    }], now=1003)
    assert [message["message_id"] for message in messages] == [3, 4]


@pytest.mark.parametrize("done_before_leave", [False, True])
def test_receiver_leave_preserves_other_group_and_unblocks_file_completion(chat_server, tmp_path, done_before_leave):
    """接收者退群后不再等待其回执，其他群传输继续等待正常确认。"""
    async def run():
        async with websockets.serve(chat_server.handle, "127.0.0.1", 0) as listening:
            port = listening.sockets[0].getsockname()[1]
            alice = await _connect(port, "alice", _identity())
            bob = await _connect(port, "bob", _identity())
            senders = {}
            try:
                source = tmp_path / "正常群文件.txt"
                source.write_bytes(b"normal file")
                for room_id in ("ABCDEF", "GHJKMN"):
                    metadata = await _create(alice, room_id)
                    await _join(bob, room_id, metadata)
                    assert (await _recv(alice))["type"] == T.USER_JOINED
                    sender = EncryptedFileSender(
                        source, b"S" * 32, transfer_id=room_id,
                        scope_type="room", scope_id=room_id, sender="alice",
                    )
                    senders[room_id] = sender
                    await alice.send(pack(
                        T.FILE_ROOM_SHARE, room_id=room_id, transfer_id=room_id,
                        **sender.offer_payload(),
                    ))
                    assert (await _recv(bob))["type"] == T.FILE_ROOM_SHARE
                    chunk = sender.next_payload()
                    await alice.send(pack(
                        T.FILE_ROOM_CHUNK, transfer_id=room_id, index=chunk["index"],
                        total=chunk["total"], encrypted_chunk=chunk["encrypted_chunk"],
                    ))
                    assert (await _recv(alice))["type"] == T.FILE_ROOM_CHUNK_ACK
                    assert (await _recv(bob))["type"] == T.FILE_ROOM_CHUNK
                    if room_id == "GHJKMN" or done_before_leave:
                        await alice.send(pack(T.FILE_ROOM_DONE, transfer_id=room_id, **sender.done_payload()))
                        assert (await _recv(bob))["type"] == T.FILE_ROOM_DONE

                await bob.send(pack(T.LEAVE_ROOM, room_id="ABCDEF"))
                assert (await _recv(bob))["payload"]["room_id"] == "ABCDEF"
                assert (await _recv(alice))["type"] == T.USER_LEFT
                assert chat_server._user_rooms["bob"] == {"GHJKMN"}
                assert "bob" in chat_server._rooms["GHJKMN"].members
                assert chat_server._transfer_meta["GHJKMN"]["pending_receivers"] == {"bob"}
                if not done_before_leave:
                    assert not chat_server._transfer_meta["ABCDEF"]["pending_receivers"]
                    await alice.send(pack(
                        T.FILE_ROOM_DONE, transfer_id="ABCDEF", **senders["ABCDEF"].done_payload(),
                    ))
                completion = await _recv(alice)
                assert completion["type"] == T.FILE_ROOM_DONE_ACK
                assert completion["payload"]["transfer_id"] == "ABCDEF"
                assert "ABCDEF" not in chat_server._transfer_meta
                await bob.send(pack(T.FILE_ROOM_RECEIVED, transfer_id="GHJKMN"))
                completion = await _recv(alice)
                assert completion["type"] == T.FILE_ROOM_DONE_ACK
                assert completion["payload"]["transfer_id"] == "GHJKMN"
            finally:
                for sender in senders.values():
                    sender.close()
                await bob.close()
                await alice.close()

    asyncio.run(run())


def test_rename_to_offline_member_name_preserves_both_identities(chat_server):
    """正常改名遇到同群离线成员名称时拒绝覆盖，双方原成员关系均可恢复。"""
    async def run():
        async with websockets.serve(chat_server.handle, "127.0.0.1", 0) as listening:
            port = listening.sockets[0].getsockname()[1]
            bob_identity = _identity()
            alice = await _connect(port, "alice", _identity())
            bob = await _connect(port, "bob", bob_identity)
            try:
                metadata = await _create(alice, "ABCDEF")
                await _join(bob, "ABCDEF", metadata)
                assert (await _recv(alice))["type"] == T.USER_JOINED
                other_metadata = await _create(alice, "GHJKMN")
                await bob.close()
                assert (await _recv(alice))["type"] == T.USER_LEFT
                saved_rooms = server_module._ROOMS_FILE.read_bytes()
                saved_members = dict(chat_server._rooms["ABCDEF"].member_identities)
                await alice.send(pack(T.SET_NAME, name="bob"))
                rejected = await _recv(alice)
                assert rejected["type"] == T.ERROR
                assert rejected["payload"]["code"] == "USERNAME_IDENTITY_MISMATCH"
                assert rejected["payload"]["recoverable"] is True
                assert chat_server._rooms["ABCDEF"].member_identities == saved_members
                assert server_module._ROOMS_FILE.read_bytes() == saved_rooms
                assert set(chat_server._name_to_ws) == {"alice"}
                assert chat_server._user_rooms["alice"] == {"ABCDEF", "GHJKMN"}
                assert chat_server._rooms["ABCDEF"].creator == "alice"
                await _send(alice, "GHJKMN", other_metadata, "拒绝后连接仍可用")
                bob = await _connect(port, "bob", bob_identity)
                assert (await _recv(alice))["type"] == T.USER_JOINED
                assert "bob" in chat_server._rooms["ABCDEF"].members
                await alice.send(pack(T.SET_NAME, name="alice-renamed"))
                assert (await _recv(bob))["type"] == T.USER_LEFT
                assert (await _recv(bob))["type"] == T.USER_JOINED
                assert (await _recv(alice))["payload"]["name"] == "alice-renamed"
                restored = ChatServer(message_db_path=chat_server._message_db_path)
                assert restored._user_rooms["bob"] == {"ABCDEF"}
                assert restored._user_rooms["alice-renamed"] == {"ABCDEF", "GHJKMN"}
                assert restored._rooms["ABCDEF"].member_identities["bob"] == saved_members["bob"]
            finally:
                await bob.close()
                await alice.close()

    asyncio.run(run())


@pytest.mark.parametrize("room_operation", ["create", "delete"])
def test_rename_completes_during_other_users_room_changes(chat_server, monkeypatch, room_operation):
    """改名通知等待网络发送时，其他用户正常建群或删群不会打断成员迁移。"""
    async def run():
        async with websockets.serve(chat_server.handle, "127.0.0.1", 0) as listening:
            port = listening.sockets[0].getsockname()[1]
            alice = await _connect(port, "alice", _identity())
            bob = await _connect(port, "bob", _identity())
            charlie = await _connect(port, "charlie", _identity())
            notification_started = asyncio.Event()
            finish_notification = asyncio.Event()
            original_broadcast = chat_server._broadcast

            async def wait_during_rename_notification(room, msg_type, **payload):
                await original_broadcast(room, msg_type, **payload)
                if msg_type == T.USER_LEFT and payload.get("username") == "alice" \
                        and not notification_started.is_set():
                    notification_started.set()
                    await finish_notification.wait()

            try:
                metadata = await _create(alice, "ABCDEF")
                await _join(bob, "ABCDEF", metadata)
                assert (await _recv(alice))["type"] == T.USER_JOINED
                await _create(alice, "GHJKMN")
                await _create(charlie, "PQRSTU")
                monkeypatch.setattr(chat_server, "_broadcast", wait_during_rename_notification)
                await alice.send(pack(T.SET_NAME, name="alice-renamed"))
                await asyncio.wait_for(notification_started.wait(), timeout=3)
                assert (await _recv(bob))["type"] == T.USER_LEFT
                if room_operation == "create":
                    await _create(charlie, "VWXYZ2")
                else:
                    await charlie.send(pack(T.DELETE_ROOM, room_id="PQRSTU"))
                    assert (await _recv(charlie))["type"] == T.ROOM_LEFT
                    for member in (charlie, alice, bob):
                        assert (await _recv(member))["type"] == T.ROOM_DELETED
                finish_notification.set()
                assert (await _recv(bob))["type"] == T.USER_JOINED
                renamed = await _recv(alice)
                assert renamed["type"] == T.READY
                assert renamed["payload"]["name"] == "alice-renamed"
                assert chat_server._user_rooms["alice-renamed"] == {"ABCDEF", "GHJKMN"}
                for room_id in ("ABCDEF", "GHJKMN"):
                    room = chat_server._rooms[room_id]
                    assert room.creator == "alice-renamed"
                    assert "alice-renamed" in room.members
                    assert "alice" not in room.member_identities
                await alice.send(pack(T.LIST_ROOMS))
                assert (await _recv(alice))["type"] == T.ROOM_LIST
                restored = ChatServer(message_db_path=chat_server._message_db_path)
                assert restored._user_rooms["alice-renamed"] == {"ABCDEF", "GHJKMN"}
                assert restored._user_rooms.get("charlie", set()) == (
                    {"PQRSTU", "VWXYZ2"} if room_operation == "create" else set()
                )
            finally:
                finish_notification.set()
                await charlie.close()
                await bob.close()
                await alice.close()

    asyncio.run(run())


def test_leave_completes_when_creator_deletes_same_group(chat_server, monkeypatch):
    """退群广播等待期间创建者删群，退群者仍保持连接与其他群成员关系。"""
    async def run():
        async with websockets.serve(chat_server.handle, "127.0.0.1", 0) as listening:
            port = listening.sockets[0].getsockname()[1]
            alice = await _connect(port, "alice", _identity())
            bob = await _connect(port, "bob", _identity())
            notification_started = asyncio.Event()
            finish_notification = asyncio.Event()
            original_broadcast = chat_server._broadcast

            async def wait_during_leave_notification(room, msg_type, **payload):
                await original_broadcast(room, msg_type, **payload)
                if msg_type == T.USER_LEFT and payload.get("username") == "bob" \
                        and not notification_started.is_set():
                    notification_started.set()
                    await finish_notification.wait()

            try:
                metadata = {}
                for room_id in ("ABCDEF", "GHJKMN"):
                    metadata[room_id] = await _create(alice, room_id)
                    await _join(bob, room_id, metadata[room_id])
                    assert (await _recv(alice))["type"] == T.USER_JOINED
                monkeypatch.setattr(chat_server, "_broadcast", wait_during_leave_notification)
                await bob.send(pack(T.LEAVE_ROOM, room_id="ABCDEF"))
                await asyncio.wait_for(notification_started.wait(), timeout=3)
                assert (await _recv(alice))["type"] == T.USER_LEFT
                await alice.send(pack(T.DELETE_ROOM, room_id="ABCDEF"))
                assert (await _recv(alice))["type"] == T.ROOM_LEFT
                for member in (alice, bob):
                    assert (await _recv(member))["type"] == T.ROOM_DELETED
                finish_notification.set()
                left = await _recv(bob)
                assert left["type"] == T.ROOM_LEFT
                assert left["payload"]["room_id"] == "ABCDEF"
                assert chat_server._user_rooms["bob"] == {"GHJKMN"}
                assert "bob" in chat_server._rooms["GHJKMN"].members
                await bob.send(pack(T.LIST_ROOMS))
                assert (await _recv(bob))["type"] == T.ROOM_LIST
                await _send(bob, "GHJKMN", metadata["GHJKMN"], "删群后连接仍可发送")
                assert (await _recv(alice))["type"] == T.NEW_ENCRYPTED_MSG
                restored = ChatServer(message_db_path=chat_server._message_db_path)
                assert "ABCDEF" not in restored._rooms
                assert restored._user_rooms["bob"] == {"GHJKMN"}
            finally:
                finish_notification.set()
                await bob.close()
                await alice.close()

    asyncio.run(run())


@pytest.mark.parametrize("legacy_creator", [False, True])
def test_offline_owner_name_reuse_cannot_migrate_another_device_rooms(chat_server, legacy_creator):
    """同名设备改名只能迁移自己的群，原群主回来仍可管理并合法改名。"""
    async def run():
        async with websockets.serve(chat_server.handle, "127.0.0.1", 0) as listening:
            port = listening.sockets[0].getsockname()[1]
            owner_identity = _identity()
            owner = await _connect(port, "alice", owner_identity)
            attacker = None
            restored_owner = None
            try:
                await _create(owner, "ABCDEF")
                await _create(owner, "GHJKMN")
                saved_identity = chat_server._rooms["ABCDEF"].member_identities["alice"]
                if legacy_creator:
                    for room_id in ("ABCDEF", "GHJKMN"):
                        chat_server._rooms[room_id].creator_identity = ""
                await owner.close()
                for _ in range(100):
                    if "alice" not in chat_server._name_to_ws:
                        break
                    await asyncio.sleep(0.01)
                assert "alice" not in chat_server._name_to_ws

                attacker = await _connect(port, "alice", _identity())
                await _create(attacker, "PQRSTU")
                await attacker.send(pack(T.SET_NAME, name="mallory"))
                assert (await _recv(attacker))["payload"]["name"] == "mallory"
                for room_id in ("ABCDEF", "GHJKMN"):
                    room = chat_server._rooms[room_id]
                    assert room.creator == "alice"
                    assert room.member_identities == {"alice": saved_identity}
                    assert "mallory" not in room.members
                assert chat_server._user_rooms["alice"] == {"ABCDEF", "GHJKMN"}
                assert chat_server._user_rooms["mallory"] == {"PQRSTU"}
                assert chat_server._rooms["PQRSTU"].creator == "mallory"

                restored_owner = await _connect(port, "alice", owner_identity)
                await restored_owner.send(pack(T.LIST_ROOMS))
                assert (await _recv(restored_owner))["type"] == T.ROOM_LIST
                await restored_owner.send(pack(T.SET_ROOM_NAME, room_id="ABCDEF", name="群主仍可管理"))
                assert (await _recv(restored_owner))["type"] == T.ROOM_NAME_UPDATED
                await restored_owner.send(pack(T.SET_NAME, name="alice-renamed"))
                assert (await _recv(restored_owner))["payload"]["name"] == "alice-renamed"
                assert chat_server._user_rooms["alice-renamed"] == {"ABCDEF", "GHJKMN"}
                for room_id in ("ABCDEF", "GHJKMN"):
                    room = chat_server._rooms[room_id]
                    assert room.creator == "alice-renamed"
                    assert room.creator_identity == saved_identity
                    assert room.member_identities == {"alice-renamed": saved_identity}
                    assert set(room.members) == {"alice-renamed"}
            finally:
                await owner.close()
                if attacker is not None:
                    await attacker.close()
                if restored_owner is not None:
                    await restored_owner.close()

    asyncio.run(run())
