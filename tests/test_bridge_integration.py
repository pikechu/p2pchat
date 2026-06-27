"""
Integration tests: WSBridge connects to a real server and sends/receives frames.
Verifies the create-room / join-room GUI flow end-to-end.
"""
import os
import socket
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from protocol import T, pack, unpack

# Skip the whole module if PyQt6 is not installed
pytest.importorskip("PyQt6")

from PyQt6.QtCore import QCoreApplication, QEventLoop, QTimer
from gui.bridge import WSBridge


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def app():
    """One QCoreApplication per test session."""
    a = QCoreApplication.instance() or QCoreApplication(sys.argv)
    yield a


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


def _wait_for_signal(signal, timeout_ms: int = 3000) -> list:
    """Block the Qt event loop until signal fires or timeout. Returns captured args."""
    captured = []
    loop = QEventLoop()

    def _on_signal(*args):
        captured.extend(args)
        loop.quit()

    signal.connect(_on_signal)
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()
    return captured


def _make_bridge(server_port: int) -> WSBridge:
    bridge = WSBridge(f"ws://127.0.0.1:{server_port}")
    bridge.start()
    # Wait for connected signal
    _wait_for_signal(bridge.connected, timeout_ms=3000)
    return bridge


def _send_and_wait(bridge: WSBridge, msg_type, expected_type: str,
                   timeout_ms: int = 3000, **kwargs) -> dict | None:
    """Send a frame and wait for the first received frame of expected_type."""
    captured = []
    loop = QEventLoop()

    def _on_received(raw: str):
        frame = unpack(raw)
        if frame.get("type") == expected_type:
            captured.append(frame)
            loop.quit()

    bridge.received.connect(_on_received)
    bridge.send_frame(msg_type, **kwargs)
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()
    bridge.received.disconnect(_on_received)
    return captured[0] if captured else None


# ── tests ─────────────────────────────────────────────────────────────────────

def test_bridge_connects_and_receives_welcome(app, server_port):
    bridge = WSBridge(f"ws://127.0.0.1:{server_port}")
    received = []
    bridge.received.connect(lambda raw: received.append(unpack(raw)))

    connected_flag = []
    bridge.connected.connect(lambda: connected_flag.append(True))

    bridge.start()
    _wait_for_signal(bridge.connected, timeout_ms=3000)

    assert connected_flag, "bridge should emit connected"

    # Give the WELCOME frame a moment to arrive
    QTimer.singleShot(200, lambda: None)
    loop = QEventLoop()
    QTimer.singleShot(400, loop.quit)
    loop.exec()

    assert any(f.get("type") == T.WELCOME for f in received), \
        "should receive WELCOME frame on connect"

    bridge.close()
    bridge.wait(2000)


def test_bridge_set_name_receives_system(app, server_port):
    bridge = _make_bridge(server_port)
    frame = _send_and_wait(bridge, T.SET_NAME, T.SYSTEM, name="test_user_br")
    assert frame is not None, "should receive SYSTEM after SET_NAME"
    bridge.close()
    bridge.wait(2000)


def test_bridge_create_room_receives_room_created(app, server_port):
    bridge = _make_bridge(server_port)
    bridge.send_frame(T.SET_NAME, name="creator_br")
    time.sleep(0.1)

    frame = _send_and_wait(bridge, T.CREATE_ROOM, T.ROOM_CREATED, name="BridgeRoom")
    assert frame is not None, "should receive ROOM_CREATED after CREATE_ROOM"
    assert "room_id" in frame["payload"]
    assert len(frame["payload"]["room_id"]) == 6
    assert frame["payload"]["name"] == "BridgeRoom"

    bridge.close()
    bridge.wait(2000)


def test_bridge_create_room_with_password_locked(app, server_port):
    bridge = _make_bridge(server_port)
    bridge.send_frame(T.SET_NAME, name="creator_pw_br")
    time.sleep(0.1)

    frame = _send_and_wait(bridge, T.CREATE_ROOM, T.ROOM_CREATED,
                           name="SecretRoom", password="pw123")
    assert frame is not None
    assert frame["payload"]["locked"] is True

    bridge.close()
    bridge.wait(2000)


def test_bridge_join_room_receives_room_joined(app, server_port):
    # Creator
    creator = _make_bridge(server_port)
    creator.send_frame(T.SET_NAME, name="host_br")
    time.sleep(0.1)
    frame = _send_and_wait(creator, T.CREATE_ROOM, T.ROOM_CREATED, name="JoinTest")
    assert frame is not None
    room_id = frame["payload"]["room_id"]

    # Joiner
    joiner = _make_bridge(server_port)
    joiner.send_frame(T.SET_NAME, name="guest_br")
    time.sleep(0.1)
    joined = _send_and_wait(joiner, T.JOIN_ROOM, T.ROOM_JOINED, room_id=room_id)
    assert joined is not None
    assert joined["payload"]["room_id"] == room_id

    creator.close()
    joiner.close()
    creator.wait(2000)
    joiner.wait(2000)


def test_bridge_join_wrong_password_receives_error(app, server_port):
    creator = _make_bridge(server_port)
    creator.send_frame(T.SET_NAME, name="host_pw2_br")
    time.sleep(0.1)
    frame = _send_and_wait(creator, T.CREATE_ROOM, T.ROOM_CREATED,
                           name="Locked", password="correct")
    room_id = frame["payload"]["room_id"]

    joiner = _make_bridge(server_port)
    joiner.send_frame(T.SET_NAME, name="guest_pw2_br")
    time.sleep(0.1)
    err = _send_and_wait(joiner, T.JOIN_ROOM, T.ERROR,
                         room_id=room_id, password="wrong")
    assert err is not None, "should receive ERROR for wrong password"

    creator.close()
    joiner.close()
    creator.wait(2000)
    joiner.wait(2000)


def test_bridge_send_frame_returns_false_when_not_connected(app):
    bridge = WSBridge("ws://127.0.0.1:1")   # nothing listening
    # Don't start — _queue is None
    result = bridge.send_frame(T.SET_NAME, name="x")
    assert result is False


def test_bridge_send_raw_frame_returns_false_when_not_connected(app):
    bridge = WSBridge("ws://127.0.0.1:1")
    result = bridge.send_raw_frame(pack(T.SET_NAME, name="x"))
    assert result is False


def test_bridge_reports_connected_state(app, server_port):
    bridge = _make_bridge(server_port)
    assert bridge.is_connected() is True
    bridge.close()
    bridge.wait(2000)
