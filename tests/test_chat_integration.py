"""
Integration tests: full chat flow — create room, join, send messages, leave.
Starts a real server subprocess; tests run over real WebSocket connections.
"""
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import uuid

import pytest
import websockets.legacy.client as ws_connect

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from protocol import CLIENT_CAPABILITIES, CLIENT_VERSION, PROTOCOL_VERSION, T, pack, unpack
from identity import DeviceIdentity, sign_key_bundle
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from crypto import create_room_access_metadata, encode_room_envelope, encrypt_room_message

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


# ── fixtures ──────────────────────────────────────────────────────────────────

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server_port():
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "server.py", "--host", "127.0.0.1", "--port", str(port),
         "--no-message-persistence"],
        cwd=os.path.join(os.path.dirname(__file__), ".."),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(0.8)
    yield port
    proc.terminate()
    proc.wait()


@pytest.fixture()
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ── helpers ───────────────────────────────────────────────────────────────────

def _hello_payload():
    identity = DeviceIdentity(Ed25519PrivateKey.generate(), X25519PrivateKey.generate())
    ephemeral = X25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return {
        "client_version": CLIENT_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "capabilities": CLIENT_CAPABILITIES,
        "key_bundle": identity.public_bundle(
            ephemeral, sign_key_bundle(identity, ephemeral, PROTOCOL_VERSION), PROTOCOL_VERSION
        ),
    }


async def _connect(port: int, name: str):
    """Connect and set username. Returns the websocket."""
    ws = await ws_connect.connect(f"ws://127.0.0.1:{port}")
    await ws.send(pack(T.CLIENT_HELLO, **_hello_payload()))
    frame = unpack(await ws.recv())
    assert frame["type"] == T.SERVER_HELLO
    await ws.send(pack(T.SET_NAME, name=name))
    frame = unpack(await ws.recv())
    assert frame["type"] == T.READY
    return ws


async def _recv(ws, timeout=3) -> dict:
    return unpack(await asyncio.wait_for(ws.recv(), timeout=timeout))


_room_material: dict[str, tuple[str, object]] = {}


async def _create_room(ws, name: str, password: str = "测试密码") -> dict:
    room_id = uuid.uuid4().hex.upper().translate(str.maketrans({"0": "A", "1": "B", "I": "C", "L": "D", "O": "E"}))[:6]
    metadata = create_room_access_metadata(room_id, password)
    _room_material[room_id] = (password, metadata)
    await ws.send(pack(T.CREATE_ROOM, room_id=room_id, name=name, locked=bool(password), **dict(metadata)))
    return await _recv(ws)


async def _join_room(ws, room_id: str, password: str | None = None) -> None:
    stored_password, metadata = _room_material[room_id]
    password = stored_password if password is None else password
    from crypto import decrypt_room_access_token
    try:
        access_token = decrypt_room_access_token(room_id, password, metadata)
    except Exception:
        access_token = "invalid"
    await ws.send(pack(T.JOIN_ROOM, room_id=room_id, access_token=access_token))


async def _join_room_from_list(ws, room_id: str, password: str) -> None:
    from crypto import decrypt_room_access_token

    await ws.send(pack(T.LIST_ROOMS))
    frame = await _recv(ws)
    assert frame["type"] == T.ROOM_LIST
    listed = next(room for room in frame["payload"]["rooms"] if room["id"] == room_id)
    metadata = {
        "salt": listed["salt"],
        "encrypted_access_token": listed["encrypted_access_token"],
    }
    access_token = decrypt_room_access_token(room_id, password, metadata)
    await ws.send(pack(T.JOIN_ROOM, room_id=room_id, access_token=access_token))


async def _send_room_message(ws, room_id: str, plaintext: str, client_msg_id: str = "room-msg") -> None:
    password, metadata = _room_material[room_id]
    ciphertext = encode_room_envelope(encrypt_room_message(room_id, password, plaintext, client_msg_id, metadata["salt"]))
    await ws.send(pack(T.SEND_ENCRYPTED_MSG, scope_type="room", scope_id=room_id,
                       ciphertext=ciphertext, client_msg_id=client_msg_id,
                       crypto_meta={"alg": "ChaCha20-Poly1305", "version": 1}))


# ── SET_NAME ──────────────────────────────────────────────────────────────────

def test_set_name_ready(server_port, event_loop):
    async def run():
        ws = await ws_connect.connect(f"ws://127.0.0.1:{server_port}")
        await ws.send(pack(T.CLIENT_HELLO, **_hello_payload()))
        frame = await _recv(ws)
        assert frame["type"] == T.SERVER_HELLO

        await ws.send(pack(T.SET_NAME, name="tester_name"))
        frame = await _recv(ws)
        assert frame["type"] == T.READY
        await ws.close()

    event_loop.run_until_complete(run())


def test_duplicate_name_takes_over_existing_connection(server_port, event_loop):
    async def run():
        ws1 = await _connect(server_port, "dup_user")
        ws2 = await ws_connect.connect(f"ws://127.0.0.1:{server_port}")
        await ws2.send(pack(T.CLIENT_HELLO, **_hello_payload()))
        await ws2.recv()  # SERVER_HELLO

        await ws2.send(pack(T.SET_NAME, name="dup_user"))
        frame = await _recv(ws2)
        assert frame["type"] == T.READY

        await ws1.close()
        await ws2.close()

    event_loop.run_until_complete(run())


def test_set_name_after_ready_renames_online_user_and_room_member(server_port, event_loop):
    async def run():
        ws = await _connect(server_port, "rename_old")
        room_id = (await _create_room(ws, "Rename Room"))["payload"]["room_id"]
        observer = await _connect(server_port, "rename_observer")
        await _join_room(observer, room_id)
        joined = await _recv(observer)
        assert joined["type"] == T.ROOM_JOINED
        assert "rename_old" in joined["payload"]["members"]
        assert (await _recv(ws))["type"] == T.USER_JOINED

        await ws.send(pack(T.SET_NAME, name="rename_new"))
        frame = await _recv(ws)
        assert frame["type"] == T.READY
        assert frame["payload"]["name"] == "rename_new"
        left = await _recv(observer)
        assert left["type"] == T.USER_LEFT
        assert left["payload"]["username"] == "rename_old"
        joined = await _recv(observer)
        assert joined["type"] == T.USER_JOINED
        assert joined["payload"]["username"] == "rename_new"

        await ws.send(pack(T.LIST_USERS))
        users = (await _recv(ws))["payload"]["users"]
        assert "rename_new" in users
        assert "rename_old" not in users

        newcomer = await _connect(server_port, "rename_newcomer")
        await _join_room(newcomer, room_id)
        newcomer_joined = await _recv(newcomer)
        assert newcomer_joined["type"] == T.ROOM_JOINED
        assert "rename_new" in newcomer_joined["payload"]["members"]
        assert "rename_old" not in newcomer_joined["payload"]["members"]

        await ws.close()
        await observer.close()
        await newcomer.close()

    event_loop.run_until_complete(run())


# ── CREATE_ROOM ───────────────────────────────────────────────────────────────

def test_create_room_returns_room_id(server_port, event_loop):
    async def run():
        ws = await _connect(server_port, "creator1")
        frame = await _create_room(ws, "My Room")
        assert frame["type"] == T.ROOM_CREATED
        assert "room_id" in frame["payload"]
        assert len(frame["payload"]["room_id"]) == 6
        await ws.close()

    event_loop.run_until_complete(run())


def test_create_room_uses_access_token_metadata(server_port, event_loop):
    async def run():
        ws = await _connect(server_port, "creator2")
        frame = await _create_room(ws, "Secret", "pw123")
        assert frame["type"] == T.ROOM_CREATED
        assert frame["payload"].get("locked") is True
        await ws.close()

    event_loop.run_until_complete(run())


def test_create_room_without_password_is_unlocked(server_port, event_loop):
    async def run():
        ws = await _connect(server_port, "creator_public")
        frame = await _create_room(ws, "Public", "")
        assert frame["type"] == T.ROOM_CREATED
        assert frame["payload"].get("locked") is False

        await ws.send(pack(T.LIST_ROOMS))
        rooms = (await _recv(ws))["payload"]["rooms"]
        listed = next(room for room in rooms if room["id"] == frame["payload"]["room_id"])
        assert listed["locked"] is False
        await ws.close()

    event_loop.run_until_complete(run())


# ── JOIN_ROOM ─────────────────────────────────────────────────────────────────

def test_join_room_notifies_creator(server_port, event_loop):
    async def run():
        alice = await _connect(server_port, "alice_j1")
        room_id = (await _create_room(alice, "Join Test"))["payload"]["room_id"]

        bob = await _connect(server_port, "bob_j1")
        await _join_room(bob, room_id)

        frame_bob = await _recv(bob)
        assert frame_bob["type"] == T.ROOM_JOINED
        assert frame_bob["payload"]["room_id"] == room_id

        frame_alice = await _recv(alice)
        assert frame_alice["type"] == T.USER_JOINED
        assert frame_alice["payload"]["username"] == "bob_j1"

        await alice.close()
        await bob.close()

    event_loop.run_until_complete(run())


def test_join_locked_room_wrong_password_rejected(server_port, event_loop):
    async def run():
        alice = await _connect(server_port, "alice_pw1")
        room_id = (await _create_room(alice, "Locked", "correct"))["payload"]["room_id"]

        bob = await _connect(server_port, "bob_pw1")
        await _join_room(bob, room_id, "wrong")
        frame = await _recv(bob)
        assert frame["type"] == T.ERROR

        await alice.close()
        await bob.close()

    event_loop.run_until_complete(run())


def test_join_locked_room_correct_password_succeeds(server_port, event_loop):
    async def run():
        alice = await _connect(server_port, "alice_pw2")
        room_id = (await _create_room(alice, "Locked2", "secret"))["payload"]["room_id"]

        bob = await _connect(server_port, "bob_pw2")
        await _join_room(bob, room_id)
        frame = await _recv(bob)
        assert frame["type"] == T.ROOM_JOINED

        await alice.close()
        await bob.close()

    event_loop.run_until_complete(run())


def test_join_room_uses_metadata_from_room_list(server_port, event_loop):
    async def run():
        alice = await _connect(server_port, "alice_pw3")
        room_id = (await _create_room(alice, "ListedLocked", "from-list"))["payload"]["room_id"]

        bob = await _connect(server_port, "bob_pw3")
        await _join_room_from_list(bob, room_id, "from-list")
        frame = await _recv(bob)
        assert frame["type"] == T.ROOM_JOINED

        await alice.close()
        await bob.close()

    event_loop.run_until_complete(run())


def test_join_nonexistent_room_returns_error(server_port, event_loop):
    async def run():
        ws = await _connect(server_port, "lost_user")
        await ws.send(pack(T.JOIN_ROOM, room_id="XXXXXX"))
        frame = await _recv(ws)
        assert frame["type"] == T.ERROR
        await ws.close()

    event_loop.run_until_complete(run())


# ── SEND_MSG / NEW_MSG ────────────────────────────────────────────────────────

def test_plaintext_room_message_is_rejected(server_port, event_loop):
    async def run():
        alice = await _connect(server_port, "alice_plain_room")
        room_id = (await _create_room(alice, "PlainRoom"))["payload"]["room_id"]

        await alice.send(pack(T.SEND_MSG, text="plain room text", encrypted=False))
        frame = await _recv(alice)
        assert frame["type"] == T.ERROR
        assert frame["payload"]["code"] == "PLAINTEXT_FORBIDDEN"
        assert frame["payload"]["recoverable"] is False
        await alice.close()

    event_loop.run_until_complete(run())


def test_send_msg_delivered_to_other_members(server_port, event_loop):
    async def run():
        alice = await _connect(server_port, "alice_msg1")
        room_id = (await _create_room(alice, "Chat Room"))["payload"]["room_id"]

        bob = await _connect(server_port, "bob_msg1")
        await _join_room(bob, room_id)
        await _recv(bob)    # ROOM_JOINED
        await _recv(alice)  # USER_JOINED

        await _send_room_message(alice, room_id, "hello bob")
        frame = await _recv(bob)
        assert frame["type"] == T.NEW_ENCRYPTED_MSG
        assert "hello bob" not in frame["payload"]["ciphertext"]
        assert frame["payload"]["sender_name"] == "alice_msg1"

        await alice.close()
        await bob.close()

    event_loop.run_until_complete(run())


def test_send_msg_not_echoed_to_sender(server_port, event_loop):
    async def run():
        alice = await _connect(server_port, "alice_msg2")
        room_id = (await _create_room(alice, "Echo Test"))["payload"]["room_id"]

        bob = await _connect(server_port, "bob_msg2")
        await _join_room(bob, room_id)
        await _recv(bob)    # ROOM_JOINED
        await _recv(alice)  # USER_JOINED

        await _send_room_message(alice, room_id, "test")
        # alice should NOT receive NEW_MSG; she gets SEND_ACK instead
        frame = await _recv(alice)
        assert frame["type"] != T.NEW_ENCRYPTED_MSG

        await alice.close()
        await bob.close()

    event_loop.run_until_complete(run())


def test_multiple_members_all_receive_message(server_port, event_loop):
    async def run():
        alice = await _connect(server_port, "alice_multi")
        room_id = (await _create_room(alice, "Multi Room"))["payload"]["room_id"]

        bob = await _connect(server_port, "bob_multi")
        carol = await _connect(server_port, "carol_multi")

        await _join_room(bob, room_id)
        await _recv(bob)
        await _recv(alice)  # USER_JOINED bob

        await _join_room(carol, room_id)
        await _recv(carol)
        await _recv(alice)  # USER_JOINED carol
        await _recv(bob)    # USER_JOINED carol

        await _send_room_message(alice, room_id, "hi all")

        frame_bob = await _recv(bob)
        frame_carol = await _recv(carol)
        assert frame_bob["type"] == T.NEW_ENCRYPTED_MSG
        assert frame_carol["type"] == T.NEW_ENCRYPTED_MSG
        assert "hi all" not in frame_bob["payload"]["ciphertext"]

        await alice.close()
        await bob.close()
        await carol.close()

    event_loop.run_until_complete(run())


# ── LEAVE_ROOM ────────────────────────────────────────────────────────────────

def test_leave_room_notifies_remaining_members(server_port, event_loop):
    async def run():
        alice = await _connect(server_port, "alice_leave")
        room_id = (await _create_room(alice, "Leave Test"))["payload"]["room_id"]

        bob = await _connect(server_port, "bob_leave")
        await _join_room(bob, room_id)
        await _recv(bob)
        await _recv(alice)  # USER_JOINED

        await bob.send(pack(T.LEAVE_ROOM, room_id=room_id))
        frame_bob = await _recv(bob)
        assert frame_bob["type"] == T.ROOM_LEFT

        frame_alice = await _recv(alice)
        assert frame_alice["type"] == T.USER_LEFT
        assert frame_alice["payload"]["username"] == "bob_leave"

        await alice.close()
        await bob.close()

    event_loop.run_until_complete(run())


def test_room_persists_when_last_member_leaves(server_port, event_loop):
    """Rooms are permanent — they survive even when all members leave."""
    async def run():
        alice = await _connect(server_port, "alice_last")
        room_id = (await _create_room(alice, "Temp Room"))["payload"]["room_id"]

        await alice.send(pack(T.LEAVE_ROOM, room_id=room_id))
        await _recv(alice)  # ROOM_LEFT

        # Room should still exist — another user can join it
        bob = await _connect(server_port, "bob_last")
        await _join_room(bob, room_id)
        frame = await _recv(bob)
        assert frame["type"] == T.ROOM_JOINED

        await alice.close()
        await bob.close()

    event_loop.run_until_complete(run())


def test_only_creator_can_delete_room(server_port, event_loop):
    """Only the room creator can delete it via DELETE_ROOM."""
    async def run():
        alice = await _connect(server_port, "alice_del")
        room_id = (await _create_room(alice, "Alice Room"))["payload"]["room_id"]

        bob = await _connect(server_port, "bob_del")
        await _join_room(bob, room_id)
        await _recv(bob)   # ROOM_JOINED
        await _recv(alice)  # USER_JOINED

        # Bob (non-creator) tries to delete → error
        await bob.send(pack(T.DELETE_ROOM, room_id=room_id))
        frame = await _recv(bob)
        assert frame["type"] == T.ERROR

        # Alice (creator) deletes → success, ROOM_DELETED broadcast to all
        await alice.send(pack(T.DELETE_ROOM, room_id=room_id))
        # Both members get ROOM_LEFT; all connected get ROOM_DELETED
        frames_alice = {(await _recv(alice))["type"], (await _recv(alice))["type"]}
        frames_bob   = {(await _recv(bob))["type"],   (await _recv(bob))["type"]}
        assert T.ROOM_LEFT    in frames_alice
        assert T.ROOM_DELETED in frames_alice
        assert T.ROOM_LEFT    in frames_bob
        assert T.ROOM_DELETED in frames_bob

        # Room is gone — nobody can join
        carol = await _connect(server_port, "carol_del")
        await _join_room(carol, room_id)
        frame = await _recv(carol)
        assert frame["type"] == T.ERROR

        await alice.close()
        await bob.close()
        await carol.close()

    event_loop.run_until_complete(run())


# ── LIST_ROOMS ────────────────────────────────────────────────────────────────

def test_list_rooms_contains_created_room(server_port, event_loop):
    async def run():
        alice = await _connect(server_port, "alice_list")
        room_id = (await _create_room(alice, "Listed Room"))["payload"]["room_id"]

        bob = await _connect(server_port, "bob_list")
        await bob.send(pack(T.LIST_ROOMS))
        frame = await _recv(bob)
        assert frame["type"] == T.ROOM_LIST
        room_ids = [r["id"] for r in frame["payload"].get("rooms", [])]
        assert room_id in room_ids

        await alice.close()
        await bob.close()

    event_loop.run_until_complete(run())


# ── DM ────────────────────────────────────────────────────────────────────────

def test_plaintext_dm_is_rejected(server_port, event_loop):
    async def run():
        alice = await _connect(server_port, "alice_dm")
        bob = await _connect(server_port, "bob_dm")

        await alice.send(pack(T.SEND_DM, to="bob_dm", text="hey", client_mid="mid1"))
        frame = await _recv(alice)
        assert frame["type"] == T.ERROR
        assert frame["payload"]["code"] == "PLAINTEXT_FORBIDDEN"
        assert frame["payload"]["recoverable"] is False

        await alice.close()
        await bob.close()

    event_loop.run_until_complete(run())


def test_dm_to_unknown_user_returns_error(server_port, event_loop):
    async def run():
        alice = await _connect(server_port, "alice_dm2")
        await alice.send(pack(T.SEND_DM, to="ghost", text="hello", client_mid="mid2"))
        frame = await _recv(alice)
        assert frame["type"] == T.ERROR
        await alice.close()

    event_loop.run_until_complete(run())
