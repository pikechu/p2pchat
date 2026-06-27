"""WebSocket client running in a QThread — thread-safe bridge to the server."""

import asyncio
import logging
import traceback

import websockets.legacy.client as _ws_legacy
import websockets.exceptions
from PyQt6.QtCore import QThread, pyqtSignal

from protocol import T, pack, unpack

_log = logging.getLogger(__name__)

_RECONNECT_DELAYS = [1, 2, 4, 8, 15, 30]  # seconds between attempts


class WSBridge(QThread):
    received     = pyqtSignal(str)
    connected    = pyqtSignal()
    disconnected = pyqtSignal(str)   # reason string
    reconnecting = pyqtSignal(int)   # attempt number (1-based)
    send_error   = pyqtSignal(str)

    def __init__(self, url: str, parent=None):
        super().__init__(parent)
        self._url   = url
        self._loop:       asyncio.AbstractEventLoop | None = None
        self._queue:      asyncio.Queue | None = None
        self._stop_event: asyncio.Event | None = None
        self._ws    = None
        self._stop  = False
        self._connected = False

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

    def is_connected(self) -> bool:
        return bool(
            self._connected
            and self._loop is not None
            and self._queue is not None
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
            except Exception:
                pass  # disconnect already emitted inside _connect()

            if self._stop:
                break
            attempt += 1

        self._queue = None
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
                self._connected = True
                self.connected.emit()
                _log.info("Connected to %s", self._url)
                recv = asyncio.create_task(self._recv_loop(ws))
                send = asyncio.create_task(self._send_loop(ws))
                done, pending = await asyncio.wait(
                    [recv, send], return_when=asyncio.FIRST_COMPLETED
                )
                for t in pending:
                    t.cancel()
                self._connected = False
                self._queue = None
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
            data = await self._queue.get()
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
