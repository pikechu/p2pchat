"""文件传输端到端加密回归测试。"""

import asyncio
import hashlib
import json
import os
import pathlib
import socket
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import websockets.legacy.client as ws_connect
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from file_transfer import EncryptedFileReceiver, EncryptedFileSender
from identity import DeviceIdentity, sign_key_bundle
from protocol import CLIENT_CAPABILITIES, CLIENT_VERSION, PROTOCOL_VERSION, T, pack, unpack


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


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


async def _connect(port: int, name: str):
    ws = await ws_connect.connect(f"ws://127.0.0.1:{port}")
    identity = DeviceIdentity(Ed25519PrivateKey.generate(), X25519PrivateKey.generate())
    ephemeral = X25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    await ws.send(pack(
        T.CLIENT_HELLO,
        client_version=CLIENT_VERSION,
        protocol_version=PROTOCOL_VERSION,
        capabilities=CLIENT_CAPABILITIES,
        key_bundle=identity.public_bundle(
            ephemeral,
            sign_key_bundle(identity, ephemeral, PROTOCOL_VERSION),
            PROTOCOL_VERSION,
        ),
    ))
    frame = unpack(await asyncio.wait_for(ws.recv(), timeout=3))
    assert frame["type"] == T.SERVER_HELLO
    await ws.send(pack(T.SET_NAME, name=name))
    frame = unpack(await asyncio.wait_for(ws.recv(), timeout=3))
    assert frame["type"] == T.READY
    return ws


async def _recv_until_type(ws, expected_type, timeout=3):
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError
        frame = unpack(await asyncio.wait_for(ws.recv(), timeout=remaining))
        if frame["type"] == expected_type:
            return frame


def test_dm_relay_file_round_trips_without_plaintext_on_server_frames(server_port, event_loop, tmp_path):
    async def run():
        alice = await _connect(server_port, "file_e2e_alice")
        bob = await _connect(server_port, "file_e2e_bob")
        key = b"E" * 32
        data = b"e2e secret file payload" * 100
        source = tmp_path / "机密资料.txt"
        source.write_bytes(data)
        sender = EncryptedFileSender(
            source, key, transfer_id="file-e2e-1", scope_type="dm",
            scope_id="file-e2e-dm", sender="file_e2e_alice", recipient="file_e2e_bob",
        )
        receiver = EncryptedFileReceiver(
            tmp_path / "downloads", key, transfer_id="file-e2e-1",
            scope_type="dm", scope_id="file-e2e-dm",
            sender="file_e2e_alice", recipient="file_e2e_bob",
        )

        offer = sender.offer_payload()
        await alice.send(pack(T.FILE_OFFER, to="file_e2e_bob", transfer_id="file-e2e-1", **offer))
        offer_frame = await _recv_until_type(bob, T.FILE_OFFER)
        offer_json = json.dumps(offer_frame, ensure_ascii=False)
        assert "机密资料.txt" not in offer_json
        assert "text/plain" not in offer_json
        assert hashlib.sha256(data).hexdigest() not in offer_json
        metadata = receiver.begin(
            offer_frame["payload"]["encrypted_metadata"],
            offer_frame["payload"]["size"],
            offer_frame["payload"]["total"],
        )
        assert metadata["filename"] == "机密资料.txt"

        await bob.send(pack(T.FILE_ACCEPT, to="file_e2e_alice", transfer_id="file-e2e-1"))
        await _recv_until_type(alice, T.FILE_ACCEPT)

        while payload := sender.next_payload():
            await alice.send(pack(
                T.FILE_CHUNK,
                to="file_e2e_bob",
                transfer_id="file-e2e-1",
                index=payload["index"],
                total=payload["total"],
                encrypted_chunk=payload["encrypted_chunk"],
            ))
            chunk_frame = await _recv_until_type(bob, T.FILE_CHUNK)
            chunk_json = json.dumps(chunk_frame, ensure_ascii=False)
            assert "e2e secret file payload" not in chunk_json
            assert "data" not in chunk_frame["payload"]
            receiver.add_chunk(
                chunk_frame["payload"]["index"],
                chunk_frame["payload"]["total"],
                chunk_frame["payload"]["encrypted_chunk"],
            )

        await alice.send(pack(T.FILE_DONE, to="file_e2e_bob", transfer_id="file-e2e-1", **sender.done_payload()))
        done_frame = await _recv_until_type(bob, T.FILE_DONE)
        done_json = json.dumps(done_frame, ensure_ascii=False)
        assert hashlib.sha256(data).hexdigest() not in done_json
        assert "sha256" not in done_frame["payload"]
        save_path = receiver.finish(done_frame["payload"]["encrypted_done"])
        assert save_path.read_bytes() == data
        assert save_path.name == "机密资料.txt"

        await alice.close()
        await bob.close()

    event_loop.run_until_complete(run())
