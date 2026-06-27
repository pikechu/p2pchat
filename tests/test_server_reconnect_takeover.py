import asyncio
import os
import socket
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import websockets.legacy.client as ws_connect
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


async def _connect_raw(port):
    ws = await ws_connect.connect(f"ws://127.0.0.1:{port}")
    await ws.recv()
    return ws


def test_same_name_reconnect_takes_over_old_socket(server_port, event_loop):
    async def run():
        ws1 = await _connect_raw(server_port)
        await ws1.send(pack(T.SET_NAME, name="pp"))
        first = unpack(await asyncio.wait_for(ws1.recv(), timeout=3))
        assert first["type"] == T.SYSTEM

        ws2 = await _connect_raw(server_port)
        await ws2.send(pack(T.SET_NAME, name="pp"))
        second = unpack(await asyncio.wait_for(ws2.recv(), timeout=3))
        assert second["type"] == T.SYSTEM
        assert second["payload"]["message"] == "Name set to 'pp'"

        with pytest.raises(Exception):
            await asyncio.wait_for(ws1.recv(), timeout=1)

        await ws2.send(pack(T.LIST_ROOMS))
        listed = unpack(await asyncio.wait_for(ws2.recv(), timeout=3))
        assert listed["type"] == T.ROOM_LIST

        await ws2.close()

    event_loop.run_until_complete(run())
