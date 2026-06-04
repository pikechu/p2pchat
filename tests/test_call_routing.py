import asyncio, json, subprocess, sys, os, time, socket, pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import websockets.legacy.client as ws_connect
from protocol import T, pack


def _free_port():
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


def test_call_offer_routed(server_port, event_loop):
    async def run():
        alice = await _connect(server_port, "calice")
        bob   = await _connect(server_port, "cbob")
        await alice.send(pack(T.CALL_OFFER, to="cbob", room_id=""))
        frame = json.loads(await asyncio.wait_for(bob.recv(), timeout=3))
        assert frame["type"] == T.CALL_OFFER
        assert frame["payload"]["from"] == "calice"
        await alice.close(); await bob.close()
    event_loop.run_until_complete(run())


def test_call_answer_routed(server_port, event_loop):
    async def run():
        alice = await _connect(server_port, "calice2")
        bob   = await _connect(server_port, "cbob2")
        await alice.send(pack(T.CALL_OFFER, to="cbob2", room_id=""))
        await asyncio.wait_for(bob.recv(), timeout=3)
        await bob.send(pack(T.CALL_ANSWER, to="calice2"))
        frame = json.loads(await asyncio.wait_for(alice.recv(), timeout=3))
        assert frame["type"] == T.CALL_ANSWER
        assert frame["payload"]["from"] == "cbob2"
        await alice.close(); await bob.close()
    event_loop.run_until_complete(run())


def test_call_ice_routed(server_port, event_loop):
    async def run():
        alice = await _connect(server_port, "calice3")
        bob   = await _connect(server_port, "cbob3")
        candidate = {"ip": "1.2.3.4", "port": 54321}
        await alice.send(pack(T.CALL_ICE, to="cbob3", candidate=candidate))
        frame = json.loads(await asyncio.wait_for(bob.recv(), timeout=3))
        assert frame["type"] == T.CALL_ICE
        assert frame["payload"]["candidate"] == candidate
        await alice.close(); await bob.close()
    event_loop.run_until_complete(run())


def test_voice_chunk_routed(server_port, event_loop):
    async def run():
        alice = await _connect(server_port, "calice4")
        bob   = await _connect(server_port, "cbob4")
        await alice.send(pack(T.VOICE_CHUNK, to="cbob4", data="AAAA"))
        frame = json.loads(await asyncio.wait_for(bob.recv(), timeout=3))
        assert frame["type"] == T.VOICE_CHUNK
        assert frame["payload"]["data"] == "AAAA"
        await alice.close(); await bob.close()
    event_loop.run_until_complete(run())


def test_call_offer_to_unknown_returns_error(server_port, event_loop):
    async def run():
        alice = await _connect(server_port, "calice5")
        await alice.send(pack(T.CALL_OFFER, to="nobody_here", room_id=""))
        frame = json.loads(await asyncio.wait_for(alice.recv(), timeout=3))
        assert frame["type"] == T.ERROR
        await alice.close()
    event_loop.run_until_complete(run())
