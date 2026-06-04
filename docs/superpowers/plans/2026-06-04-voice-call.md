# Voice Call (1v1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add 1v1 voice calling to BeamChat — relay signalling through the existing WebSocket server, audio over UDP direct when possible, relay WebSocket fallback when NAT blocks direct connection.

**Architecture:** New protocol messages (CALL_OFFER/ANSWER/REJECT/HANGUP/ICE/VOICE_CHUNK) are routed user-to-user by the relay server the same way FILE_* frames are. Two new modules handle the audio engine (`voice_call.py`) and NAT traversal (`ice.py`). The floating call window (`gui/call_widget.py`) is a frameless always-on-top QWidget. Audio is raw PCM int16 at 16 kHz — no native codec DLL needed, acceptable bandwidth (~32 KB/s relay).

**Tech Stack:** `sounddevice` (audio I/O, bundles PortAudio), `numpy` (PCM array ops), Python stdlib `socket` (UDP + STUN), PyQt6 (UI).

---

## File map

| File | Action | Responsibility |
|------|--------|---------------|
| `protocol.py` | Modify | Add 6 new T enum values |
| `server.py` | Modify | Add new types to user-to-user routing block |
| `ice.py` | Create | STUN query + UDP hole-punch |
| `voice_call.py` | Create | Call state machine, audio I/O, UDP/relay send |
| `gui/call_widget.py` | Create | Floating call window + incoming-call dialog |
| `gui/theme.py` | Modify | CallWidget QSS |
| `gui/window.py` | Modify | Dispatch handlers, call button, sys messages |
| `requirements.txt` | Modify | Add sounddevice, numpy |
| `tests/test_call_routing.py` | Create | Server routes CALL_* frames correctly |
| `tests/test_ice.py` | Create | STUN response parser |
| `tests/test_voice_call_state.py` | Create | VoiceCall state machine |

---

## Task 1: Protocol messages + server routing

**Files:**
- Modify: `protocol.py`
- Modify: `server.py:358-379`
- Create: `tests/test_call_routing.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_call_routing.py
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
```

- [ ] **Step 2: Run tests — expect FAIL (T.CALL_OFFER doesn't exist yet)**

```
cd "f:\claude projects\p2pchat"
python -m pytest tests/test_call_routing.py -v
```

Expected: `AttributeError: CALL_OFFER` or `ImportError`

- [ ] **Step 3: Add message types to protocol.py**

In `protocol.py`, after the `SET_AVATAR` line (line 32), add:

```python
    # voice call (client↔server, user-to-user relay like FILE_*)
    CALL_OFFER   = "CALL_OFFER"   # {to, room_id?}
    CALL_ANSWER  = "CALL_ANSWER"  # {to}
    CALL_REJECT  = "CALL_REJECT"  # {to, reason?}
    CALL_HANGUP  = "CALL_HANGUP"  # {to}
    CALL_ICE     = "CALL_ICE"     # {to, candidate: {ip, port}}
    VOICE_CHUNK  = "VOICE_CHUNK"  # {to, data: base64 PCM int16}
```

- [ ] **Step 4: Add routing to server.py**

Find the block at line 358:
```python
elif mtype in (T.FILE_OFFER, T.FILE_ACCEPT, T.FILE_REJECT,
               T.FILE_CHUNK, T.FILE_DONE, T.FILE_ERROR):
```

Replace with:
```python
elif mtype in (T.FILE_OFFER, T.FILE_ACCEPT, T.FILE_REJECT,
               T.FILE_CHUNK, T.FILE_DONE, T.FILE_ERROR,
               T.CALL_OFFER, T.CALL_ANSWER, T.CALL_REJECT,
               T.CALL_HANGUP, T.CALL_ICE, T.VOICE_CHUNK):
```

- [ ] **Step 5: Run tests — expect PASS**

```
python -m pytest tests/test_call_routing.py -v
```

Expected: 5 passed

- [ ] **Step 6: Run full suite to check no regressions**

```
python -m pytest tests/ -q
```

Expected: all pass

- [ ] **Step 7: Commit**

```
git add protocol.py server.py tests/test_call_routing.py
git commit -m "feat: add CALL_*/VOICE_CHUNK protocol messages + server routing"
```

---

## Task 2: ice.py — STUN query + UDP hole punch

**Files:**
- Create: `ice.py`
- Create: `tests/test_ice.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ice.py
import struct, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _make_stun_response(ip: str, port: int, transaction_id: bytes) -> bytes:
    """Build a minimal STUN Binding Success Response with XOR-MAPPED-ADDRESS."""
    import socket
    magic = 0x2112A442
    xport = port ^ (magic >> 16)
    xip   = struct.unpack(">I", socket.inet_aton(ip))[0] ^ magic

    attr_body = struct.pack(">BBHI", 0x00, 0x01, xport, xip)  # pad, family, port, ip
    attr = struct.pack(">HH", 0x0020, len(attr_body)) + attr_body

    header = struct.pack(">HHI12s", 0x0101, len(attr), magic, transaction_id)
    return header + attr


def test_parse_stun_response_returns_ip_port():
    from ice import _parse_stun_response
    tid = os.urandom(12)
    data = _make_stun_response("203.0.113.5", 54321, tid)
    result = _parse_stun_response(data, tid)
    assert result == ("203.0.113.5", 54321)


def test_parse_stun_response_wrong_tid_returns_none():
    from ice import _parse_stun_response
    tid = os.urandom(12)
    data = _make_stun_response("1.2.3.4", 1234, tid)
    assert _parse_stun_response(data, b"\x00" * 12) is None


def test_parse_stun_response_too_short_returns_none():
    from ice import _parse_stun_response
    assert _parse_stun_response(b"\x00" * 10, b"\x00" * 12) is None
```

- [ ] **Step 2: Run tests — expect FAIL**

```
python -m pytest tests/test_ice.py -v
```

Expected: `ModuleNotFoundError: No module named 'ice'`

- [ ] **Step 3: Create ice.py**

```python
"""STUN query and UDP hole-punching for 1v1 NAT traversal."""
import os
import socket
import struct
import time
import threading
from typing import Optional, Tuple

STUN_HOST    = "stun.l.google.com"
STUN_PORT    = 19302
STUN_TIMEOUT = 3.0
PUNCH_PROBES = 5       # UDP packets to send per direction during hole-punch
PUNCH_DELAY  = 0.05    # seconds between probes


def get_external_address(local_sock: socket.socket) -> Optional[Tuple[str, int]]:
    """
    Query the STUN server using an existing bound UDP socket.
    Returns (external_ip, external_port) or None on failure.
    The socket is NOT closed — caller keeps using it for data.
    """
    try:
        tid = os.urandom(12)
        request = struct.pack(">HHI12s", 0x0001, 0, 0x2112A442, tid)
        stun_ip = socket.gethostbyname(STUN_HOST)
        old_timeout = local_sock.gettimeout()
        local_sock.settimeout(STUN_TIMEOUT)
        local_sock.sendto(request, (stun_ip, STUN_PORT))
        data, _ = local_sock.recvfrom(512)
        local_sock.settimeout(old_timeout)
        return _parse_stun_response(data, tid)
    except Exception:
        return None


def send_hole_punch_probes(sock: socket.socket, peer_addr: Tuple[str, int]) -> None:
    """Send PUNCH_PROBES UDP pings to peer_addr to open NAT pinholes."""
    for _ in range(PUNCH_PROBES):
        try:
            sock.sendto(b"PING", peer_addr)
        except Exception:
            break
        time.sleep(PUNCH_DELAY)


def _parse_stun_response(data: bytes, transaction_id: bytes) -> Optional[Tuple[str, int]]:
    if len(data) < 20:
        return None
    msg_type, _msg_len, magic, tid = struct.unpack(">HHI12s", data[:20])
    if msg_type != 0x0101 or tid != transaction_id:
        return None
    magic_int = 0x2112A442
    pos = 20
    while pos + 4 <= len(data):
        attr_type, attr_len = struct.unpack(">HH", data[pos:pos + 4])
        pos += 4
        if attr_type == 0x0020 and attr_len >= 8:   # XOR-MAPPED-ADDRESS
            family = data[pos + 1]
            if family == 0x01:   # IPv4
                xport, xip_int = struct.unpack(">HI", data[pos + 2:pos + 8])
                port = xport ^ (magic_int >> 16)
                ip   = socket.inet_ntoa(struct.pack(">I", xip_int ^ magic_int))
                return (ip, port)
        aligned = attr_len + (4 - attr_len % 4) % 4
        pos += aligned
    return None
```

- [ ] **Step 4: Run tests — expect PASS**

```
python -m pytest tests/test_ice.py -v
```

Expected: 3 passed

- [ ] **Step 5: Commit**

```
git add ice.py tests/test_ice.py
git commit -m "feat: add ice.py — STUN query + UDP hole-punch helpers"
```

---

## Task 3: voice_call.py — call engine

**Files:**
- Create: `voice_call.py`
- Create: `tests/test_voice_call_state.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_voice_call_state.py
"""Test VoiceCall state machine without real audio or network."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import MagicMock, patch


@pytest.fixture()
def mock_bridge():
    b = MagicMock()
    b.send_frame = MagicMock()
    return b


def _make_call(bridge):
    # Patch sounddevice so tests run headlessly
    with patch("voice_call.sd"):
        from voice_call import VoiceCall
        vc = VoiceCall.__new__(VoiceCall)
        VoiceCall.__init__(vc, bridge, "alice")
        return vc


def test_initial_state_is_idle(mock_bridge):
    from voice_call import CallState
    vc = _make_call(mock_bridge)
    assert vc.state == CallState.IDLE


def test_start_call_transitions_to_calling(mock_bridge):
    from voice_call import CallState
    vc = _make_call(mock_bridge)
    with patch.object(vc, "_start_audio"), patch.object(vc, "_start_timer"):
        vc.start_call("bob")
    assert vc.state == CallState.CALLING
    assert vc.peer == "bob"
    mock_bridge.send_frame.assert_called_once()
    args = mock_bridge.send_frame.call_args
    from protocol import T
    assert args[0][0] == T.CALL_OFFER


def test_on_call_offer_transitions_to_ringing(mock_bridge):
    from voice_call import CallState
    vc = _make_call(mock_bridge)
    vc.on_call_offer("charlie")
    assert vc.state == CallState.RINGING
    assert vc.peer == "charlie"


def test_busy_rejects_second_call(mock_bridge):
    from voice_call import CallState
    vc = _make_call(mock_bridge)
    vc.on_call_offer("bob")
    assert vc.state == CallState.RINGING
    vc.on_call_offer("carol")   # arrives while ringing
    # bob still ringing, carol was auto-rejected
    assert vc.state == CallState.RINGING
    assert vc.peer == "bob"
    from protocol import T
    reject_calls = [c for c in mock_bridge.send_frame.call_args_list
                    if c[0][0] == T.CALL_REJECT]
    assert len(reject_calls) == 1
    assert reject_calls[0][1]["to"] == "carol"


def test_hangup_from_idle_does_nothing(mock_bridge):
    from voice_call import CallState
    vc = _make_call(mock_bridge)
    vc.hangup()
    assert vc.state == CallState.IDLE
    mock_bridge.send_frame.assert_not_called()


def test_reject_sends_reject_frame(mock_bridge):
    from voice_call import CallState
    vc = _make_call(mock_bridge)
    vc.on_call_offer("dave")
    vc.reject_call("dave")
    assert vc.state == CallState.IDLE
    from protocol import T
    mock_bridge.send_frame.assert_called_once_with(T.CALL_REJECT, to="dave", reason="rejected")


def test_hangup_active_call_sends_hangup(mock_bridge):
    from voice_call import CallState
    vc = _make_call(mock_bridge)
    vc.on_call_offer("eve")
    # force to CONNECTED state directly
    vc._state = CallState.CONNECTED
    vc._start_ts = 0.0
    vc._stop_event.clear()
    vc.hangup()
    assert vc.state == CallState.IDLE
    from protocol import T
    hangup_calls = [c for c in mock_bridge.send_frame.call_args_list
                    if c[0][0] == T.CALL_HANGUP]
    assert len(hangup_calls) == 1


def test_toggle_mute(mock_bridge):
    vc = _make_call(mock_bridge)
    assert vc.is_muted is False
    result = vc.toggle_mute()
    assert result is True
    assert vc.is_muted is True
    result = vc.toggle_mute()
    assert result is False
```

- [ ] **Step 2: Run tests — expect FAIL**

```
python -m pytest tests/test_voice_call_state.py -v
```

Expected: `ModuleNotFoundError: No module named 'voice_call'`

- [ ] **Step 3: Install new dependencies**

```
pip install sounddevice numpy
```

- [ ] **Step 4: Create voice_call.py**

```python
"""
VoiceCall: 1v1 call engine.

State machine:
  IDLE → CALLING  (start_call)
  IDLE → RINGING  (on_call_offer)
  CALLING → ICE   (on_call_answer)
  RINGING → ICE   (accept_call)
  ICE → CONNECTED (hole-punch succeeds or relay timeout)
  any → IDLE      (hangup / on_call_hangup / on_call_reject)

Audio:
  sounddevice InputStream → PCM int16 → UDP direct or VOICE_CHUNK relay
  incoming PCM → jitter deque → sounddevice OutputStream
"""
import base64
import collections
import socket
import threading
import time
from enum import Enum, auto
from typing import Optional, Tuple

import numpy as np
import sounddevice as sd
from PyQt6.QtCore import QObject, pyqtSignal

from protocol import T

SAMPLE_RATE    = 16000
FRAME_MS       = 20
FRAME_SAMPLES  = SAMPLE_RATE * FRAME_MS // 1000   # 320 samples
CHANNELS       = 1
JITTER_FRAMES  = 4         # ~80 ms buffer
DIRECT_TIMEOUT = 2.5       # seconds to wait for hole punch before using relay
STALE_DIRECT   = 5.0       # seconds without UDP packet → fall back to relay


class CallState(Enum):
    IDLE      = auto()
    CALLING   = auto()
    RINGING   = auto()
    ICE       = auto()
    CONNECTED = auto()


class _Mode(Enum):
    RELAY  = "relay"
    DIRECT = "direct"


class VoiceCall(QObject):
    state_changed = pyqtSignal(str)    # CallState.name
    mode_changed  = pyqtSignal(str)    # "relay" or "direct"
    duration_tick = pyqtSignal(int)    # elapsed seconds
    call_ended    = pyqtSignal(str, int)  # reason, duration_seconds
    incoming_call = pyqtSignal(str)    # peer username

    def __init__(self, bridge, username: str, parent=None):
        super().__init__(parent)
        self._bridge   = bridge
        self._username = username
        self._state    = CallState.IDLE
        self._peer     = ""
        self._room_id  = ""
        self._mode     = _Mode.RELAY
        self._start_ts = 0.0
        self._muted    = False

        self._udp_sock:  Optional[socket.socket] = None
        self._peer_addr: Optional[Tuple[str, int]] = None

        self._in_stream:  Optional[sd.InputStream]  = None
        self._out_stream: Optional[sd.OutputStream] = None
        self._jitter: collections.deque = collections.deque(maxlen=JITTER_FRAMES * 2)

        self._stop_event = threading.Event()

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def state(self) -> CallState:
        return self._state

    @property
    def peer(self) -> str:
        return self._peer

    @property
    def is_muted(self) -> bool:
        return self._muted

    def start_call(self, peer: str, room_id: str = "") -> None:
        if self._state != CallState.IDLE:
            return
        self._peer    = peer
        self._room_id = room_id
        self._set_state(CallState.CALLING)
        self._bridge.send_frame(T.CALL_OFFER, to=peer, room_id=room_id)

    def accept_call(self) -> None:
        if self._state != CallState.RINGING:
            return
        self._bridge.send_frame(T.CALL_ANSWER, to=self._peer)
        self._set_state(CallState.ICE)
        self._start_ice()

    def reject_call(self, peer: str) -> None:
        self._bridge.send_frame(T.CALL_REJECT, to=peer, reason="rejected")
        self._set_state(CallState.IDLE)

    def hangup(self) -> None:
        if self._state == CallState.IDLE:
            return
        if self._peer:
            self._bridge.send_frame(T.CALL_HANGUP, to=self._peer)
        self._teardown("hangup")

    def toggle_mute(self) -> bool:
        self._muted = not self._muted
        return self._muted

    # ── Incoming frame handlers (called from GUI thread via signal) ───────────

    def on_call_offer(self, peer: str, room_id: str = "") -> None:
        if self._state != CallState.IDLE:
            self._bridge.send_frame(T.CALL_REJECT, to=peer, reason="busy")
            return
        self._peer    = peer
        self._room_id = room_id
        self._set_state(CallState.RINGING)
        self.incoming_call.emit(peer)

    def on_call_answer(self) -> None:
        if self._state != CallState.CALLING:
            return
        self._set_state(CallState.ICE)
        self._start_ice()

    def on_call_reject(self, reason: str = "") -> None:
        self._teardown("rejected")

    def on_call_hangup(self) -> None:
        self._teardown("remote_hangup")

    def on_call_ice(self, candidate: dict) -> None:
        ip   = str(candidate.get("ip", ""))
        port = int(candidate.get("port", 0))
        if ip and port and self._udp_sock:
            self._peer_addr = (ip, port)
            threading.Thread(target=self._send_punch_probes, daemon=True).start()

    def on_voice_chunk(self, data_b64: str) -> None:
        if self._state != CallState.CONNECTED or self._mode == _Mode.DIRECT:
            return
        try:
            pcm = base64.b64decode(data_b64)
            arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
            self._jitter.append(arr)
        except Exception:
            pass

    # ── ICE / UDP ──────────────────────────────────────────────────────────────

    def _start_ice(self) -> None:
        self._stop_event.clear()
        self._udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp_sock.bind(("0.0.0.0", 0))

        from ice import get_external_address
        ext = get_external_address(self._udp_sock)
        if ext:
            ip, port = ext
            self._bridge.send_frame(T.CALL_ICE, to=self._peer,
                                    candidate={"ip": ip, "port": port})

        threading.Thread(target=self._udp_recv_loop, daemon=True).start()
        # Ensure we connect via relay if direct never fires
        threading.Timer(DIRECT_TIMEOUT, self._ensure_connected).start()

    def _send_punch_probes(self) -> None:
        from ice import send_hole_punch_probes
        if self._udp_sock and self._peer_addr:
            send_hole_punch_probes(self._udp_sock, self._peer_addr)

    def _udp_recv_loop(self) -> None:
        self._udp_sock.settimeout(1.0)
        last_recv = 0.0
        while not self._stop_event.is_set():
            try:
                data, addr = self._udp_sock.recvfrom(8192)
            except socket.timeout:
                if (self._mode == _Mode.DIRECT and last_recv > 0
                        and time.time() - last_recv > STALE_DIRECT
                        and self._state == CallState.CONNECTED):
                    self._mode = _Mode.RELAY
                    self.mode_changed.emit("relay")
                continue
            except Exception:
                break

            if data in (b"PING", b"PONG"):
                self._peer_addr = addr
                try:
                    self._udp_sock.sendto(b"PONG", addr)
                except Exception:
                    pass
                if self._mode != _Mode.DIRECT:
                    self._mode = _Mode.DIRECT
                    self.mode_changed.emit("direct")
                self._ensure_connected()
                last_recv = time.time()
            else:
                # Audio PCM data
                if self._state == CallState.CONNECTED and self._mode == _Mode.DIRECT:
                    try:
                        arr = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
                        self._jitter.append(arr)
                    except Exception:
                        pass
                    last_recv = time.time()

    def _ensure_connected(self) -> None:
        if self._state in (CallState.ICE, CallState.CALLING):
            self._set_state(CallState.CONNECTED)
            self._start_audio()
            self._start_timer()

    # ── Audio ──────────────────────────────────────────────────────────────────

    def _start_audio(self) -> None:
        def _capture(indata, frames, time_info, status):
            if self._muted or self._state != CallState.CONNECTED:
                return
            pcm = (indata[:, 0] * 32768).astype(np.int16)
            if self._mode == _Mode.DIRECT and self._peer_addr and self._udp_sock:
                try:
                    self._udp_sock.sendto(pcm.tobytes(), self._peer_addr)
                except Exception:
                    pass
            else:
                self._bridge.send_frame(
                    T.VOICE_CHUNK, to=self._peer,
                    data=base64.b64encode(pcm.tobytes()).decode(),
                )

        def _playback(outdata, frames, time_info, status):
            if self._jitter:
                chunk = self._jitter.popleft()
                n = min(len(chunk), frames)
                outdata[:n, 0] = chunk[:n]
                if n < frames:
                    outdata[n:] = 0
            else:
                outdata.fill(0)

        self._in_stream = sd.InputStream(
            samplerate=SAMPLE_RATE, channels=CHANNELS,
            dtype="float32", blocksize=FRAME_SAMPLES, callback=_capture,
        )
        self._out_stream = sd.OutputStream(
            samplerate=SAMPLE_RATE, channels=CHANNELS,
            dtype="float32", blocksize=FRAME_SAMPLES, callback=_playback,
        )
        self._in_stream.start()
        self._out_stream.start()

    def _start_timer(self) -> None:
        self._start_ts = time.time()

        def _tick():
            while not self._stop_event.is_set() and self._state == CallState.CONNECTED:
                self.duration_tick.emit(int(time.time() - self._start_ts))
                time.sleep(1.0)

        threading.Thread(target=_tick, daemon=True).start()

    # ── Teardown ───────────────────────────────────────────────────────────────

    def _teardown(self, reason: str) -> None:
        duration = int(time.time() - self._start_ts) if self._start_ts else 0
        self._stop_event.set()
        for stream in (self._in_stream, self._out_stream):
            if stream:
                try:
                    stream.stop(); stream.close()
                except Exception:
                    pass
        self._in_stream = self._out_stream = None
        if self._udp_sock:
            try:
                self._udp_sock.close()
            except Exception:
                pass
            self._udp_sock = None
        self._peer_addr = None
        self._muted = False
        self._mode = _Mode.RELAY
        self._peer = ""
        self._room_id = ""
        self._start_ts = 0.0
        self._set_state(CallState.IDLE)
        self.call_ended.emit(reason, duration)

    def _set_state(self, state: CallState) -> None:
        self._state = state
        self.state_changed.emit(state.name)
```

- [ ] **Step 5: Run tests — expect PASS**

```
python -m pytest tests/test_voice_call_state.py -v
```

Expected: 8 passed

- [ ] **Step 6: Run full suite**

```
python -m pytest tests/ -q
```

Expected: all pass

- [ ] **Step 7: Commit**

```
git add voice_call.py ice.py tests/test_voice_call_state.py requirements.txt
git commit -m "feat: add VoiceCall engine + ICE hole-punch (voice_call.py, ice.py)"
```

---

## Task 4: gui/call_widget.py + theme

**Files:**
- Create: `gui/call_widget.py`
- Modify: `gui/theme.py`

(No unit tests — pure Qt UI. Verified visually in Task 6.)

- [ ] **Step 1: Add theme styles to gui/theme.py**

Insert before the closing `"""` of `make_qss`, after the `QMenu` block:

```python
    /* ── Call widget ──────────────────────────────────────────── */
    #CallWidget {{
        background: {t['bg_sidebar']};
        border: 1px solid {t['line_strong']};
        border-radius: 12px;
    }}
    #CallDragBar {{
        background: transparent;
    }}
    #CallPeerName {{
        font-size: 14px; font-weight: 600; color: {t['fg']};
    }}
    #CallDuration {{
        font-family: "IBM Plex Mono", monospace;
        font-size: 12px; color: {t['fg3']};
    }}
    #CallModeBadge {{
        font-family: "IBM Plex Mono", monospace;
        font-size: 11px; border-radius: 8px; padding: 2px 8px;
    }}
    #CallModeDirect {{ background: #dcfce7; color: {t['ok']}; }}
    #CallModeRelay  {{ background: #fef3c7; color: {t['warn']}; }}
    #CallHangupBtn {{
        background: {t['error']}; color: white;
        border: none; border-radius: 20px;
        font-size: 18px; min-width: 40px; min-height: 40px;
    }}
    #CallHangupBtn:hover {{ background: #b91c1c; }}
    #CallMuteBtn {{
        background: {t['bg_hover']}; color: {t['fg2']};
        border: 1px solid {t['line']}; border-radius: 20px;
        font-size: 16px; min-width: 40px; min-height: 40px;
    }}
    #CallMuteBtn:hover {{ background: {t['bg_active']}; }}
    #CallMutedBtn {{
        background: {t['warn']}; color: white;
        border: none; border-radius: 20px;
        font-size: 16px; min-width: 40px; min-height: 40px;
    }}
    #CallMutedBtn:hover {{ background: #b45309; }}
```

- [ ] **Step 2: Create gui/call_widget.py**

```python
"""Floating call window and incoming-call dialog."""
from PyQt6.QtCore import Qt, QPoint, pyqtSignal, QTimer
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QDialog,
)
from PyQt6.QtGui import QMouseEvent


def _fmt_duration(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


class CallWidget(QWidget):
    """Frameless, draggable, always-on-top call HUD."""

    hangup_requested = pyqtSignal()
    mute_toggled     = pyqtSignal()

    def __init__(self, peer: str, theme: str = "light", parent=None):
        super().__init__(parent,
                         Qt.WindowType.Window |
                         Qt.WindowType.FramelessWindowHint |
                         Qt.WindowType.WindowStaysOnTopHint)
        self.setObjectName("CallWidget")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self._drag_pos: QPoint | None = None
        self._muted = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 14)
        lay.setSpacing(8)

        # ── Drag bar / title ─────────────────────────────────────
        top = QHBoxLayout()
        top.setSpacing(8)
        drag = QLabel("📞")
        drag.setObjectName("CallDragBar")
        top.addWidget(drag)
        self._peer_lbl = QLabel(peer)
        self._peer_lbl.setObjectName("CallPeerName")
        top.addWidget(self._peer_lbl, 1)
        lay.addLayout(top)

        # ── Status row ────────────────────────────────────────────
        status = QHBoxLayout()
        status.setSpacing(8)
        self._mode_lbl = QLabel("中继 ●")
        self._mode_lbl.setObjectName("CallModeRelay")
        self._mode_lbl.setObjectName("CallModeBadge")
        status.addWidget(self._mode_lbl)
        self._dur_lbl = QLabel("00:00")
        self._dur_lbl.setObjectName("CallDuration")
        status.addWidget(self._dur_lbl)
        status.addStretch()
        lay.addLayout(status)

        # ── Buttons ───────────────────────────────────────────────
        btns = QHBoxLayout()
        btns.setSpacing(12)
        btns.addStretch()
        self._mute_btn = QPushButton("🎤")
        self._mute_btn.setObjectName("CallMuteBtn")
        self._mute_btn.setFixedSize(40, 40)
        self._mute_btn.clicked.connect(self._on_mute)
        btns.addWidget(self._mute_btn)
        hangup_btn = QPushButton("📵")
        hangup_btn.setObjectName("CallHangupBtn")
        hangup_btn.setFixedSize(40, 40)
        hangup_btn.clicked.connect(self.hangup_requested)
        btns.addWidget(hangup_btn)
        btns.addStretch()
        lay.addLayout(btns)

        self.setFixedWidth(220)
        self.adjustSize()

    # ── Public update methods ─────────────────────────────────────────────────

    def set_duration(self, seconds: int) -> None:
        self._dur_lbl.setText(_fmt_duration(seconds))

    def set_mode(self, mode: str) -> None:
        if mode == "direct":
            self._mode_lbl.setText("直连 ●")
            self._mode_lbl.setObjectName("CallModeDirect")
        else:
            self._mode_lbl.setText("中继 ●")
            self._mode_lbl.setObjectName("CallModeRelay")
        # Force QSS re-polish after objectName change
        self._mode_lbl.style().unpolish(self._mode_lbl)
        self._mode_lbl.style().polish(self._mode_lbl)

    def set_muted(self, muted: bool) -> None:
        self._muted = muted
        if muted:
            self._mute_btn.setText("🔇")
            self._mute_btn.setObjectName("CallMutedBtn")
        else:
            self._mute_btn.setText("🎤")
            self._mute_btn.setObjectName("CallMuteBtn")
        self._mute_btn.style().unpolish(self._mute_btn)
        self._mute_btn.style().polish(self._mute_btn)

    # ── Drag ──────────────────────────────────────────────────────────────────

    def mousePressEvent(self, ev: QMouseEvent) -> None:
        if ev.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = ev.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, ev: QMouseEvent) -> None:
        if self._drag_pos and ev.buttons() & Qt.MouseButton.LeftButton:
            self.move(ev.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, ev: QMouseEvent) -> None:
        self._drag_pos = None

    def _on_mute(self) -> None:
        self.mute_toggled.emit()


class IncomingCallDialog(QDialog):
    """Modal dialog shown when a CALL_OFFER arrives."""

    accepted_signal = pyqtSignal()
    rejected_signal = pyqtSignal()

    def __init__(self, peer: str, theme: str = "light", parent=None):
        super().__init__(parent,
                         Qt.WindowType.Dialog |
                         Qt.WindowType.FramelessWindowHint |
                         Qt.WindowType.WindowStaysOnTopHint)
        self.setObjectName("Dialog")
        self._peer = peer

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(14)

        lay.addWidget(QLabel(f"📞  {peer} 正在呼叫你"))

        btns = QHBoxLayout()
        btns.setSpacing(12)
        accept_btn = QPushButton("接听")
        accept_btn.setObjectName("BtnPrimary")
        accept_btn.setMinimumWidth(80)
        accept_btn.clicked.connect(self._on_accept)
        reject_btn = QPushButton("拒绝")
        reject_btn.setObjectName("BtnGhost")
        reject_btn.setMinimumWidth(80)
        reject_btn.clicked.connect(self._on_reject)
        btns.addStretch()
        btns.addWidget(accept_btn)
        btns.addWidget(reject_btn)
        btns.addStretch()
        lay.addLayout(btns)

        # Auto-reject after 15 s
        self._auto_timer = QTimer(self)
        self._auto_timer.setSingleShot(True)
        self._auto_timer.setInterval(15000)
        self._auto_timer.timeout.connect(self._on_reject)
        self._auto_timer.start()

    def _on_accept(self) -> None:
        self._auto_timer.stop()
        self.accepted_signal.emit()
        self.accept()

    def _on_reject(self) -> None:
        self._auto_timer.stop()
        self.rejected_signal.emit()
        self.reject()
```

- [ ] **Step 3: Commit**

```
git add gui/call_widget.py gui/theme.py
git commit -m "feat: add CallWidget + IncomingCallDialog + theme styles"
```

---

## Task 5: gui/window.py integration

**Files:**
- Modify: `gui/window.py`

- [ ] **Step 1: Add imports at the top of window.py**

Find the imports block. After the existing `from .widgets import (...)` block, add:

```python
from voice_call import VoiceCall, CallState
from .call_widget import CallWidget, IncomingCallDialog
```

- [ ] **Step 2: Add T enum values for new messages**

Find where `T` is used in the dispatch (around line 1639). The T enum is already imported from protocol. No change needed — they're already in protocol.py from Task 1.

- [ ] **Step 3: Initialise VoiceCall in MainWindow.__init__**

In `MainWindow.__init__`, after the line `self._ft_cards: dict[str, "FileCard"] = {}` (around line 1456), add:

```python
        # Voice call state
        self._voice_call: VoiceCall | None = None   # created after bridge is ready
        self._call_widget: CallWidget | None = None
        self._incoming_dlg: IncomingCallDialog | None = None
```

- [ ] **Step 4: Create VoiceCall after bridge connects**

Find `_on_connected` (the method called when `self._bridge.connected` fires). Add at the top of its body:

```python
        if self._voice_call is None:
            self._voice_call = VoiceCall(self._bridge, self._username, self)
            self._voice_call.incoming_call.connect(self._on_incoming_call)
            self._voice_call.call_ended.connect(self._on_call_ended)
            self._voice_call.state_changed.connect(self._on_call_state_changed)
            self._voice_call.mode_changed.connect(self._on_call_mode_changed)
            self._voice_call.duration_tick.connect(self._on_call_duration)
```

- [ ] **Step 5: Add CALL_* dispatch handlers to _dispatch_frame**

Find `elif mtype == T.USER_AVATAR:` near the end of `_dispatch_frame`. Before it, add:

```python
        elif mtype == T.CALL_OFFER:
            peer    = payload.get("from", "")
            room_id = payload.get("room_id", "")
            if self._voice_call:
                self._voice_call.on_call_offer(peer, room_id)

        elif mtype == T.CALL_ANSWER:
            if self._voice_call:
                self._voice_call.on_call_answer()

        elif mtype == T.CALL_REJECT:
            reason = payload.get("reason", "")
            if self._voice_call:
                self._voice_call.on_call_reject(reason)

        elif mtype == T.CALL_HANGUP:
            if self._voice_call:
                self._voice_call.on_call_hangup()

        elif mtype == T.CALL_ICE:
            candidate = payload.get("candidate", {})
            if self._voice_call:
                self._voice_call.on_call_ice(candidate)

        elif mtype == T.VOICE_CHUNK:
            data = payload.get("data", "")
            if self._voice_call:
                self._voice_call.on_voice_chunk(data)
```

- [ ] **Step 6: Add VoiceCall slot methods to MainWindow**

Add these methods after `_on_user_avatar`:

```python
    # ── Voice call slots ──────────────────────────────────────────────────────

    def _on_incoming_call(self, peer: str) -> None:
        QApplication.beep()
        self._incoming_dlg = IncomingCallDialog(peer, self._theme, self)
        self._incoming_dlg.setStyleSheet(self.styleSheet())
        self._incoming_dlg.accepted_signal.connect(
            lambda: self._voice_call.accept_call() if self._voice_call else None
        )
        self._incoming_dlg.rejected_signal.connect(
            lambda: self._voice_call.reject_call(peer) if self._voice_call else None
        )
        self._incoming_dlg.show()

    def _on_call_state_changed(self, state_name: str) -> None:
        if state_name == "CONNECTED":
            if self._incoming_dlg:
                self._incoming_dlg.close()
                self._incoming_dlg = None
            peer = self._voice_call.peer if self._voice_call else ""
            self._call_widget = CallWidget(peer, self._theme)
            self._call_widget.setStyleSheet(self.styleSheet())
            self._call_widget.hangup_requested.connect(
                lambda: self._voice_call.hangup() if self._voice_call else None
            )
            self._call_widget.mute_toggled.connect(self._on_call_mute_toggled)
            # Position in bottom-right of main window
            geo = self.geometry()
            self._call_widget.move(
                geo.right() - self._call_widget.width() - 20,
                geo.bottom() - self._call_widget.height() - 60,
            )
            self._call_widget.show()
        elif state_name == "IDLE":
            if self._call_widget:
                self._call_widget.close()
                self._call_widget = None
            if self._incoming_dlg:
                self._incoming_dlg.close()
                self._incoming_dlg = None

    def _on_call_mode_changed(self, mode: str) -> None:
        if self._call_widget:
            self._call_widget.set_mode(mode)

    def _on_call_duration(self, seconds: int) -> None:
        if self._call_widget:
            self._call_widget.set_duration(seconds)

    def _on_call_mute_toggled(self) -> None:
        if self._voice_call:
            muted = self._voice_call.toggle_mute()
            if self._call_widget:
                self._call_widget.set_muted(muted)

    def _on_call_ended(self, reason: str, duration: int) -> None:
        """Write a system message into the room/DM where call originated."""
        room_id = self._voice_call._room_id if self._voice_call else ""
        if not room_id:
            room_id = self._chat.current_room_id or ""
        if not room_id:
            return
        msgs = self._active_msgs()
        if not msgs:
            return
        if reason == "hangup" or reason == "remote_hangup":
            m, s = divmod(duration, 60)
            msgs.add_sys(f"📞 通话时长 {m:02d}:{s:02d}")
        elif reason == "rejected":
            msgs.add_sys("📵 通话未接通")
        # "busy" from incoming: the other side was busy (we called them)

    def _start_voice_call(self, peer: str) -> None:
        """Initiate a call to peer, using current room_id as context."""
        if not self._voice_call:
            return
        if self._voice_call.state != CallState.IDLE:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.information(self, "通话中", "当前已有通话进行中。")
            return
        room_id = self._chat.current_room_id or ""
        self._voice_call.start_call(peer, room_id)
```

- [ ] **Step 7: Add call button to _show_peers_dialog**

Find `_show_peers_dialog`. Replace the inner list section (the part that creates `list_w` and `_on_double_click`) with:

```python
        if not users:
            lay.addWidget(_lbl("No users online right now.", "EmptyDesc"))
        else:
            list_w = QListWidget()
            for u in users:
                item = QListWidgetItem(f"{u}（我）" if u == self._username else u)
                item.setData(Qt.ItemDataRole.UserRole, u)
                list_w.addItem(item)

            def _on_double_click(item):
                uid = item.data(Qt.ItemDataRole.UserRole)
                if uid != self._username:
                    self._start_dm(uid)
                    dlg.accept()

            def _on_call_click():
                items = list_w.selectedItems()
                if not items:
                    return
                uid = items[0].data(Qt.ItemDataRole.UserRole)
                if uid and uid != self._username:
                    self._start_voice_call(uid)
                    dlg.accept()

            list_w.itemDoubleClicked.connect(_on_double_click)
            lay.addWidget(list_w)

            call_btn = _btn("📞 发起通话", "BtnGhost")
            call_btn.setMinimumWidth(112)
            call_btn.clicked.connect(_on_call_click)
            lay.addWidget(call_btn)

            hint = _lbl("双击发私信，选中后点按钮发起通话", "FormLabel")
            hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lay.addWidget(hint)
```

- [ ] **Step 8: Run full test suite**

```
python -m pytest tests/ -q
```

Expected: all pass (no window.py unit tests — verified manually in Task 6)

- [ ] **Step 9: Commit**

```
git add gui/window.py
git commit -m "feat: integrate VoiceCall into MainWindow — dispatch, UI, sys messages"
```

---

## Task 6: requirements.txt + PyInstaller hidden imports

**Files:**
- Modify: `requirements.txt`
- Modify: `build.py`

- [ ] **Step 1: Update requirements.txt**

```
websockets>=12.0
cryptography>=41.0
rich>=13.0
PyQt6>=6.6.0
sounddevice>=0.4.6
numpy>=1.26.0
```

- [ ] **Step 2: Check how build.py configures PyInstaller hidden imports**

```
python -m pytest tests/ -q && python -c "import sounddevice, numpy; print('deps ok')"
```

Expected: `deps ok`

- [ ] **Step 3: Add hidden imports to build.py**

Read build.py to find where `--hidden-import` args are passed (look for `pyinstaller_args` or similar list), then add:

```python
"--hidden-import=sounddevice",
"--hidden-import=numpy",
"--hidden-import=_sounddevice_data",
```

If build.py calls PyInstaller as a list like:
```python
cmd = ["pyinstaller", "--onefile", ...]
```
then add those three `--hidden-import` entries to that list.

- [ ] **Step 4: Commit**

```
git add requirements.txt build.py
git commit -m "chore: add sounddevice + numpy deps, PyInstaller hidden imports"
```

---

## Task 7: Bump version + build + release

- [ ] **Step 1: Run full test suite one final time**

```
python -m pytest tests/ -q
```

Expected: all pass

- [ ] **Step 2: Bump version**

Edit `version.py`:
```python
__version__ = "1.1.0"
```

- [ ] **Step 3: Build**

```
python build.py
```

Expected: `OK: BeamChat.exe   ~40 MB`

- [ ] **Step 4: Commit + tag**

```
git add version.py
git commit -m "chore: bump version to v1.1.0 — 1v1 voice calls"
git tag v1.1.0
git push && git push origin v1.1.0
```

---

## Self-review checklist

- [x] **Spec coverage**: protocol ✓, server routing ✓, STUN ✓, hole-punch ✓, relay fallback ✓, audio ✓, jitter buffer ✓, call widget ✓, incoming dialog ✓, sys messages ✓, call button in user list ✓
- [x] **Placeholder scan**: No TBD/TODO — all code blocks complete
- [x] **Type consistency**: `VoiceCall.accept_call()` takes no args (peer is stored from `on_call_offer`) — consistent across Tasks 3, 5
- [x] **Mode badge re-polish**: `set_mode()` calls `unpolish/polish` after `setObjectName` — QSS will apply correctly
- [x] **Button text width**: "📞 发起通话" = emoji (16px) + 5 chars × ~14px + 32px padding = ~118px → `setMinimumWidth(112)` is safe
