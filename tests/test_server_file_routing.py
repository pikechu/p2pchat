"""Integration test: server routes FILE_* frames user-to-user."""
import asyncio, json, subprocess, sys, os, time, socket, pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import websockets
from protocol import T, pack, unpack


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
    ws = await websockets.connect(f"ws://127.0.0.1:{port}")
    await ws.recv()   # WELCOME
    await ws.send(pack(T.SET_NAME, name=name))
    await ws.recv()   # SYSTEM
    return ws


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
