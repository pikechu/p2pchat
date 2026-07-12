"""Integration test: server routes FILE_* frames user-to-user."""
import asyncio, json, subprocess, sys, os, time, socket, pytest
import pathlib
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import websockets.legacy.client as ws_connect
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from identity import DeviceIdentity, sign_key_bundle
from crypto import create_room_access_metadata
from protocol import CLIENT_CAPABILITIES, CLIENT_VERSION, PROTOCOL_VERSION, T, pack, unpack
from server import ChatServer
from file_transfer import EncryptedFileSender

_ROOM_ACCESS_TOKENS: dict[str, str] = {}


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
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
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


async def _connect(port, name):
    ws = await ws_connect.connect(f"ws://127.0.0.1:{port}")
    identity = DeviceIdentity(Ed25519PrivateKey.generate(), X25519PrivateKey.generate())
    ephemeral = X25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    await ws.send(pack(T.CLIENT_HELLO,
                       client_version=CLIENT_VERSION,
                       protocol_version=PROTOCOL_VERSION,
                       capabilities=CLIENT_CAPABILITIES,
                       key_bundle=identity.public_bundle(
                           ephemeral, sign_key_bundle(identity, ephemeral, PROTOCOL_VERSION), PROTOCOL_VERSION
                       )))
    frame = unpack(await asyncio.wait_for(ws.recv(), timeout=3))
    assert frame["type"] == T.SERVER_HELLO
    await ws.send(pack(T.SET_NAME, name=name))
    frame = unpack(await asyncio.wait_for(ws.recv(), timeout=3))
    assert frame["type"] == T.READY
    return ws


async def _create_room(ws, name="room"):
    room_id = "R" + os.urandom(4).hex().upper()
    room_id = room_id.translate(str.maketrans({"0": "A", "1": "B", "I": "C", "L": "D", "O": "E"}))[:6]
    metadata = create_room_access_metadata(room_id, "测试密码")
    _ROOM_ACCESS_TOKENS[room_id] = metadata.access_token
    await ws.send(pack(T.CREATE_ROOM, room_id=room_id, name=name, **dict(metadata)))
    frame = unpack(await asyncio.wait_for(ws.recv(), timeout=3))
    assert frame["type"] == T.ROOM_CREATED
    return frame["payload"]["room_id"]


async def _join_room(ws, room_id):
    await ws.send(pack(T.JOIN_ROOM, room_id=room_id, access_token=_ROOM_ACCESS_TOKENS[room_id]))
    frame = unpack(await asyncio.wait_for(ws.recv(), timeout=3))
    assert frame["type"] == T.ROOM_JOINED
    return frame


async def _recv_until_type(ws, expected_type, timeout=3):
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError
        frame = unpack(await asyncio.wait_for(ws.recv(), timeout=remaining))
        if frame["type"] == expected_type:
            return frame


def _encrypted_file_frames(
    transfer_id: str,
    data: bytes = b"ABCD",
    *,
    filename: str = "safe.bin",
    scope_type: str = "dm",
    scope_id: str = "scope",
    sender: str = "alice",
    recipient: str = "bob",
):
    tmpdir = pathlib.Path(tempfile.mkdtemp())
    path = tmpdir / filename
    path.write_bytes(data)
    sender_obj = EncryptedFileSender(
        path, b"S" * 32, transfer_id=transfer_id, scope_type=scope_type,
        scope_id=scope_id, sender=sender, recipient=recipient,
    )
    offer = sender_obj.offer_payload()
    chunks = []
    while payload := sender_obj.next_payload():
        chunks.append(payload)
    return offer, chunks, sender_obj.done_payload()


def test_file_offer_routed_to_recipient(server_port, event_loop):
    async def run():
        alice = await _connect(server_port, "alice")
        bob   = await _connect(server_port, "bob")
        offer, _, _ = _encrypted_file_frames("tid1", b"hello", filename="hi.txt", sender="alice", recipient="bob")

        await alice.send(pack(T.FILE_OFFER,
                              to="bob", transfer_id="tid1",
                              **offer))
        frame = json.loads(await asyncio.wait_for(bob.recv(), timeout=3))
        assert frame["type"] == T.FILE_OFFER
        assert frame["payload"]["from"] == "alice"
        assert frame["payload"]["transfer_id"] == "tid1"
        assert "filename" not in frame["payload"]
        assert frame["payload"]["encrypted_metadata"] == offer["encrypted_metadata"]

        await alice.close()
        await bob.close()

    event_loop.run_until_complete(run())


def test_file_accept_routed_back_to_sender(server_port, event_loop):
    async def run():
        alice = await _connect(server_port, "alice2")
        bob   = await _connect(server_port, "bob2")

        offer, _, _ = _encrypted_file_frames("tid2", b"x" * 100, filename="img.png", sender="alice2", recipient="bob2")
        await alice.send(pack(T.FILE_OFFER,
                              to="bob2", transfer_id="tid2",
                              **offer))
        await asyncio.wait_for(bob.recv(), timeout=3)   # consume offer

        await bob.send(pack(T.FILE_ACCEPT, to="alice2", transfer_id="tid2"))
        frame = json.loads(await asyncio.wait_for(alice.recv(), timeout=3))
        assert frame["type"] == T.FILE_ACCEPT
        assert frame["payload"]["from"] == "bob2"
        assert frame["payload"]["transfer_id"] == "tid2"

        await alice.close()
        await bob.close()

    event_loop.run_until_complete(run())


def test_file_chunk_routed_to_recipient(server_port, event_loop):
    async def run():
        alice = await _connect(server_port, "alice3")
        bob   = await _connect(server_port, "bob3")

        offer, chunks, _ = _encrypted_file_frames("tid3", b"ABCD", sender="alice3", recipient="bob3")
        await alice.send(pack(T.FILE_OFFER,
                              to="bob3", transfer_id="tid3",
                              **offer))
        await _recv_until_type(bob, T.FILE_OFFER)

        await alice.send(pack(T.FILE_CHUNK,
                              to="bob3", transfer_id="tid3",
                              index=0, total=1,
                              encrypted_chunk=chunks[0]["encrypted_chunk"]))
        frame = json.loads(await asyncio.wait_for(bob.recv(), timeout=3))
        assert frame["type"] == T.FILE_CHUNK
        assert "data" not in frame["payload"]
        assert frame["payload"]["encrypted_chunk"] == chunks[0]["encrypted_chunk"]

        await alice.close()
        await bob.close()

    event_loop.run_until_complete(run())


def test_direct_file_chunk_with_wrong_total_is_rejected(server_port, event_loop):
    async def run():
        alice = await _connect(server_port, "direct_sender_bad_total")
        bob = await _connect(server_port, "direct_receiver_bad_total")

        offer, chunks, _ = _encrypted_file_frames("direct-bad-total-1", b"ABCD", sender="direct_sender_bad_total", recipient="direct_receiver_bad_total")
        await alice.send(pack(T.FILE_OFFER,
                              to="direct_receiver_bad_total",
                              transfer_id="direct-bad-total-1",
                              **offer))
        await _recv_until_type(bob, T.FILE_OFFER)

        await alice.send(pack(T.FILE_CHUNK,
                              to="direct_receiver_bad_total",
                              transfer_id="direct-bad-total-1",
                              index=0, total=2,
                              encrypted_chunk=chunks[0]["encrypted_chunk"]))
        frame = await _recv_until_type(alice, T.FILE_ERROR)
        assert frame["payload"]["transfer_id"] == "direct-bad-total-1"
        assert "total" in frame["payload"]["message"]
        receiver_error = await _recv_until_type(bob, T.FILE_ERROR)
        assert receiver_error["payload"]["transfer_id"] == "direct-bad-total-1"

        await alice.close()
        await bob.close()

    event_loop.run_until_complete(run())


def test_direct_file_done_with_invalid_envelope_is_rejected(server_port, event_loop):
    async def run():
        alice = await _connect(server_port, "direct_sender_bad_sha")
        bob = await _connect(server_port, "direct_receiver_bad_sha")

        offer, chunks, _ = _encrypted_file_frames("direct-bad-done-1", b"ABCD", sender="direct_sender_bad_sha", recipient="direct_receiver_bad_sha")
        await alice.send(pack(T.FILE_OFFER,
                              to="direct_receiver_bad_sha",
                              transfer_id="direct-bad-done-1",
                              **offer))
        await _recv_until_type(bob, T.FILE_OFFER)

        await alice.send(pack(T.FILE_CHUNK,
                              to="direct_receiver_bad_sha",
                              transfer_id="direct-bad-done-1",
                              index=0, total=1,
                              encrypted_chunk=chunks[0]["encrypted_chunk"]))
        chunk = await _recv_until_type(bob, T.FILE_CHUNK)
        assert chunk["payload"]["transfer_id"] == "direct-bad-done-1"

        await alice.send(pack(T.FILE_DONE,
                              to="direct_receiver_bad_sha",
                              transfer_id="direct-bad-done-1",
                              encrypted_done={"version": 1, "nonce": "bad", "ciphertext": "bad"}))
        frame = await _recv_until_type(alice, T.FILE_ERROR)
        assert frame["payload"]["transfer_id"] == "direct-bad-done-1"
        assert "encrypted_done" in frame["payload"]["message"]
        receiver_error = await _recv_until_type(bob, T.FILE_ERROR)
        assert receiver_error["payload"]["transfer_id"] == "direct-bad-done-1"

        await alice.close()
        await bob.close()

    event_loop.run_until_complete(run())


def test_direct_file_sender_disconnect_notifies_recipient(server_port, event_loop):
    async def run():
        alice = await _connect(server_port, "direct_sender_disconnect")
        bob = await _connect(server_port, "direct_receiver_disconnect")

        offer, _, _ = _encrypted_file_frames("direct-disconnect-1", b"ABCD", sender="direct_sender_disconnect", recipient="direct_receiver_disconnect")
        await alice.send(pack(T.FILE_OFFER,
                              to="direct_receiver_disconnect",
                              transfer_id="direct-disconnect-1",
                              **offer))
        await _recv_until_type(bob, T.FILE_OFFER)

        await alice.close()
        frame = await _recv_until_type(bob, T.FILE_ERROR)
        assert frame["payload"]["transfer_id"] == "direct-disconnect-1"
        assert "disconnected" in frame["payload"]["message"]

        await bob.close()

    event_loop.run_until_complete(run())


def test_file_offer_to_unknown_user_returns_error(server_port, event_loop):
    async def run():
        alice = await _connect(server_port, "alice4")

        offer, _, _ = _encrypted_file_frames("tid4", b"x", sender="alice4", recipient="nobody")
        await alice.send(pack(T.FILE_OFFER,
                              to="nobody", transfer_id="tid4",
                              **offer))
        frame = json.loads(await asyncio.wait_for(alice.recv(), timeout=3))
        assert frame["type"] == T.ERROR

        await alice.close()

    event_loop.run_until_complete(run())


def test_direct_plaintext_file_offer_is_rejected(server_port, event_loop):
    async def run():
        alice = await _connect(server_port, "plain_direct_sender")
        bob = await _connect(server_port, "plain_direct_receiver")

        await alice.send(pack(T.FILE_OFFER,
                              to="plain_direct_receiver", transfer_id="plain-direct-1",
                              filename="x.bin", size=1, mime="application/octet-stream"))
        frame = await _recv_until_type(alice, T.FILE_ERROR)
        assert frame["payload"]["transfer_id"] == "plain-direct-1"
        assert frame["payload"].get("code") == "PLAINTEXT_FORBIDDEN"
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(bob.recv(), timeout=0.5)

        await alice.close()
        await bob.close()

    event_loop.run_until_complete(run())


def test_webrtc_offer_routed_to_recipient(server_port, event_loop):
    async def run():
        alice = await _connect(server_port, "webrtc_alice")
        bob = await _connect(server_port, "webrtc_bob")

        await alice.send(pack(
            T.WEBRTC_OFFER,
            to="webrtc_bob",
            session_id="rtc-1",
            sdp={"type": "offer", "sdp": "v=0"},
        ))
        frame = await _recv_until_type(bob, T.WEBRTC_OFFER)
        assert frame["payload"]["from"] == "webrtc_alice"
        assert frame["payload"]["session_id"] == "rtc-1"
        assert frame["payload"]["sdp"]["type"] == "offer"

        await alice.close()
        await bob.close()

    event_loop.run_until_complete(run())


def test_webrtc_answer_and_ice_routed_back(server_port, event_loop):
    async def run():
        alice = await _connect(server_port, "webrtc_offer_peer")
        bob = await _connect(server_port, "webrtc_answer_peer")

        await bob.send(pack(
            T.WEBRTC_ANSWER,
            to="webrtc_offer_peer",
            session_id="rtc-2",
            sdp={"type": "answer", "sdp": "v=0"},
        ))
        answer = await _recv_until_type(alice, T.WEBRTC_ANSWER)
        assert answer["payload"]["from"] == "webrtc_answer_peer"
        assert answer["payload"]["session_id"] == "rtc-2"

        await alice.send(pack(
            T.WEBRTC_ICE,
            to="webrtc_answer_peer",
            session_id="rtc-2",
            candidate={"candidate": "candidate:1", "sdpMid": "0", "sdpMLineIndex": 0},
        ))
        ice = await _recv_until_type(bob, T.WEBRTC_ICE)
        assert ice["payload"]["from"] == "webrtc_offer_peer"
        assert ice["payload"]["candidate"]["candidate"] == "candidate:1"

        await alice.close()
        await bob.close()

    event_loop.run_until_complete(run())


def test_webrtc_signal_to_unknown_user_returns_error(server_port, event_loop):
    async def run():
        alice = await _connect(server_port, "webrtc_unknown_sender")

        await alice.send(pack(
            T.WEBRTC_OFFER,
            to="missing_webrtc_peer",
            session_id="rtc-missing",
            sdp={"type": "offer", "sdp": "v=0"},
        ))
        frame = await _recv_until_type(alice, T.ERROR)
        assert "missing_webrtc_peer" in frame["payload"]["message"]

        await alice.close()

    event_loop.run_until_complete(run())


def test_room_file_share_rejects_files_larger_than_50mb(server_port, event_loop):
    async def run():
        alice = await _connect(server_port, "room_sender")
        await _create_room(alice, "BigFiles")
        offer, _, _ = _encrypted_file_frames("room-big-1", b"x", scope_type="room", scope_id="room", sender="room_sender", recipient="")
        offer["size"] = 51 * 1024 * 1024
        offer["total"] = (offer["size"] + 32768 - 1) // 32768
        offer["ciphertext_size"] = offer["size"] + offer["total"] * 16

        await alice.send(pack(
            T.FILE_ROOM_SHARE,
            room_id="IGNORED",
            transfer_id="room-big-1",
            **offer,
        ))
        frame = unpack(await asyncio.wait_for(alice.recv(), timeout=3))
        assert frame["type"] == T.FILE_ROOM_ERROR
        assert "最大 50 MB" in frame["payload"]["message"]

        await alice.close()

    event_loop.run_until_complete(run())


def test_room_plaintext_file_share_is_rejected(server_port, event_loop):
    async def run():
        alice = await _connect(server_port, "plain_room_sender")
        await _create_room(alice, "PlainFiles")

        await alice.send(pack(
            T.FILE_ROOM_SHARE,
            room_id="IGNORED",
            transfer_id="plain-room-1",
            filename="x.bin",
            size=1,
            mime="application/octet-stream",
        ))
        frame = await _recv_until_type(alice, T.FILE_ROOM_ERROR)
        assert frame["payload"]["transfer_id"] == "plain-room-1"
        assert frame["payload"].get("code") == "PLAINTEXT_FORBIDDEN"

        await alice.close()

    event_loop.run_until_complete(run())


def test_room_file_chunk_from_non_sender_is_ignored(server_port, event_loop):
    async def run():
        alice = await _connect(server_port, "room_owner")
        room_id = await _create_room(alice, "SecureRoom")
        bob = await _connect(server_port, "room_peer")
        mallory = await _connect(server_port, "room_attacker")
        await _join_room(bob, room_id)
        await _join_room(mallory, room_id)

        offer, _, _ = _encrypted_file_frames("room-secure-1", b"ABCD", scope_type="room", scope_id=room_id, sender="room_owner", recipient="")
        await alice.send(pack(
            T.FILE_ROOM_SHARE,
            room_id=room_id,
            transfer_id="room-secure-1",
            **offer,
        ))
        share_for_bob = await _recv_until_type(bob, T.FILE_ROOM_SHARE)
        share_for_mallory = await _recv_until_type(mallory, T.FILE_ROOM_SHARE)
        assert share_for_bob["type"] == T.FILE_ROOM_SHARE
        assert share_for_mallory["type"] == T.FILE_ROOM_SHARE

        await mallory.send(pack(
            T.FILE_ROOM_CHUNK,
            transfer_id="room-secure-1",
            index=0,
            total=1,
            encrypted_chunk={"version": 1, "nonce": "AAAAAAAAAAAAAAAA", "ciphertext": "AAAAAAAAAAAAAAAAAAAAAA=="},
        ))
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(bob.recv(), timeout=0.5)

        await alice.close()
        await bob.close()
        await mallory.close()

    event_loop.run_until_complete(run())


def test_room_file_done_is_acked_only_after_receiver_confirms(server_port, event_loop):
    async def run():
        alice = await _connect(server_port, "room_sender_ack")
        room_id = await _create_room(alice, "AckRoom")
        bob = await _connect(server_port, "room_receiver_ack")
        await _join_room(bob, room_id)

        offer, chunks, done = _encrypted_file_frames("room-ack-1", b"ABCD", scope_type="room", scope_id=room_id, sender="room_sender_ack", recipient="")
        await alice.send(pack(
            T.FILE_ROOM_SHARE,
            room_id=room_id,
            transfer_id="room-ack-1",
            **offer,
        ))
        await _recv_until_type(bob, T.FILE_ROOM_SHARE)

        await alice.send(pack(
            T.FILE_ROOM_CHUNK,
            transfer_id="room-ack-1",
            index=0,
            total=1,
            encrypted_chunk=chunks[0]["encrypted_chunk"],
        ))
        chunk_ack = await _recv_until_type(alice, T.FILE_ROOM_CHUNK_ACK)
        assert chunk_ack["payload"]["transfer_id"] == "room-ack-1"
        assert chunk_ack["payload"]["index"] == 0

        await alice.send(pack(
            T.FILE_ROOM_DONE,
            transfer_id="room-ack-1",
            encrypted_done=done["encrypted_done"],
        ))
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(alice.recv(), timeout=0.5)

        await bob.send(pack(
            T.FILE_ROOM_RECEIVED,
            transfer_id="room-ack-1",
        ))
        done_ack = await _recv_until_type(alice, T.FILE_ROOM_DONE_ACK)
        assert done_ack["payload"]["transfer_id"] == "room-ack-1"

        await alice.close()
        await bob.close()

    event_loop.run_until_complete(run())


def test_room_file_chunk_with_wrong_total_is_rejected(server_port, event_loop):
    async def run():
        alice = await _connect(server_port, "room_sender_bad_total")
        room_id = await _create_room(alice, "BadTotalRoom")
        bob = await _connect(server_port, "room_receiver_bad_total")
        await _join_room(bob, room_id)

        offer, chunks, _ = _encrypted_file_frames("room-bad-total-1", b"ABCD", scope_type="room", scope_id=room_id, sender="room_sender_bad_total", recipient="")
        await alice.send(pack(
            T.FILE_ROOM_SHARE,
            room_id=room_id,
            transfer_id="room-bad-total-1",
            **offer,
        ))
        await _recv_until_type(bob, T.FILE_ROOM_SHARE)

        await alice.send(pack(
            T.FILE_ROOM_CHUNK,
            transfer_id="room-bad-total-1",
            index=0,
            total=2,
            encrypted_chunk=chunks[0]["encrypted_chunk"],
        ))
        err = await _recv_until_type(alice, T.FILE_ROOM_ERROR)
        assert err["payload"]["transfer_id"] == "room-bad-total-1"
        assert "total" in err["payload"]["message"]
        receiver_error = await _recv_until_type(bob, T.FILE_ROOM_ERROR)
        assert receiver_error["payload"]["transfer_id"] == "room-bad-total-1"

        await alice.close()
        await bob.close()

    event_loop.run_until_complete(run())


def test_room_file_chunk_out_of_order_is_rejected(server_port, event_loop):
    async def run():
        alice = await _connect(server_port, "room_sender_ooo")
        room_id = await _create_room(alice, "OutOfOrderRoom")
        bob = await _connect(server_port, "room_receiver_ooo")
        await _join_room(bob, room_id)

        offer, chunks, _ = _encrypted_file_frames("room-ooo-1", b"A" * 40000, scope_type="room", scope_id=room_id, sender="room_sender_ooo", recipient="")
        await alice.send(pack(
            T.FILE_ROOM_SHARE,
            room_id=room_id,
            transfer_id="room-ooo-1",
            **offer,
        ))
        await _recv_until_type(bob, T.FILE_ROOM_SHARE)

        await alice.send(pack(
            T.FILE_ROOM_CHUNK,
            transfer_id="room-ooo-1",
            index=1,
            total=2,
            encrypted_chunk=chunks[1]["encrypted_chunk"],
        ))
        err = await _recv_until_type(alice, T.FILE_ROOM_ERROR)
        assert err["payload"]["transfer_id"] == "room-ooo-1"
        assert "index" in err["payload"]["message"]
        receiver_error = await _recv_until_type(bob, T.FILE_ROOM_ERROR)
        assert receiver_error["payload"]["transfer_id"] == "room-ooo-1"

        await alice.close()
        await bob.close()

    event_loop.run_until_complete(run())


def test_room_file_done_with_invalid_envelope_is_rejected(server_port, event_loop):
    async def run():
        alice = await _connect(server_port, "room_sender_bad_sha")
        room_id = await _create_room(alice, "BadShaRoom")
        bob = await _connect(server_port, "room_receiver_bad_sha")
        await _join_room(bob, room_id)

        offer, chunks, _ = _encrypted_file_frames("room-bad-done-1", b"ABCD", scope_type="room", scope_id=room_id, sender="room_sender_bad_sha", recipient="")
        await alice.send(pack(
            T.FILE_ROOM_SHARE,
            room_id=room_id,
            transfer_id="room-bad-done-1",
            **offer,
        ))
        await _recv_until_type(bob, T.FILE_ROOM_SHARE)

        await alice.send(pack(
            T.FILE_ROOM_CHUNK,
            transfer_id="room-bad-done-1",
            index=0,
            total=1,
            encrypted_chunk=chunks[0]["encrypted_chunk"],
        ))
        await _recv_until_type(alice, T.FILE_ROOM_CHUNK_ACK)
        chunk = await _recv_until_type(bob, T.FILE_ROOM_CHUNK)
        assert chunk["payload"]["transfer_id"] == "room-bad-done-1"

        await alice.send(pack(
            T.FILE_ROOM_DONE,
            transfer_id="room-bad-done-1",
            encrypted_done={"version": 1, "nonce": "bad", "ciphertext": "bad"},
        ))
        err = await _recv_until_type(alice, T.FILE_ROOM_ERROR)
        assert err["payload"]["transfer_id"] == "room-bad-done-1"
        assert "encrypted_done" in err["payload"]["message"]
        receiver_error = await _recv_until_type(bob, T.FILE_ROOM_ERROR)
        assert receiver_error["payload"]["transfer_id"] == "room-bad-done-1"

        await alice.close()
        await bob.close()

    event_loop.run_until_complete(run())


def test_room_file_sender_disconnect_notifies_receivers(server_port, event_loop):
    async def run():
        alice = await _connect(server_port, "room_sender_disconnect")
        room_id = await _create_room(alice, "DisconnectRoom")
        bob = await _connect(server_port, "room_receiver_disconnect")
        await _join_room(bob, room_id)

        offer, _, _ = _encrypted_file_frames("room-disconnect-1", b"ABCD", scope_type="room", scope_id=room_id, sender="room_sender_disconnect", recipient="")
        await alice.send(pack(
            T.FILE_ROOM_SHARE,
            room_id=room_id,
            transfer_id="room-disconnect-1",
            **offer,
        ))
        await _recv_until_type(bob, T.FILE_ROOM_SHARE)

        await alice.close()
        err = await _recv_until_type(bob, T.FILE_ROOM_ERROR)
        assert err["payload"]["transfer_id"] == "room-disconnect-1"
        assert "disconnected" in err["payload"]["message"]

        await bob.close()

    event_loop.run_until_complete(run())


def test_room_file_receiver_disconnect_unblocks_sender_done_ack(server_port, event_loop):
    async def run():
        alice = await _connect(server_port, "room_sender_receiver_gone")
        room_id = await _create_room(alice, "ReceiverGoneRoom")
        bob = await _connect(server_port, "room_receiver_gone")
        await _join_room(bob, room_id)

        offer, chunks, done = _encrypted_file_frames("room-receiver-gone-1", b"ABCD", scope_type="room", scope_id=room_id, sender="room_sender_receiver_gone", recipient="")
        await alice.send(pack(
            T.FILE_ROOM_SHARE,
            room_id=room_id,
            transfer_id="room-receiver-gone-1",
            **offer,
        ))
        await _recv_until_type(bob, T.FILE_ROOM_SHARE)

        await alice.send(pack(
            T.FILE_ROOM_CHUNK,
            transfer_id="room-receiver-gone-1",
            index=0,
            total=1,
            encrypted_chunk=chunks[0]["encrypted_chunk"],
        ))
        await _recv_until_type(alice, T.FILE_ROOM_CHUNK_ACK)
        await _recv_until_type(bob, T.FILE_ROOM_CHUNK)

        await alice.send(pack(
            T.FILE_ROOM_DONE,
            transfer_id="room-receiver-gone-1",
            encrypted_done=done["encrypted_done"],
        ))
        await _recv_until_type(bob, T.FILE_ROOM_DONE)
        await bob.close()

        done_ack = await _recv_until_type(alice, T.FILE_ROOM_DONE_ACK)
        assert done_ack["payload"]["transfer_id"] == "room-receiver-gone-1"

        await alice.close()

    event_loop.run_until_complete(run())


def test_prune_stale_transfers_removes_expired_entries():
    server = ChatServer(enable_message_persistence=False)
    now = time.time()
    server._transfer_meta = {
        "fresh": {"from_user": "alice", "last_seen": now - 5},
        "stale": {"from_user": "bob", "last_seen": now - 700},
    }

    removed = server._prune_stale_transfers(now=now, max_age=600)

    assert removed == ["stale"]
    assert "fresh" in server._transfer_meta
    assert "stale" not in server._transfer_meta
