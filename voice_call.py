"""
VoiceCall: 1v1 call engine.

状态机：
  IDLE → CALLING（发起呼叫）
  IDLE → RINGING（收到来电）
  CALLING/RINGING → ICE（建立媒体通道）
  ICE → CONNECTED（本地音频启动且双方均报告媒体就绪）
  任意状态 → IDLE（挂断、拒绝、错误或断线）

Audio:
  sounddevice InputStream → PCM int16 → encrypted UDP direct or VOICE_CHUNK relay
  incoming encrypted voice packet → PCM → jitter deque → sounddevice OutputStream
"""
import collections
import socket
import threading
import time
import uuid
from enum import Enum, auto
from typing import Callable, Optional, Tuple

import numpy as np
import sounddevice as sd
from PyQt6.QtCore import QObject, pyqtSignal

from protocol import T
from voice_crypto import VoiceCipher, VoiceCryptoError, encode_voice_packet

SAMPLE_RATE    = 16000
FRAME_MS       = 20
FRAME_SAMPLES  = SAMPLE_RATE * FRAME_MS // 1000   # 320 samples
CHANNELS       = 1
JITTER_FRAMES  = 4         # ~80 ms buffer
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
    call_ended    = pyqtSignal(str, int, str, str)  # reason, duration, room_id, peer
    incoming_call = pyqtSignal(str)    # peer username
    audio_error   = pyqtSignal(str)
    remote_mute_changed = pyqtSignal(bool)

    def __init__(
        self,
        bridge,
        username: str,
        parent=None,
        *,
        voice_key_provider: Optional[Callable[[str, str, str], bytes | None]] = None,
    ):
        super().__init__(parent)
        self._bridge   = bridge
        self._username = username
        self._state    = CallState.IDLE
        self._peer     = ""
        self._room_id  = ""
        self._call_id  = ""
        self._mode     = _Mode.RELAY
        self._start_ts = 0.0
        self._muted    = False
        self._local_media_ready = False
        self._remote_media_ready = False
        self._voice_key_provider = voice_key_provider
        self._tx_voice_cipher: Optional[VoiceCipher] = None
        self._rx_voice_cipher: Optional[VoiceCipher] = None

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

    @property
    def room_id(self) -> str:
        return self._room_id

    @property
    def call_id(self) -> str:
        return self._call_id

    def set_bridge(self, bridge) -> None:
        """重连后切换到当前有效的 WebSocket bridge。"""
        self._bridge = bridge

    def start_call(self, peer: str, room_id: str = "") -> bool:
        if self._state != CallState.IDLE:
            return False
        self._peer    = peer
        self._room_id = room_id
        self._call_id = uuid.uuid4().hex
        self._reset_voice_ciphers()
        self._set_state(CallState.CALLING)
        if not self._bridge.send_frame(
            T.CALL_OFFER, to=peer, room_id=room_id, call_id=self._call_id
        ):
            self._teardown("send_error")
            return False
        return True

    def accept_call(self) -> None:
        if self._state != CallState.RINGING:
            return
        if not self._bridge.send_frame(
            T.CALL_ANSWER, to=self._peer, call_id=self._call_id
        ):
            self._teardown("send_error")
            return
        self._set_state(CallState.ICE)
        self._start_ice()

    def reject_call(self, peer: str) -> None:
        if self._state != CallState.RINGING or peer != self._peer:
            return
        self._bridge.send_frame(
            T.CALL_REJECT, to=peer, call_id=self._call_id, reason="rejected"
        )
        self._teardown("rejected")

    def hangup(self) -> None:
        if self._state == CallState.IDLE:
            return
        if self._peer:
            self._bridge.send_frame(
                T.CALL_HANGUP, to=self._peer, call_id=self._call_id
            )
        self._teardown("hangup")

    def toggle_mute(self) -> bool:
        self._muted = not self._muted
        if self._state != CallState.IDLE and self._peer:
            self._bridge.send_frame(
                T.CALL_MUTE_STATE, to=self._peer, call_id=self._call_id,
                muted=self._muted,
            )
        return self._muted

    def force_end(self, reason: str) -> None:
        if self._state != CallState.IDLE:
            self._teardown(reason)

    # ── Incoming frame handlers (called from GUI thread via signal) ───────────

    def on_call_offer(self, peer: str, room_id: str = "", call_id: str = "") -> None:
        if self._state != CallState.IDLE:
            self._bridge.send_frame(
                T.CALL_REJECT, to=peer, call_id=call_id, reason="busy"
            )
            return
        self._peer    = peer
        self._room_id = room_id
        self._call_id = call_id or uuid.uuid4().hex
        self._reset_voice_ciphers()
        self._set_state(CallState.RINGING)
        self.incoming_call.emit(peer)

    def on_call_answer(self, call_id: str = "", peer: str = "") -> None:
        if not self._matches_call(call_id, peer) or self._state != CallState.CALLING:
            return
        self._set_state(CallState.ICE)
        self._start_ice()

    def on_call_reject(self, reason: str = "", call_id: str = "", peer: str = "") -> None:
        if not self._matches_call(call_id, peer):
            return
        self._teardown("rejected")

    def on_call_hangup(self, call_id: str = "", peer: str = "") -> None:
        if not self._matches_call(call_id, peer):
            return
        self._teardown("remote_hangup")

    def on_call_ice(self, candidate: dict, call_id: str = "", peer: str = "") -> None:
        if not self._matches_call(call_id, peer):
            return
        ip   = str(candidate.get("ip", ""))
        port = int(candidate.get("port", 0))
        if ip and port and self._udp_sock:
            self._peer_addr = (ip, port)
            threading.Thread(target=self._send_punch_probes, daemon=True).start()

    def on_media_ready(self, call_id: str = "", peer: str = "") -> None:
        if not self._matches_call(call_id, peer) or self._state != CallState.ICE:
            return
        self._remote_media_ready = True
        self._maybe_connected()

    def on_mute_state(self, muted: bool, call_id: str = "", peer: str = "") -> None:
        if self._matches_call(call_id, peer):
            self.remote_mute_changed.emit(bool(muted))

    def on_voice_chunk(self, voice_packet, call_id: str = "", peer: str = "") -> None:
        packet_call_id = str(
            voice_packet.get("call_id", "") if isinstance(voice_packet, dict) else ""
        )
        if not self._matches_call(call_id or packet_call_id, peer):
            return
        if self._state != CallState.CONNECTED or self._mode == _Mode.DIRECT:
            return
        try:
            cipher = self._voice_cipher(transmit=False)
            if cipher is None:
                return
            pcm = cipher.decrypt(voice_packet)
            arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
            self._jitter.append(arr)
        except (VoiceCryptoError, ValueError):
            pass

    # ── ICE / UDP ──────────────────────────────────────────────────────────────

    def _start_ice(self) -> None:
        self._stop_event.clear()
        try:
            self._udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._udp_sock.bind(("0.0.0.0", 0))

            from ice import get_external_address
            ext = get_external_address(self._udp_sock)
            if ext:
                ip, port = ext
                self._bridge.send_frame(
                    T.CALL_ICE, to=self._peer, call_id=self._call_id,
                    candidate={"ip": ip, "port": port},
                )

            threading.Thread(target=self._udp_recv_loop, daemon=True).start()
            self._start_audio()
        except Exception as exc:
            self.audio_error.emit(str(exc))
            self._teardown("audio_error")
            return
        self._local_media_ready = True
        if not self._bridge.send_frame(
            T.CALL_MEDIA_READY, to=self._peer, call_id=self._call_id
        ):
            self._teardown("send_error")
            return
        self._maybe_connected()

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
                self._maybe_connected()
                last_recv = time.time()
            else:
                # 加密语音包；PING/PONG 仍保持明文控制帧。
                if self._state == CallState.CONNECTED and self._mode == _Mode.DIRECT:
                    try:
                        cipher = self._voice_cipher(transmit=False)
                        if cipher is None:
                            continue
                        pcm = cipher.decrypt(data)
                        arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
                        self._jitter.append(arr)
                        last_recv = time.time()
                    except (VoiceCryptoError, ValueError):
                        pass

    def _maybe_connected(self) -> None:
        if (self._state == CallState.ICE
                and self._local_media_ready
                and self._remote_media_ready):
            self._set_state(CallState.CONNECTED)
            self._start_timer()

    # ── Audio ──────────────────────────────────────────────────────────────────

    def _start_audio(self) -> None:
        def _capture(indata, frames, time_info, status):
            if self._muted or self._state != CallState.CONNECTED:
                return
            pcm = (indata[:, 0] * 32768).astype(np.int16)
            cipher = self._voice_cipher(transmit=True)
            if cipher is None:
                return
            try:
                packet = cipher.encrypt(pcm.tobytes())
            except VoiceCryptoError:
                return
            if self._mode == _Mode.DIRECT and self._peer_addr and self._udp_sock:
                try:
                    self._udp_sock.sendto(encode_voice_packet(packet), self._peer_addr)
                except Exception:
                    pass
            else:
                sender = getattr(self._bridge, "send_voice_frame", None)
                if sender is not None:
                    sender(to=self._peer, call_id=self._call_id, voice=packet)
                else:
                    self._bridge.send_frame(
                        T.VOICE_CHUNK, to=self._peer,
                        call_id=self._call_id, voice=packet,
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
        if self._state == CallState.IDLE:
            return
        duration = int(time.time() - self._start_ts) if self._start_ts else 0
        old_room_id = self._room_id
        old_peer = self._peer
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
        self._local_media_ready = False
        self._remote_media_ready = False
        self._mode = _Mode.RELAY
        self._peer = ""
        self._room_id = ""
        self._call_id = ""
        self._reset_voice_ciphers()
        self._start_ts = 0.0
        self._set_state(CallState.IDLE)
        self.call_ended.emit(reason, duration, old_room_id, old_peer)

    def _set_state(self, state: CallState) -> None:
        self._state = state
        self.state_changed.emit(state.name)

    def _voice_cipher(self, *, transmit: bool) -> Optional[VoiceCipher]:
        if not self._peer:
            return None
        if self._voice_key_provider is None:
            return None
        if not self._call_id:
            self._call_id = uuid.uuid4().hex
        call_id = self._call_id
        try:
            key = self._voice_key_provider(self._peer, call_id, self._room_id)
        except Exception:
            return None
        if not isinstance(key, bytes) or len(key) != 32:
            return None
        if transmit:
            if self._tx_voice_cipher is None:
                self._tx_voice_cipher = VoiceCipher(
                    key,
                    call_id=call_id,
                    sender=self._username,
                    recipient=self._peer,
                    direction=f"{self._username}->{self._peer}",
                )
            return self._tx_voice_cipher
        if self._rx_voice_cipher is None:
            self._rx_voice_cipher = VoiceCipher(
                key,
                call_id=call_id,
                sender=self._peer,
                recipient=self._username,
                direction=f"{self._peer}->{self._username}",
            )
        return self._rx_voice_cipher

    def _reset_voice_ciphers(self) -> None:
        self._tx_voice_cipher = None
        self._rx_voice_cipher = None

    def _matches_call(self, call_id: str = "", peer: str = "") -> bool:
        """忽略迟到、串线或来自其他用户的通话信令。"""
        if self._state == CallState.IDLE:
            return False
        if call_id and call_id != self._call_id:
            return False
        if peer and peer != self._peer:
            return False
        return True
