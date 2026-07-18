"""WebSocket client running in a QThread — thread-safe bridge to the server."""

import asyncio
import logging
import traceback
from pathlib import Path

import websockets.legacy.client as _ws_legacy
import websockets.exceptions
from PyQt6.QtCore import QThread, pyqtSignal

from protocol import CLIENT_CAPABILITIES, CLIENT_VERSION, PROTOCOL_VERSION, T, pack, unpack
from identity import IdentityStore, sign_key_bundle
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

_log = logging.getLogger(__name__)

_RECONNECT_DELAYS = [1, 2, 4, 8, 15, 30]  # seconds between attempts
VOICE_MAX_QUEUE = 8


class ProtocolIncompatibleError(ConnectionError):
    """表示服务端没有完成当前客户端要求的严格协议握手。"""


class WSBridge(QThread):
    received     = pyqtSignal(str)
    connected    = pyqtSignal()
    disconnected = pyqtSignal(str)   # reason string
    reconnecting = pyqtSignal(int)   # attempt number (1-based)
    send_error   = pyqtSignal(str)

    def __init__(self, url: str, parent=None, username: str | None = None):
        super().__init__(parent)
        self._url   = url
        self._username = username
        self._loop:       asyncio.AbstractEventLoop | None = None
        self._queue:      asyncio.Queue | None = None
        self._voice_queue: asyncio.Queue | None = None
        self._stop_event: asyncio.Event | None = None
        self._ws    = None
        self._stop  = False
        self._connected = False
        self._identity = IdentityStore(Path.home() / ".beamchat" / "identity.json").load_or_create()
        self._ephemeral_private: X25519PrivateKey | None = None

    # ── public API (called from GUI thread) ───────────────────────────────────

    def send_frame(self, msg_type: T, **payload) -> bool:
        """Enqueue a frame to be sent. Returns False when not connected."""
        if self.is_connected():
            asyncio.run_coroutine_threadsafe(
                self._queue.put(pack(msg_type, **payload)), self._loop
            )
            return True
        return False

    def send_raw_frame(self, raw_json: str) -> bool:
        """Enqueue an already-serialised JSON frame (large file chunks)."""
        if self.is_connected():
            asyncio.run_coroutine_threadsafe(
                self._queue.put(raw_json), self._loop
            )
            return True
        return False

    def send_voice_frame(self, **payload) -> bool:
        """将可丢弃的实时语音帧放入独立有界队列。"""
        if not self.is_connected() or self._voice_queue is None:
            return False
        if self._voice_queue.qsize() >= VOICE_MAX_QUEUE:
            return False
        raw = pack(T.VOICE_CHUNK, **payload)

        def _enqueue() -> None:
            if self._voice_queue is None:
                return
            try:
                self._voice_queue.put_nowait(raw)
            except asyncio.QueueFull:
                pass

        self._loop.call_soon_threadsafe(_enqueue)
        return True

    def is_connected(self) -> bool:
        return bool(
            self._connected
            and self._loop is not None
            and self._queue is not None
            and self._voice_queue is not None
            and self._ws is not None
        )

    def close(self):
        self._stop = True
        self._connected = False
        # Wake up any reconnect sleep immediately
        if self._loop and self._stop_event:
            self._loop.call_soon_threadsafe(self._stop_event.set)
        # Send None sentinel to close the active WebSocket gracefully
        if self._loop and self._queue:
            asyncio.run_coroutine_threadsafe(
                self._queue.put(None), self._loop
            )

    # ── QThread.run ───────────────────────────────────────────────────────────

    def run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._run_with_reconnect())

    # ── async internals ───────────────────────────────────────────────────────

    async def _run_with_reconnect(self):
        self._stop_event = asyncio.Event()
        attempt = 0
        while not self._stop:
            if attempt > 0:
                delay = _RECONNECT_DELAYS[min(attempt - 1, len(_RECONNECT_DELAYS) - 1)]
                _log.info("Reconnecting in %ds (attempt %d)…", delay, attempt)
                self.reconnecting.emit(attempt)
                try:
                    await asyncio.wait_for(
                        asyncio.shield(self._stop_event.wait()),
                        timeout=float(delay),
                    )
                    break   # stop_event fired — explicit close
                except asyncio.TimeoutError:
                    pass
                if self._stop:
                    break

            try:
                await self._connect()
            except ProtocolIncompatibleError:
                break
            except Exception:
                pass  # disconnect already emitted inside _connect()

            if self._stop:
                break
            attempt += 1

        self._queue = None
        self._voice_queue = None
        self._ws = None
        self._connected = False
        self._stop_event = None

    async def _connect(self):
        try:
            async with _ws_legacy.connect(
                self._url, open_timeout=30,
                ping_interval=20, ping_timeout=60,
            ) as ws:
                self._ws    = ws
                self._queue = asyncio.Queue()
                self._voice_queue = asyncio.Queue(maxsize=VOICE_MAX_QUEUE)
                self._ephemeral_private = X25519PrivateKey.generate()
                ephemeral_public = self._ephemeral_private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
                await ws.send(pack(
                    T.CLIENT_HELLO,
                    client_version=CLIENT_VERSION,
                    protocol_version=PROTOCOL_VERSION,
                    capabilities=CLIENT_CAPABILITIES,
                    key_bundle=self._identity.public_bundle(
                        ephemeral_public,
                        sign_key_bundle(self._identity, ephemeral_public, PROTOCOL_VERSION),
                        PROTOCOL_VERSION,
                    ),
                ))
                raw = await ws.recv()
                hello = unpack(raw)
                if hello.get("type") != T.SERVER_HELLO:
                    reason = "服务器协议不兼容：未接受严格握手"
                    if hello.get("type") == T.ERROR:
                        reason = str(hello.get("payload", {}).get("message") or reason)
                    raise ProtocolIncompatibleError(reason)
                self.received.emit(raw)
                if self._username:
                    await ws.send(pack(T.SET_NAME, name=self._username))
                    raw = await ws.recv()
                    ready = unpack(raw)
                    if ready.get("type") != T.READY:
                        reason = "服务器协议不兼容：未完成身份确认"
                        if ready.get("type") == T.ERROR:
                            reason = str(ready.get("payload", {}).get("message") or reason)
                        raise ProtocolIncompatibleError(reason)
                self._connected = True
                if self._username:
                    self.received.emit(raw)
                self.connected.emit()
                _log.info("已完成服务器握手：%s", self._url)
                recv = asyncio.create_task(self._recv_loop(ws))
                send = asyncio.create_task(self._send_loop(ws))
                done, pending = await asyncio.wait(
                    [recv, send], return_when=asyncio.FIRST_COMPLETED
                )
                for t in pending:
                    t.cancel()
                self._connected = False
                self._queue = None
                self._voice_queue = None
                self._ws = None
                for t in done:
                    if exc := t.exception():
                        raise exc
        except websockets.exceptions.ConnectionClosedOK:
            self._connected = False
            _log.info("Connection closed normally")
            self.disconnected.emit("closed")
        except websockets.exceptions.ConnectionClosedError as exc:
            self._connected = False
            _log.error("Connection closed: code=%s reason=%r", exc.code, exc.reason)
            self.disconnected.emit(f"connection error: {exc.reason or exc.code}")
            raise
        except TimeoutError:
            self._connected = False
            _log.error("Connection timed out")
            self.disconnected.emit("timed out during handshake")
            raise
        except ProtocolIncompatibleError as exc:
            self._connected = False
            _log.error("Protocol incompatible: %s", exc)
            self.disconnected.emit(str(exc))
            raise
        except OSError as exc:
            self._connected = False
            _log.error("OS error: %s", exc)
            self.disconnected.emit(str(exc))
            raise
        except Exception as exc:
            self._connected = False
            _log.error("Unexpected error:\n%s", traceback.format_exc())
            self.disconnected.emit(str(exc))
            raise

    async def _recv_loop(self, ws):
        async for raw in ws:
            self.received.emit(raw)

    async def _send_loop(self, ws):
        while True:
            data = await self._next_outgoing()
            if data is None:
                await ws.close()
                return
            try:
                await ws.send(data)
            except Exception as exc:
                _log.error("ws.send error: %s", exc)
                raise   # propagate → task fails → _connect() sees exception
            # Yield between every send so ping/pong tasks can run
            # even when many file chunks are queued back-to-back.
            await asyncio.sleep(0)

    async def _next_outgoing(self):
        """优先发送实时语音，并确保语音与文件/控制帧不共用 FIFO。"""
        try:
            return self._voice_queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        try:
            return self._queue.get_nowait()
        except asyncio.QueueEmpty:
            pass

        control_get = asyncio.create_task(self._queue.get())
        voice_get = asyncio.create_task(self._voice_queue.get())
        done, pending = await asyncio.wait(
            [voice_get, control_get], return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if voice_get in done:
            if control_get in done:
                self._queue.put_nowait(control_get.result())
            return voice_get.result()
        return control_get.result()
