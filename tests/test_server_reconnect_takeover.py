import asyncio
import os
import socket
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import websockets.legacy.client as ws_connect
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from identity import DeviceIdentity, sign_key_bundle
from protocol import CLIENT_CAPABILITIES, CLIENT_VERSION, PROTOCOL_VERSION, T, pack, unpack


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


async def _connect_raw(port, identity=None):
    ws = await ws_connect.connect(f"ws://127.0.0.1:{port}")
    identity = identity or DeviceIdentity(Ed25519PrivateKey.generate(), X25519PrivateKey.generate())
    ephemeral = X25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    await ws.send(pack(T.CLIENT_HELLO,
                       client_version=CLIENT_VERSION,
                       protocol_version=PROTOCOL_VERSION,
                       capabilities=CLIENT_CAPABILITIES,
                       key_bundle=identity.public_bundle(
                           ephemeral, sign_key_bundle(identity, ephemeral, PROTOCOL_VERSION), PROTOCOL_VERSION
                       )))
    await ws.recv()
    return ws


def test_same_name_reconnect_takes_over_old_socket(server_port, event_loop):
    async def run():
        identity = DeviceIdentity(Ed25519PrivateKey.generate(), X25519PrivateKey.generate())
        ws1 = await _connect_raw(server_port, identity)
        await ws1.send(pack(T.SET_NAME, name="pp"))
        first = unpack(await asyncio.wait_for(ws1.recv(), timeout=3))
        assert first["type"] == T.READY

        ws2 = await _connect_raw(server_port, identity)
        await ws2.send(pack(T.SET_NAME, name="pp"))
        second = unpack(await asyncio.wait_for(ws2.recv(), timeout=3))
        assert second["type"] == T.READY

        with pytest.raises(Exception):
            await asyncio.wait_for(ws1.recv(), timeout=1)

        await ws2.send(pack(T.LIST_ROOMS))
        listed = unpack(await asyncio.wait_for(ws2.recv(), timeout=3))
        assert listed["type"] == T.ROOM_LIST

        await ws2.close()

    event_loop.run_until_complete(run())


def test_different_identity_cannot_take_over_online_username(server_port, event_loop):
    async def run():
        ws1 = await _connect_raw(server_port)
        await ws1.send(pack(T.SET_NAME, name="identity_bound_user"))
        assert unpack(await asyncio.wait_for(ws1.recv(), timeout=3))["type"] == T.READY

        ws2 = await _connect_raw(server_port)
        await ws2.send(pack(T.SET_NAME, name="identity_bound_user"))
        rejected = unpack(await asyncio.wait_for(ws2.recv(), timeout=3))
        assert rejected["type"] == T.ERROR
        assert rejected["payload"]["code"] == "USERNAME_IDENTITY_MISMATCH"

        await ws1.send(pack(T.LIST_ROOMS))
        assert unpack(await asyncio.wait_for(ws1.recv(), timeout=3))["type"] == T.ROOM_LIST
        await ws1.close()
        await ws2.close()

    event_loop.run_until_complete(run())
