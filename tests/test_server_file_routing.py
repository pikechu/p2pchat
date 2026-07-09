"""Integration test: server routes FILE_* frames user-to-user."""
import asyncio, json, subprocess, sys, os, time, socket, pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import websockets.legacy.client as ws_connect
from protocol import T, pack, unpack
from server import ChatServer


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server_port():
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "server.py", "--host", "127.0.0.1", "--port", str(port)],
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
    await ws.recv()   # WELCOME
    await ws.send(pack(T.SET_NAME, name=name))
    await ws.recv()   # SYSTEM
    return ws


async def _create_room(ws, name="room"):
    await ws.send(pack(T.CREATE_ROOM, name=name))
    frame = unpack(await asyncio.wait_for(ws.recv(), timeout=3))
    assert frame["type"] == T.ROOM_CREATED
    return frame["payload"]["room_id"]


async def _join_room(ws, room_id):
    await ws.send(pack(T.JOIN_ROOM, room_id=room_id))
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


def test_file_offer_routed_to_recipient(server_port, event_loop):
    async def run():
        alice = await _connect(server_port, "alice")
        bob   = await _connect(server_port, "bob")

        await alice.send(pack(T.FILE_OFFER,
                              to="bob", transfer_id="tid1",
                              filename="hi.txt", size=5, mime="text/plain"))
        frame = json.loads(await asyncio.wait_for(bob.recv(), timeout=3))
        assert frame["type"] == T.FILE_OFFER
        assert frame["payload"]["from"] == "alice"
        assert frame["payload"]["transfer_id"] == "tid1"
        assert frame["payload"]["filename"] == "hi.txt"

        await alice.close()
        await bob.close()

    event_loop.run_until_complete(run())


def test_file_accept_routed_back_to_sender(server_port, event_loop):
    async def run():
        alice = await _connect(server_port, "alice2")
        bob   = await _connect(server_port, "bob2")

        await alice.send(pack(T.FILE_OFFER,
                              to="bob2", transfer_id="tid2",
                              filename="img.png", size=100, mime="image/png"))
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

        await alice.send(pack(T.FILE_CHUNK,
                              to="bob3", transfer_id="tid3",
                              index=0, total=1, data="AAAA"))
        frame = json.loads(await asyncio.wait_for(bob.recv(), timeout=3))
        assert frame["type"] == T.FILE_CHUNK
        assert frame["payload"]["data"] == "AAAA"

        await alice.close()
        await bob.close()

    event_loop.run_until_complete(run())


def test_direct_file_chunk_with_wrong_total_is_rejected(server_port, event_loop):
    async def run():
        alice = await _connect(server_port, "direct_sender_bad_total")
        bob = await _connect(server_port, "direct_receiver_bad_total")

        await alice.send(pack(T.FILE_OFFER,
                              to="direct_receiver_bad_total",
                              transfer_id="direct-bad-total-1",
                              filename="x.bin", size=4,
                              mime="application/octet-stream"))
        await _recv_until_type(bob, T.FILE_OFFER)

        await alice.send(pack(T.FILE_CHUNK,
                              to="direct_receiver_bad_total",
                              transfer_id="direct-bad-total-1",
                              index=0, total=2, data="QUJDRA=="))
        frame = await _recv_until_type(alice, T.FILE_ERROR)
        assert frame["payload"]["transfer_id"] == "direct-bad-total-1"
        assert "total" in frame["payload"]["message"]
        receiver_error = await _recv_until_type(bob, T.FILE_ERROR)
        assert receiver_error["payload"]["transfer_id"] == "direct-bad-total-1"

        await alice.close()
        await bob.close()

    event_loop.run_until_complete(run())


def test_direct_file_done_with_bad_sha_is_rejected(server_port, event_loop):
    async def run():
        alice = await _connect(server_port, "direct_sender_bad_sha")
        bob = await _connect(server_port, "direct_receiver_bad_sha")

        await alice.send(pack(T.FILE_OFFER,
                              to="direct_receiver_bad_sha",
                              transfer_id="direct-bad-sha-1",
                              filename="x.bin", size=4,
                              mime="application/octet-stream"))
        await _recv_until_type(bob, T.FILE_OFFER)

        await alice.send(pack(T.FILE_CHUNK,
                              to="direct_receiver_bad_sha",
                              transfer_id="direct-bad-sha-1",
                              index=0, total=1, data="QUJDRA=="))
        chunk = await _recv_until_type(bob, T.FILE_CHUNK)
        assert chunk["payload"]["transfer_id"] == "direct-bad-sha-1"

        await alice.send(pack(T.FILE_DONE,
                              to="direct_receiver_bad_sha",
                              transfer_id="direct-bad-sha-1",
                              sha256="deadbeef" * 8))
        frame = await _recv_until_type(alice, T.FILE_ERROR)
        assert frame["payload"]["transfer_id"] == "direct-bad-sha-1"
        assert "sha256" in frame["payload"]["message"]
        receiver_error = await _recv_until_type(bob, T.FILE_ERROR)
        assert receiver_error["payload"]["transfer_id"] == "direct-bad-sha-1"

        await alice.close()
        await bob.close()

    event_loop.run_until_complete(run())


def test_direct_file_sender_disconnect_notifies_recipient(server_port, event_loop):
    async def run():
        alice = await _connect(server_port, "direct_sender_disconnect")
        bob = await _connect(server_port, "direct_receiver_disconnect")

        await alice.send(pack(T.FILE_OFFER,
                              to="direct_receiver_disconnect",
                              transfer_id="direct-disconnect-1",
                              filename="x.bin", size=4,
                              mime="application/octet-stream"))
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

        await alice.send(pack(T.FILE_OFFER,
                              to="nobody", transfer_id="tid4",
                              filename="x.bin", size=1, mime="application/octet-stream"))
        frame = json.loads(await asyncio.wait_for(alice.recv(), timeout=3))
        assert frame["type"] == T.ERROR

        await alice.close()

    event_loop.run_until_complete(run())


def test_room_file_share_rejects_files_larger_than_50mb(server_port, event_loop):
    async def run():
        alice = await _connect(server_port, "room_sender")
        await _create_room(alice, "BigFiles")

        await alice.send(pack(
            T.FILE_ROOM_SHARE,
            room_id="IGNORED",
            transfer_id="room-big-1",
            filename="huge.bin",
            size=51 * 1024 * 1024,
            mime="application/octet-stream",
        ))
        frame = unpack(await asyncio.wait_for(alice.recv(), timeout=3))
        assert frame["type"] == T.FILE_ROOM_ERROR
        assert "最大 50 MB" in frame["payload"]["message"]

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

        await alice.send(pack(
            T.FILE_ROOM_SHARE,
            room_id=room_id,
            transfer_id="room-secure-1",
            filename="safe.txt",
            size=4,
            mime="text/plain",
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
            data="QUJDRA==",
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

        await alice.send(pack(
            T.FILE_ROOM_SHARE,
            room_id=room_id,
            transfer_id="room-ack-1",
            filename="safe.txt",
            size=4,
            mime="text/plain",
        ))
        await _recv_until_type(bob, T.FILE_ROOM_SHARE)

        await alice.send(pack(
            T.FILE_ROOM_CHUNK,
            transfer_id="room-ack-1",
            index=0,
            total=1,
            data="QUJDRA==",
        ))
        chunk_ack = await _recv_until_type(alice, T.FILE_ROOM_CHUNK_ACK)
        assert chunk_ack["payload"]["transfer_id"] == "room-ack-1"
        assert chunk_ack["payload"]["index"] == 0

        await alice.send(pack(
            T.FILE_ROOM_DONE,
            transfer_id="room-ack-1",
            sha256="e12e115acf4552b2568b55e93cbd39394c4ef81c82447fafc997882a02d23677",
        ))
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(alice.recv(), timeout=0.5)

        await bob.send(pack(
            T.FILE_ROOM_RECEIVED,
            transfer_id="room-ack-1",
            sha256="e12e115acf4552b2568b55e93cbd39394c4ef81c82447fafc997882a02d23677",
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

        await alice.send(pack(
            T.FILE_ROOM_SHARE,
            room_id=room_id,
            transfer_id="room-bad-total-1",
            filename="safe.txt",
            size=4,
            mime="text/plain",
        ))
        await _recv_until_type(bob, T.FILE_ROOM_SHARE)

        await alice.send(pack(
            T.FILE_ROOM_CHUNK,
            transfer_id="room-bad-total-1",
            index=0,
            total=2,
            data="QUJDRA==",
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

        await alice.send(pack(
            T.FILE_ROOM_SHARE,
            room_id=room_id,
            transfer_id="room-ooo-1",
            filename="safe.txt",
            size=40000,
            mime="application/octet-stream",
        ))
        await _recv_until_type(bob, T.FILE_ROOM_SHARE)

        await alice.send(pack(
            T.FILE_ROOM_CHUNK,
            transfer_id="room-ooo-1",
            index=1,
            total=2,
            data="QQ==",
        ))
        err = await _recv_until_type(alice, T.FILE_ROOM_ERROR)
        assert err["payload"]["transfer_id"] == "room-ooo-1"
        assert "index" in err["payload"]["message"]
        receiver_error = await _recv_until_type(bob, T.FILE_ROOM_ERROR)
        assert receiver_error["payload"]["transfer_id"] == "room-ooo-1"

        await alice.close()
        await bob.close()

    event_loop.run_until_complete(run())


def test_room_file_done_with_bad_sha_is_rejected(server_port, event_loop):
    async def run():
        alice = await _connect(server_port, "room_sender_bad_sha")
        room_id = await _create_room(alice, "BadShaRoom")
        bob = await _connect(server_port, "room_receiver_bad_sha")
        await _join_room(bob, room_id)

        await alice.send(pack(
            T.FILE_ROOM_SHARE,
            room_id=room_id,
            transfer_id="room-bad-sha-1",
            filename="safe.txt",
            size=4,
            mime="text/plain",
        ))
        await _recv_until_type(bob, T.FILE_ROOM_SHARE)

        await alice.send(pack(
            T.FILE_ROOM_CHUNK,
            transfer_id="room-bad-sha-1",
            index=0,
            total=1,
            data="QUJDRA==",
        ))
        await _recv_until_type(alice, T.FILE_ROOM_CHUNK_ACK)
        chunk = await _recv_until_type(bob, T.FILE_ROOM_CHUNK)
        assert chunk["payload"]["transfer_id"] == "room-bad-sha-1"

        await alice.send(pack(
            T.FILE_ROOM_DONE,
            transfer_id="room-bad-sha-1",
            sha256="deadbeef" * 8,
        ))
        err = await _recv_until_type(alice, T.FILE_ROOM_ERROR)
        assert err["payload"]["transfer_id"] == "room-bad-sha-1"
        assert "sha256" in err["payload"]["message"]
        receiver_error = await _recv_until_type(bob, T.FILE_ROOM_ERROR)
        assert receiver_error["payload"]["transfer_id"] == "room-bad-sha-1"

        await alice.close()
        await bob.close()

    event_loop.run_until_complete(run())


def test_room_file_sender_disconnect_notifies_receivers(server_port, event_loop):
    async def run():
        alice = await _connect(server_port, "room_sender_disconnect")
        room_id = await _create_room(alice, "DisconnectRoom")
        bob = await _connect(server_port, "room_receiver_disconnect")
        await _join_room(bob, room_id)

        await alice.send(pack(
            T.FILE_ROOM_SHARE,
            room_id=room_id,
            transfer_id="room-disconnect-1",
            filename="safe.txt",
            size=4,
            mime="text/plain",
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

        await alice.send(pack(
            T.FILE_ROOM_SHARE,
            room_id=room_id,
            transfer_id="room-receiver-gone-1",
            filename="safe.txt",
            size=4,
            mime="text/plain",
        ))
        await _recv_until_type(bob, T.FILE_ROOM_SHARE)

        await alice.send(pack(
            T.FILE_ROOM_CHUNK,
            transfer_id="room-receiver-gone-1",
            index=0,
            total=1,
            data="QUJDRA==",
        ))
        await _recv_until_type(alice, T.FILE_ROOM_CHUNK_ACK)
        await _recv_until_type(bob, T.FILE_ROOM_CHUNK)

        await alice.send(pack(
            T.FILE_ROOM_DONE,
            transfer_id="room-receiver-gone-1",
            sha256="e12e115acf4552b2568b55e93cbd39394c4ef81c82447fafc997882a02d23677",
        ))
        await _recv_until_type(bob, T.FILE_ROOM_DONE)
        await bob.close()

        done_ack = await _recv_until_type(alice, T.FILE_ROOM_DONE_ACK)
        assert done_ack["payload"]["transfer_id"] == "room-receiver-gone-1"

        await alice.close()

    event_loop.run_until_complete(run())


def test_prune_stale_transfers_removes_expired_entries():
    server = ChatServer()
    now = time.time()
    server._transfer_meta = {
        "fresh": {"from_user": "alice", "last_seen": now - 5},
        "stale": {"from_user": "bob", "last_seen": now - 700},
    }

    removed = server._prune_stale_transfers(now=now, max_age=600)

    assert removed == ["stale"]
    assert "fresh" in server._transfer_meta
    assert "stale" not in server._transfer_meta
