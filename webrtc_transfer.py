"""WebRTC DataChannel transfer signaling helpers.

This module owns the WebRTC session state and signaling messages. The real
peer connection implementation is injected or lazily imported so the GUI and
tests do not need a hard aiortc dependency at import time.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import uuid
import json
import hashlib
import base64
from dataclasses import dataclass
from typing import Any, Callable

from file_transfer import FileTransferManager, guess_mime
from ice_config import load_ice_servers
from protocol import T

log = logging.getLogger("webrtc_transfer")


SignalSender = Callable[..., Any]
PeerFactory = Callable[[], Any]
FileReceivedCallback = Callable[[pathlib.Path, dict[str, Any]], Any]
FileSentCallback = Callable[[pathlib.Path, dict[str, Any]], Any]
FileProgressCallback = Callable[[dict[str, Any]], Any]
ChannelOpenCallback = Callable[[dict[str, Any]], Any]
SessionClosedCallback = Callable[[dict[str, Any]], Any]

WEBRTC_BUFFER_HIGH_WATER = 1_000_000
WEBRTC_BUFFER_POLL_SECONDS = 0.01


@dataclass
class WebRTCSession:
    session_id: str
    peer: str
    pc: Any
    channel: Any = None
    path: pathlib.Path | None = None


class WebRTCTransfer:
    def __init__(
        self,
        send_signal: SignalSender,
        *,
        peer_factory: PeerFactory | None = None,
        data_channel_label: str = "file",
        downloads_dir: pathlib.Path | None = None,
        on_file_received: FileReceivedCallback | None = None,
        on_file_sent: FileSentCallback | None = None,
        on_file_progress: FileProgressCallback | None = None,
        on_channel_open: ChannelOpenCallback | None = None,
        on_session_closed: SessionClosedCallback | None = None,
        ice_servers: list[dict[str, Any]] | None = None,
    ):
        self._send_signal = send_signal
        self._peer_factory = peer_factory
        self._data_channel_label = data_channel_label
        self._ft_manager = FileTransferManager(downloads_dir) if downloads_dir else None
        self._on_file_received = on_file_received
        self._on_file_sent = on_file_sent
        self._on_file_progress = on_file_progress
        self._on_channel_open = on_channel_open
        self._on_session_closed = on_session_closed
        self._ice_servers = ice_servers if ice_servers is not None else load_ice_servers()
        self._sessions: dict[str, WebRTCSession] = {}
        self._incoming_meta: dict[str, dict[str, Any]] = {}

    async def start_offer(
        self,
        peer: str,
        path: pathlib.Path,
        *,
        session_id: str | None = None,
    ) -> str:
        session_id = session_id or uuid.uuid4().hex[:12]
        log.info("WEBRTC start_offer session=%s peer=%s filename=%s size=%d ice_servers=%d",
                 session_id, peer, path.name, path.stat().st_size, len(self._ice_servers))
        pc = self._create_peer_connection()
        channel = pc.createDataChannel(self._data_channel_label)
        self._sessions[session_id] = WebRTCSession(session_id, peer, pc, channel, path)
        self._bind_peer_events(session_id)

        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        description = getattr(pc, "localDescription", None) or offer
        self._send_signal(
            T.WEBRTC_OFFER,
            to=peer,
            session_id=session_id,
            sdp=self._description_to_payload(description),
            filename=path.name,
            size=path.stat().st_size,
        )
        return session_id

    async def handle_offer(self, payload: dict) -> str:
        peer = str(payload["from"])
        session_id = str(payload["session_id"])
        log.info("WEBRTC handle_offer session=%s peer=%s filename=%s size=%s",
                 session_id, peer, payload.get("filename", ""), payload.get("size", ""))
        pc = self._create_peer_connection()
        self._sessions[session_id] = WebRTCSession(session_id, peer, pc)
        self._bind_peer_events(session_id)

        await pc.setRemoteDescription(self._description_from_payload(payload["sdp"]))
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
        description = getattr(pc, "localDescription", None) or answer
        self._send_signal(
            T.WEBRTC_ANSWER,
            to=peer,
            session_id=session_id,
            sdp=self._description_to_payload(description),
        )
        return session_id

    async def handle_answer(self, payload: dict) -> None:
        session = self._require_session(str(payload["session_id"]))
        log.info("WEBRTC handle_answer session=%s peer=%s",
                 session.session_id, session.peer)
        await session.pc.setRemoteDescription(self._description_from_payload(payload["sdp"]))

    async def handle_ice(self, payload: dict) -> None:
        session = self._require_session(str(payload["session_id"]))
        log.debug("WEBRTC handle_ice session=%s peer=%s candidate_present=%s",
                  session.session_id, session.peer, bool(payload.get("candidate")))
        await session.pc.addIceCandidate(payload.get("candidate"))

    async def close(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session is not None:
            log.info("WEBRTC close session=%s peer=%s", session_id, session.peer)
            await session.pc.close()

    async def send_file(self, session_id: str) -> None:
        session = self._require_session(session_id)
        if session.channel is None:
            raise RuntimeError(f"WebRTC session '{session_id}' has no data channel")
        if session.path is None:
            raise RuntimeError(f"WebRTC session '{session_id}' has no file path")

        path = session.path
        hasher = hashlib.sha256()
        size = path.stat().st_size
        log.info("WEBRTC send_file_start session=%s peer=%s filename=%s size=%d",
                 session_id, session.peer, path.name, size)
        session.channel.send(json.dumps({
            "kind": "file-start",
            "transfer_id": session_id,
            "filename": path.name,
            "size": size,
            "mime": guess_mime(path.name),
        }))
        self._emit_progress({
            "direction": "send",
            "peer": session.peer,
            "transfer_id": session_id,
            "filename": path.name,
            "size": size,
            "progress": 0,
        })
        for index, total, data_b64 in FileTransferManager.iter_file_chunks(path):
            await self._wait_for_channel_buffer(session.channel)
            hasher.update(base64.b64decode(data_b64))
            session.channel.send(json.dumps({
                "kind": "file-chunk",
                "transfer_id": session_id,
                "index": index,
                "total": total,
                "data": data_b64,
            }))
            self._emit_progress({
                "direction": "send",
                "peer": session.peer,
                "transfer_id": session_id,
                "filename": path.name,
                "size": size,
                "progress": int((index + 1) / max(total, 1) * 100),
            })
        session.channel.send(json.dumps({
            "kind": "file-done",
            "transfer_id": session_id,
            "sha256": hasher.hexdigest(),
        }))
        log.info("WEBRTC send_file_done session=%s peer=%s filename=%s size=%d",
                 session_id, session.peer, path.name, size)
        if self._on_file_sent is not None:
            self._on_file_sent(path, {
                "to_user": session.peer,
                "transfer_id": session_id,
                "filename": path.name,
                "size": size,
                "mime": guess_mime(path.name),
            })

    def handle_data_message(self, from_user: str, message: str) -> pathlib.Path | None:
        if self._ft_manager is None:
            raise RuntimeError("downloads_dir is required to receive WebRTC files")
        payload = json.loads(message)
        kind = payload.get("kind")
        transfer_id = str(payload.get("transfer_id", ""))

        if kind == "file-start":
            log.info("WEBRTC recv_file_start session=%s peer=%s filename=%s size=%s",
                     transfer_id, from_user, payload.get("filename", ""), payload.get("size", ""))
            self._incoming_meta[transfer_id] = {
                "from_user": from_user,
                "transfer_id": transfer_id,
                "filename": str(payload.get("filename", "file")),
                "size": int(payload.get("size", 0)),
                "mime": str(payload.get("mime", "application/octet-stream")),
            }
            self._ft_manager.begin_incoming(
                transfer_id,
                from_user,
                self._incoming_meta[transfer_id]["filename"],
                self._incoming_meta[transfer_id]["size"],
                self._incoming_meta[transfer_id]["mime"],
            )
            self._emit_progress({
                "direction": "receive",
                "peer": from_user,
                "transfer_id": transfer_id,
                "filename": self._incoming_meta[transfer_id]["filename"],
                "size": self._incoming_meta[transfer_id]["size"],
                "progress": 0,
            })
            return None

        if kind == "file-chunk":
            index = int(payload.get("index", 0))
            total = int(payload.get("total", 1))
            self._ft_manager.add_chunk(
                transfer_id,
                index,
                total,
                str(payload.get("data", "")),
            )
            meta = self._incoming_meta.get(transfer_id, {})
            self._emit_progress({
                "direction": "receive",
                "peer": from_user,
                "transfer_id": transfer_id,
                "filename": str(meta.get("filename", "")),
                "size": int(meta.get("size", 0)),
                "progress": int((index + 1) / max(total, 1) * 100),
            })
            return None

        if kind == "file-done":
            save_path = self._ft_manager.finish_incoming(
                transfer_id,
                str(payload.get("sha256", "")),
            )
            meta = self._incoming_meta.pop(transfer_id, None)
            if save_path is not None and self._on_file_received is not None and meta is not None:
                self._on_file_received(save_path, meta)
            log.info("WEBRTC recv_file_done session=%s peer=%s saved=%s",
                     transfer_id, from_user, bool(save_path))
            return save_path

        return None

    def _bind_peer_events(self, session_id: str) -> None:
        session = self._require_session(session_id)
        pc = session.pc
        if not hasattr(pc, "on"):
            return

        @pc.on("icecandidate")
        def _on_icecandidate(candidate):
            if candidate is None:
                return
            log.debug("WEBRTC local_ice session=%s peer=%s", session_id, session.peer)
            self._send_signal(
                T.WEBRTC_ICE,
                to=session.peer,
                session_id=session_id,
                candidate=self._candidate_to_payload(candidate),
            )

        @pc.on("datachannel")
        def _on_datachannel(channel):
            log.info("WEBRTC datachannel_received session=%s peer=%s label=%s",
                     session_id, session.peer, getattr(channel, "label", ""))
            session.channel = channel
            self._bind_channel_message(session_id, channel)

        if session.channel is not None:
            self._bind_channel_message(session_id, session.channel)

    def _bind_channel_message(self, session_id: str, channel: Any) -> None:
        session = self._require_session(session_id)
        if not hasattr(channel, "on"):
            return

        @channel.on("open")
        def _on_open():
            log.info("WEBRTC datachannel_open session=%s peer=%s",
                     session_id, session.peer)
            if self._on_channel_open is not None:
                meta = {
                    "peer": session.peer,
                    "session_id": session_id,
                }
                if session.path is not None:
                    meta.update({
                        "filename": session.path.name,
                        "size": session.path.stat().st_size,
                    })
                self._on_channel_open(meta)
            if session.path is not None:
                return self._run_async(self.send_file(session_id))
            return None

        @channel.on("message")
        def _on_message(message):
            return self.handle_data_message(session.peer, str(message))

        @channel.on("close")
        def _on_close():
            log.info("WEBRTC datachannel_close session=%s peer=%s",
                     session_id, session.peer)
            self._emit_session_closed(session_id, session.peer, "DataChannel closed")

        @channel.on("error")
        def _on_error(error):
            log.warning("WEBRTC datachannel_error session=%s peer=%s error=%s",
                        session_id, session.peer, error)
            self._emit_session_closed(session_id, session.peer, str(error) or "DataChannel error")

    @staticmethod
    def _run_async(coro):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        return loop.create_task(coro)

    async def _wait_for_channel_buffer(self, channel: Any) -> None:
        for _ in range(200):
            buffered = getattr(channel, "bufferedAmount", 0)
            if buffered is None or int(buffered) <= WEBRTC_BUFFER_HIGH_WATER:
                return
            await asyncio.sleep(WEBRTC_BUFFER_POLL_SECONDS)

    def _emit_progress(self, meta: dict[str, Any]) -> None:
        if self._on_file_progress is not None:
            self._on_file_progress(meta)

    def _emit_session_closed(self, session_id: str, peer: str, message: str) -> None:
        if self._on_session_closed is not None:
            self._on_session_closed({
                "peer": peer,
                "session_id": session_id,
                "message": message,
            })

    @staticmethod
    def _candidate_to_payload(candidate: Any) -> Any:
        if isinstance(candidate, dict):
            return candidate
        if hasattr(candidate, "to_json"):
            return candidate.to_json()
        if hasattr(candidate, "to_sdp"):
            return {"candidate": candidate.to_sdp()}
        return candidate

    def _require_session(self, session_id: str) -> WebRTCSession:
        try:
            return self._sessions[session_id]
        except KeyError as exc:
            raise KeyError(f"unknown WebRTC session '{session_id}'") from exc

    @staticmethod
    def _description_to_payload(description: Any) -> dict[str, str]:
        return {"type": str(description.type), "sdp": str(description.sdp)}

    @staticmethod
    def _description_from_payload(payload: dict) -> Any:
        try:
            from aiortc import RTCSessionDescription
        except ImportError:
            return _FallbackDescription(str(payload["type"]), str(payload["sdp"]))
        return RTCSessionDescription(sdp=str(payload["sdp"]), type=str(payload["type"]))

    def _create_peer_connection(self) -> Any:
        if self._peer_factory is not None:
            return self._peer_factory()
        return self._default_peer_factory()

    def _default_peer_factory(self) -> Any:
        try:
            from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection
        except ImportError as exc:
            raise RuntimeError("aiortc is required for WebRTC transfers") from exc
        ice_servers = [
            RTCIceServer(
                urls=server["urls"],
                username=server.get("username"),
                credential=server.get("credential"),
            )
            for server in self._ice_servers
        ]
        return RTCPeerConnection(RTCConfiguration(iceServers=ice_servers))


@dataclass
class _FallbackDescription:
    type: str
    sdp: str
