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
