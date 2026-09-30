"""通过终端接收循环验证多群凭证、同步与退出兼容性。"""

import asyncio
import json
from collections import deque
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import websockets
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

import client as client_module
from client import ChatClient
from crypto import create_room_access_metadata, decode_room_envelope, decrypt_room_message, encode_room_envelope, encrypt_room_message
from identity import DeviceIdentity, sign_key_bundle
from protocol import CLIENT_CAPABILITIES, CLIENT_VERSION, PROTOCOL_VERSION, T, pack, unpack


class TerminalSocket:
    def __init__(self):
        self.frames = deque()
        self.sent = []

    def push(self, msg_type, **payload):
        self.frames.append(pack(msg_type, **payload))

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.frames:
            raise StopAsyncIteration
        return self.frames.popleft()

    async def send(self, raw):
        self.sent.append(unpack(raw))


@pytest.fixture
def terminal(tmp_path, monkeypatch):
    identity = DeviceIdentity(Ed25519PrivateKey.generate(), X25519PrivateKey.generate())
    monkeypatch.setattr(client_module, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(client_module, "IdentityStore", lambda _path: SimpleNamespace(load_or_create=lambda: identity))
    monkeypatch.setattr(client_module, "console", MagicMock())
    errors = []
    monkeypatch.setattr(client_module, "_err", errors.append)
    monkeypatch.setattr(client_module, "_sys", lambda _message: None)
    client = ChatClient("ws://localhost:8765")
    client._username = "alice"
    client._ready = True
    client._ws = TerminalSocket()
    shown = []
    monkeypatch.setattr(client, "_show_msg", lambda sender, text, encrypted, ts, room_id=None:
                        shown.append((room_id, sender, text, ts)))
    return client, shown, errors


async def _join(client, room_id, password):
    metadata = create_room_access_metadata(room_id, password)
    client._known_room_metadata[room_id] = dict(metadata)
    await client._handle_line(f"/join {room_id} {password}")
    client._ws.push(T.ROOM_JOINED, room_id=room_id, name=room_id, members=["alice"])
    client._ws.push(T.SYNC_MESSAGES_RESULT, messages=[], has_more=False, next_scopes=[])
    await client._recv_loop()
    return metadata


def _message(room_id, password, metadata, message_id, text, *, sender="bob", client_msg_id=None):
    client_msg_id = client_msg_id or f"{room_id}-{message_id}"
    ciphertext = encode_room_envelope(encrypt_room_message(
        room_id, password, text, client_msg_id, metadata["salt"],
    ))
    return {
        "scope_type": "room", "scope_id": room_id, "message_id": message_id,
        "sender_name": sender, "client_msg_id": client_msg_id, "ciphertext": ciphertext,
        "created_at": 1000 + message_id,
    }


def test_background_messages_and_history_use_each_groups_password_and_cursor(terminal):
    client, shown, errors = terminal

    async def run():
        first = await _join(client, "ABCDEF", "密码甲")
        second = await _join(client, "GHJKMN", "密码乙")
        first_message = _message("ABCDEF", "密码甲", first, 11, "后台群甲")
        second_message = _message("GHJKMN", "密码乙", second, 21, "当前群乙")
        client._ws.push(T.NEW_ENCRYPTED_MSG, **first_message)
        client._ws.push(T.NEW_ENCRYPTED_MSG, **second_message)
        client._ws.push(T.SYNC_MESSAGES_RESULT, messages=[
            second_message, _message("ABCDEF", "密码甲", first, 12, "群甲补收"), first_message,
        ], has_more=False, next_scopes=[])
        await client._recv_loop()
        assert [(room, text) for room, _sender, text, _ts in shown] == [
            ("ABCDEF", "后台群甲"), ("GHJKMN", "当前群乙"), ("ABCDEF", "群甲补收"),
        ]
        assert client._offsets == {"room:ABCDEF": 12, "room:GHJKMN": 21}
        assert client._room_id == "GHJKMN"
        assert client._pending_pw == "密码乙"
        assert errors == []

    asyncio.run(run())


def test_join_in_progress_and_failure_do_not_change_current_group_credentials(terminal):
    client, shown, errors = terminal

    async def run():
        first = await _join(client, "ABCDEF", "密码甲")
        second = create_room_access_metadata("GHJKMN", "密码乙")
        client._known_room_metadata["GHJKMN"] = dict(second)
        await client._handle_line("/join GHJKMN 密码乙")
        client._ws.push(T.NEW_ENCRYPTED_MSG, **_message("ABCDEF", "密码甲", first, 13, "加入期间后台消息"))
        client._ws.push(T.ERROR, code="ROOM_ACCESS_DENIED", room_id="GHJKMN", message="群访问令牌无效")
        await client._recv_loop()
        assert shown[0][2] == "加入期间后台消息"
        assert client._room_id == "ABCDEF"
        assert client._pending_pw == "密码甲"
        assert "GHJKMN" not in client._joined_room_credentials
        assert "GHJKMN" not in client._pending_room_credentials
        await client._handle_line("继续在原群发送")
        sent = client._ws.sent[-1]["payload"]
        assert decrypt_room_message(
            "ABCDEF", "密码甲", decode_room_envelope(sent["ciphertext"]), sent["client_msg_id"], first["salt"],
        ) == "继续在原群发送"
        assert errors == ["群访问令牌无效"]

    asyncio.run(run())


def test_sync_pagination_preserves_safe_cursor_when_live_message_arrives_first(terminal):
    client, shown, errors = terminal

    async def run():
        metadata = await _join(client, "ABCDEF", "密码甲")
        client._offsets["room:ABCDEF"] = 7
        await client._sync_room_messages("ABCDEF")
        latest = _message("ABCDEF", "密码甲", metadata, 100, "新实时消息")
        client._ws.push(T.NEW_ENCRYPTED_MSG, **latest)
        next_scopes = [{"scope_type": "room", "scope_id": "ABCDEF", "after_message_id": 9}]
        client._ws.push(T.SYNC_MESSAGES_RESULT, messages=[
            _message("ABCDEF", "密码甲", metadata, 9, "离线第二条"),
            _message("ABCDEF", "密码甲", metadata, 8, "离线第一条"),
        ], has_more=True, next_scopes=next_scopes)
        await client._recv_loop()
        assert client._offsets["room:ABCDEF"] == 9
        assert json.loads(client_module.STATE_FILE.read_text(encoding="utf-8"))["offsets"]["room:ABCDEF"] == 9
        assert client._ws.sent[-1]["payload"]["scopes"] == next_scopes

        client._ws.push(T.SYNC_MESSAGES_RESULT, messages=[
            _message("ABCDEF", "密码甲", metadata, 10, "离线第三条"), latest,
        ], has_more=False, next_scopes=[])
        await client._recv_loop()
        assert client._offsets["room:ABCDEF"] == 100
        assert "ABCDEF" not in client._room_sync_offsets
        assert [text for _room, _sender, text, _ts in shown] == [
            "新实时消息", "离线第一条", "离线第二条", "离线第三条",
        ]
        assert errors == []

    asyncio.run(run())


def test_leave_is_explicit_and_other_groups_keep_decrypting(terminal):
    client, shown, errors = terminal

    async def run():
        first = await _join(client, "ABCDEF", "密码甲")
        await _join(client, "GHJKMN", "密码乙")
        await client._handle_line("/leave")
        assert client._ws.sent[-1]["type"] == T.LEAVE_ROOM
        assert client._ws.sent[-1]["payload"] == {"room_id": "GHJKMN"}
        client._ws.push(T.ROOM_LEFT, room_id="GHJKMN")
        client._ws.push(T.NEW_ENCRYPTED_MSG, **_message("ABCDEF", "密码甲", first, 14, "保留群仍收消息"))
        await client._recv_loop()
        assert client._room_id is None
        assert set(client._joined_room_credentials) == {"ABCDEF"}
        assert shown[0][:3] == ("ABCDEF", "bob", "保留群仍收消息")
        assert client._offsets["room:ABCDEF"] == 14
        assert errors == []

    asyncio.run(run())


def test_missing_credentials_never_advance_cursor_and_own_history_is_deduplicated(terminal):
    client, shown, errors = terminal

    async def run():
        first = await _join(client, "ABCDEF", "密码甲")
        await client._handle_line("自己的消息")
        sent = client._ws.sent[-1]["payload"]
        client._ws.push(T.SEND_ACK, scope_type="room", scope_id="ABCDEF", client_mid=sent["client_msg_id"], message_id=30)
        client._ws.push(T.SYNC_MESSAGES_RESULT, messages=[_message(
            "ABCDEF", "密码甲", first, 30, "自己的消息", sender="alice", client_msg_id=sent["client_msg_id"],
        )], has_more=False, next_scopes=[])
        unknown = create_room_access_metadata("GHJKMN", "未知密码")
        client._ws.push(T.NEW_ENCRYPTED_MSG, **_message("GHJKMN", "未知密码", unknown, 99, "不应显示"))
        await client._recv_loop()
        assert len(shown) == 1
        assert shown[0][2] == "自己的消息"
        assert client._offsets["room:ABCDEF"] == 30
        assert "room:GHJKMN" not in client._offsets
        assert errors == ["尚未保存群 GHJKMN 的解密凭证，请先加入该群"]

    asyncio.run(run())


def test_terminal_receive_loop_decrypts_background_group_over_real_websocket(terminal, tmp_path, monkeypatch):
    import server as server_module

    client, shown, errors = terminal
    monkeypatch.setattr(server_module, "_ROOMS_FILE", tmp_path / "rooms.json")
    relay = server_module.ChatServer(message_db_path=tmp_path / "messages.db")

    def hello(identity):
        ephemeral = X25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        return pack(
            T.CLIENT_HELLO, client_version=CLIENT_VERSION, protocol_version=PROTOCOL_VERSION,
            capabilities=CLIENT_CAPABILITIES,
            key_bundle=identity.public_bundle(
                ephemeral, sign_key_bundle(identity, ephemeral, PROTOCOL_VERSION), PROTOCOL_VERSION,
            ),
        )

    async def wait_until(predicate):
        async def wait():
            while not predicate():
                await asyncio.sleep(0.01)
        await asyncio.wait_for(wait(), timeout=3)

    async def run():
        async with websockets.serve(relay.handle, "127.0.0.1", 0) as listening:
            port = listening.sockets[0].getsockname()[1]
            url = f"ws://127.0.0.1:{port}"
            async with websockets.connect(url) as alice, websockets.connect(url) as bob:
                client._ws = alice
                client._ready = False
                receive_task = asyncio.create_task(client._recv_loop())
                try:
                    await alice.send(hello(client._identity))
                    await wait_until(lambda: client._ready)
                    await client._handle_line("/create 群甲 密码甲")
                    await wait_until(lambda: len(client._joined_room_credentials) == 1)
                    first_id = client._room_id
                    first = dict(client._joined_room_credentials[first_id])
                    await client._handle_line("/create 群乙 密码乙")
                    await wait_until(lambda: len(client._joined_room_credentials) == 2)
                    second_id = client._room_id
                    await wait_until(lambda: not client._pending_sync_requests)

                    bob_identity = DeviceIdentity(Ed25519PrivateKey.generate(), X25519PrivateKey.generate())
                    await bob.send(hello(bob_identity))
                    assert unpack(await bob.recv())["type"] == T.SERVER_HELLO
                    await bob.send(pack(T.SET_NAME, name="bob"))
                    assert unpack(await bob.recv())["type"] == T.READY
                    await bob.send(pack(T.JOIN_ROOM, room_id=first_id, access_token=first["access_token"]))
                    assert unpack(await bob.recv())["type"] == T.ROOM_JOINED
                    await bob.send(pack(T.SEND_ENCRYPTED_MSG, **_message(
                        first_id, "密码甲", first["metadata"], 1, "来自真实连接的后台消息",
                    )))
                    assert unpack(await bob.recv())["type"] == T.SEND_ACK
                    await wait_until(lambda: bool(shown))
                    assert shown[0][:3] == (first_id, "bob", "来自真实连接的后台消息")
                    assert client._room_id == second_id
                    assert client._offsets[f"room:{first_id}"] == 1
                    assert errors == []
                finally:
                    await alice.close()
                    await receive_task

    asyncio.run(run())
