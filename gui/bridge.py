"""WebSocket client running in a QThread — thread-safe bridge to the server."""

import asyncio
import traceback

import websockets
import websockets.exceptions
from PyQt6.QtCore import QThread, pyqtSignal

from protocol import T, pack, unpack


class WSBridge(QThread):
    received   = pyqtSignal(str)           # raw JSON frame
    connected  = pyqtSignal()
    disconnected = pyqtSignal(str)         # reason string
    send_error = pyqtSignal(str)

    def __init__(self, url: str, parent=None):
        super().__init__(parent)
        self._url   = url
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue | None = None
        self._ws    = None

    # ── public API (called from GUI thread) ───────────────────────────────────

    def send_frame(self, msg_type: T, **payload):
        """Enqueue a frame to be sent to the server (thread-safe)."""
        if self._loop and self._queue:
            asyncio.run_coroutine_threadsafe(
                self._queue.put(pack(msg_type, **payload)), self._loop
            )

    def close(self):
        if self._loop and self._queue:
            asyncio.run_coroutine_threadsafe(
                self._queue.put(None), self._loop      # sentinel → shutdown
            )

    # ── QThread.run ───────────────────────────────────────────────────────────

    def run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._connect())
        except Exception as exc:
            self.disconnected.emit(str(exc))

    # ── async internals ───────────────────────────────────────────────────────

    async def _connect(self):
        try:
            async with websockets.connect(
                self._url, open_timeout=30,
                ping_interval=20, ping_timeout=60,
            ) as ws:
                self._ws    = ws
                self._queue = asyncio.Queue()
                self.connected.emit()
                recv = asyncio.create_task(self._recv_loop(ws))
                send = asyncio.create_task(self._send_loop(ws))
                done, pending = await asyncio.wait(
                    [recv, send], return_when=asyncio.FIRST_COMPLETED
                )
                for t in pending:
                    t.cancel()
                for t in done:
                    if exc := t.exception():
                        raise exc
        except websockets.exceptions.ConnectionClosedOK:
            self.disconnected.emit("closed")
        except websockets.exceptions.ConnectionClosedError as exc:
            self.disconnected.emit(f"connection error: {exc.reason}")
        except TimeoutError:
            self.disconnected.emit("timed out during handshake")
        except OSError as exc:
            self.disconnected.emit(str(exc))
        except Exception as exc:
            self.disconnected.emit(traceback.format_exc())

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
                self.send_error.emit(str(exc))
