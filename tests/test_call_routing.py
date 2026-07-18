import asyncio, json, subprocess, sys, os, time, socket, pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import websockets.legacy.client as ws_connect
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from identity import DeviceIdentity, sign_key_bundle
from protocol import CLIENT_CAPABILITIES, CLIENT_VERSION, PROTOCOL_VERSION, T, pack


def _free_port():
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
    await ws.recv()   # SERVER_HELLO
    await ws.send(pack(T.SET_NAME, name=name))
    await ws.recv()   # READY
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


def test_voice_chunk_rejects_legacy_plaintext_payload(server_port, event_loop):
    async def run():
        alice = await _connect(server_port, "calice4")
        bob   = await _connect(server_port, "cbob4")
        await alice.send(pack(T.VOICE_CHUNK, to="cbob4", data="AAAA"))
        frame = json.loads(await asyncio.wait_for(alice.recv(), timeout=3))
        assert frame["type"] == T.ERROR
        assert frame["payload"]["code"] == "PLAINTEXT_FORBIDDEN"
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(bob.recv(), timeout=0.2)
        await alice.close(); await bob.close()
    event_loop.run_until_complete(run())


def test_encrypted_voice_chunk_routed(server_port, event_loop):
    async def run():
        alice = await _connect(server_port, "calice4enc")
        bob   = await _connect(server_port, "cbob4enc")
        voice = {
            "version": 1,
            "alg": "VOICE-AEAD-v1",
            "call_id": "call-1",
            "sender": "calice4enc",
            "recipient": "cbob4enc",
            "direction": "calice4enc->cbob4enc",
            "seq": 0,
            "nonce": "AAAAAAAAAAAAAAAA",
            "ciphertext": "ZW5jcnlwdGVk",
        }
        await alice.send(pack(T.VOICE_CHUNK, to="cbob4enc", voice=voice))
        frame = json.loads(await asyncio.wait_for(bob.recv(), timeout=3))
        assert frame["type"] == T.VOICE_CHUNK
        assert frame["payload"]["voice"] == voice
        assert frame["payload"]["from"] == "calice4enc"
        await alice.close(); await bob.close()
    event_loop.run_until_complete(run())


def test_voice_chunk_rejects_sender_recipient_context_mismatch(server_port, event_loop):
    async def run():
        alice = await _connect(server_port, "calice_ctx")
        bob   = await _connect(server_port, "cbob_ctx")
        voice = {
            "version": 1,
            "alg": "VOICE-AEAD-v1",
            "call_id": "call-ctx",
            "sender": "mallory",
            "recipient": "cbob_ctx",
            "direction": "mallory->cbob_ctx",
            "seq": 0,
            "nonce": "AAAAAAAAAAAAAAAA",
            "ciphertext": "ZW5jcnlwdGVk",
        }
        await alice.send(pack(T.VOICE_CHUNK, to="cbob_ctx", voice=voice))
        frame = json.loads(await asyncio.wait_for(alice.recv(), timeout=3))
        assert frame["type"] == T.ERROR
        assert frame["payload"]["code"] == "VOICE_CONTEXT_MISMATCH"
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(bob.recv(), timeout=0.2)
        await alice.close(); await bob.close()
    event_loop.run_until_complete(run())


def test_call_offer_to_unknown_returns_error(server_port, event_loop):
    async def run():
        alice = await _connect(server_port, "calice5")
        await alice.send(pack(
            T.CALL_OFFER, to="nobody_here", room_id="", call_id="missing-call"
        ))
        frame = json.loads(await asyncio.wait_for(alice.recv(), timeout=3))
        assert frame["type"] == T.ERROR
        assert frame["payload"]["code"] == "CALL_UNREACHABLE"
        assert frame["payload"]["call_id"] == "missing-call"
        await alice.close()
    event_loop.run_until_complete(run())
