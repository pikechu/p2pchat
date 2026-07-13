"""严格握手与在线公开密钥目录的集成测试。"""
import asyncio
import pytest
import websockets
import websockets.legacy.client as ws_connect
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from identity import DeviceIdentity, sign_key_bundle, verify_key_bundle
from protocol import CLIENT_CAPABILITIES, CLIENT_VERSION, PROTOCOL_VERSION, REQUIRED_CAPABILITIES, T, pack, unpack
from server import ChatServer


def _hello_payload(*, capabilities=None, protocol_version=PROTOCOL_VERSION, key_bundle=None):
    identity = DeviceIdentity(Ed25519PrivateKey.generate(), X25519PrivateKey.generate())
    ephemeral = X25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return {
        "client_version": CLIENT_VERSION,
        "protocol_version": protocol_version,
        "capabilities": CLIENT_CAPABILITIES if capabilities is None else capabilities,
        "key_bundle": key_bundle or identity.public_bundle(
            ephemeral, sign_key_bundle(identity, ephemeral, protocol_version), protocol_version
        ),
    }


def _invalid_base64_bundle():
    bundle = _hello_payload()["key_bundle"]
    bundle["identity_public"] = "not base64!"
    return bundle


@pytest.fixture()
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


async def _ready(port, name):
    ws = await ws_connect.connect(f"ws://127.0.0.1:{port}")
    await ws.send(pack(T.CLIENT_HELLO, **_hello_payload()))
    assert unpack(await ws.recv())["type"] == T.SERVER_HELLO
    await ws.send(pack(T.SET_NAME, name=name))
    assert unpack(await ws.recv())["type"] == T.READY
    return ws


async def _with_server(test):
    server = ChatServer(enable_message_persistence=False)
    async with websockets.serve(server.handle, "127.0.0.1", 0) as listening:
        await test(listening.sockets[0].getsockname()[1])


def test_server_requires_client_hello_as_first_frame(event_loop):
    async def test(port):
        ws = await ws_connect.connect(f"ws://127.0.0.1:{port}")
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(ws.recv(), timeout=0.1)
        await ws.send(pack(T.SET_NAME, name="late"))
        error = unpack(await ws.recv())
        assert error["type"] == T.ERROR
        assert error["payload"]["code"] == "PROTOCOL_INCOMPATIBLE"
        assert error["payload"]["recoverable"] is False
        await ws.close()
    event_loop.run_until_complete(_with_server(test))


@pytest.mark.parametrize("payload", [
    lambda: _hello_payload(capabilities=[c for c in CLIENT_CAPABILITIES if c != "ttl_policy"]),
    lambda: _hello_payload(protocol_version=PROTOCOL_VERSION - 1),
    lambda: _hello_payload(key_bundle=_invalid_base64_bundle()),
])
def test_invalid_hello_returns_structured_error(event_loop, payload):
    async def test(port):
        ws = await ws_connect.connect(f"ws://127.0.0.1:{port}")
        await ws.send(pack(T.CLIENT_HELLO, **payload()))
        error = unpack(await ws.recv())
        assert error["type"] == T.ERROR
        assert error["payload"]["code"] == "PROTOCOL_INCOMPATIBLE"
        assert error["payload"]["recoverable"] is False
        await ws.close()
    event_loop.run_until_complete(_with_server(test))


def test_ready_user_can_request_online_peer_bundle(event_loop):
    async def test(port):
        alice = await _ready(port, "alice")
        bob = await _ready(port, "bob")
        await alice.send(pack(T.GET_PEER_KEY, name="bob"))
        frame = unpack(await alice.recv())
        assert frame["type"] == T.PEER_KEY_BUNDLE
        assert frame["payload"]["name"] == "bob"
        assert verify_key_bundle(frame["payload"]["key_bundle"], PROTOCOL_VERSION)
        await alice.close()
        await bob.close()
    event_loop.run_until_complete(_with_server(test))


def test_required_security_capabilities_are_enough_for_hello(event_loop):
    async def test(port):
        ws = await ws_connect.connect(f"ws://127.0.0.1:{port}")
        await ws.send(pack(T.CLIENT_HELLO, **_hello_payload(capabilities=REQUIRED_CAPABILITIES)))
        frame = unpack(await ws.recv())
        assert frame["type"] == T.SERVER_HELLO
        await ws.close()
    event_loop.run_until_complete(_with_server(test))


def test_peer_key_directory_removes_disconnected_user(event_loop):
    async def test(port):
        alice = await _ready(port, "alice")
        bob = await _ready(port, "bob")
        await bob.close()
        await asyncio.sleep(0.05)

        await alice.send(pack(T.GET_PEER_KEY, name="bob"))
        frame = unpack(await alice.recv())
        assert frame["type"] == T.ERROR
        assert frame["payload"]["code"] == "PEER_KEY_UNAVAILABLE"
        assert frame["payload"]["name"] == "bob"
        await alice.close()
    event_loop.run_until_complete(_with_server(test))


def test_unready_user_cannot_request_peer_bundle(event_loop):
    async def test(port):
        bob = await _ready(port, "bob")
        ws = await ws_connect.connect(f"ws://127.0.0.1:{port}")
        await ws.send(pack(T.CLIENT_HELLO, **_hello_payload()))
        assert unpack(await ws.recv())["type"] == T.SERVER_HELLO
        await ws.send(pack(T.GET_PEER_KEY, name="bob"))
        error = unpack(await ws.recv())
        assert error["type"] == T.ERROR
        assert error["payload"]["code"] == "HANDSHAKE_NOT_READY"
        await ws.close()
        await bob.close()
    event_loop.run_until_complete(_with_server(test))


def test_reconnect_replaces_ephemeral_key_with_valid_signature(event_loop):
    async def identify(port, payload):
        ws = await ws_connect.connect(f"ws://127.0.0.1:{port}")
        await ws.send(pack(T.CLIENT_HELLO, **payload))
        assert unpack(await ws.recv())["type"] == T.SERVER_HELLO
        await ws.send(pack(T.SET_NAME, name="reconnect_user"))
        assert unpack(await ws.recv())["type"] == T.READY
        return ws

    async def test(port):
        first = _hello_payload()
        first_ws = await identify(port, first)
        await first_ws.close()
        await asyncio.sleep(0)
        second = _hello_payload()
        second_ws = await identify(port, second)
        assert first["key_bundle"]["ephemeral_public"] != second["key_bundle"]["ephemeral_public"]
        assert verify_key_bundle(second["key_bundle"], PROTOCOL_VERSION)
        await second_ws.close()
    event_loop.run_until_complete(_with_server(test))
