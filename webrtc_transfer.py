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
from dataclasses import dataclass
from typing import Any, Callable

from file_transfer import EncryptedFileReceiver, EncryptedFileSender, FileCryptoError, FileTransferManager, guess_mime
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
FileKeyProvider = Callable[[str, str], bytes]

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
        file_key_provider: FileKeyProvider,
        local_user: str = "",
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
        self._file_key_provider = file_key_provider
        self._local_user = str(local_user)
        self._ice_servers = ice_servers if ice_servers is not None else load_ice_servers()
        self._sessions: dict[str, WebRTCSession] = {}
        self._incoming_meta: dict[str, dict[str, Any]] = {}
        self._incoming_receivers: dict[str, EncryptedFileReceiver] = {}
        self._send_tasks: dict[str, asyncio.Task] = {}

    async def start_offer(
        self,
        peer: str,
        path: pathlib.Path,
        *,
        session_id: str | None = None,
    ) -> str:
        session_id = session_id or uuid.uuid4().hex[:12]
        if session_id in self._sessions:
            await self.close(session_id)
        log.info("WEBRTC start_offer session=%s peer=%s filename=%s size=%d ice_servers=%d",
                 session_id, peer, path.name, path.stat().st_size, len(self._ice_servers))
        pc = self._create_peer_connection()
        session = WebRTCSession(session_id, peer, pc, path=path)
        self._sessions[session_id] = session
        try:
            session.channel = pc.createDataChannel(self._data_channel_label)
            self._bind_peer_events(session_id)
            offer = await pc.createOffer()
            await pc.setLocalDescription(offer)
            if self._sessions.get(session_id) is not session:
                return session_id
            description = getattr(pc, "localDescription", None) or offer
            if self._send_signal(
                T.WEBRTC_OFFER,
                to=peer,
                session_id=session_id,
                sdp=self._description_to_payload(description),
            ) is False:
                raise RuntimeError("WebRTC 信令发送失败")
        except BaseException:
            await self.close(session_id)
            raise
        return session_id

    async def handle_offer(self, payload: dict) -> str:
        peer = str(payload["from"])
        session_id = str(payload["session_id"])
        if session_id in self._sessions:
            await self.close(session_id)
        log.info("WEBRTC handle_offer session=%s peer=%s", session_id, peer)
        pc = self._create_peer_connection()
        session = WebRTCSession(session_id, peer, pc)
        self._sessions[session_id] = session
        self._bind_peer_events(session_id)

        try:
            await pc.setRemoteDescription(self._description_from_payload(payload["sdp"]))
            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)
            if self._sessions.get(session_id) is not session:
                return session_id
            description = getattr(pc, "localDescription", None) or answer
            if self._send_signal(
                T.WEBRTC_ANSWER,
                to=peer,
                session_id=session_id,
                sdp=self._description_to_payload(description),
            ) is False:
                raise RuntimeError("WebRTC 信令发送失败")
        except BaseException:
            await self.close(session_id)
            raise
        return session_id

    async def handle_answer(self, payload: dict) -> None:
        session = self._require_session(str(payload["session_id"]))
        log.info("WEBRTC handle_answer session=%s peer=%s",
                 session.session_id, session.peer)
        try:
            await session.pc.setRemoteDescription(self._description_from_payload(payload["sdp"]))
        except Exception as exc:
            await self._fail_session(session.session_id, f"WebRTC 应答无效：{exc}")
            raise

    async def handle_ice(self, payload: dict) -> None:
        session = self._require_session(str(payload["session_id"]))
        log.debug("WEBRTC handle_ice session=%s peer=%s candidate_present=%s",
                  session.session_id, session.peer, bool(payload.get("candidate")))
        await session.pc.addIceCandidate(payload.get("candidate"))

    async def close(self, session_id: str) -> None:
        self._cleanup_incoming_receiver(session_id)
        session = self._sessions.pop(session_id, None)
        task = self._send_tasks.pop(session_id, None)
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            if task.get_loop() is asyncio.get_running_loop():
                await asyncio.gather(task, return_exceptions=True)
        if session is not None:
            log.info("WEBRTC close session=%s peer=%s", session_id, session.peer)
            await session.pc.close()

    async def close_all(self) -> None:
        """断连、退出或更换配置时回收全部会话，包括没有界面卡片的接收端。"""
        session_ids = set(self._sessions) | set(self._incoming_receivers) | set(self._incoming_meta) | set(self._send_tasks)
        results = await asyncio.gather(*(self.close(sid) for sid in session_ids), return_exceptions=True)
        for sid, result in zip(session_ids, results):
            if isinstance(result, BaseException):
                log.warning("WEBRTC cleanup failed session=%s error=%s", sid, result)

    def get_session_peer(self, session_id: str) -> str | None:
        """查询活跃会话的对端，供已建立通道后的取消操作使用。"""
        session = self._sessions.get(session_id)
        return session.peer if session is not None else None

    async def send_file(self, session_id: str) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._send_tasks[session_id] = task
        try:
            await self._send_file(session_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._fail_session(session_id, f"文件发送失败：{exc}")
            raise
        finally:
            if self._send_tasks.get(session_id) is task:
                self._send_tasks.pop(session_id, None)

    async def _send_file(self, session_id: str) -> None:
        session = self._require_session(session_id)
        if session.channel is None:
            raise RuntimeError(f"WebRTC session '{session_id}' has no data channel")
        if session.path is None:
            raise RuntimeError(f"WebRTC session '{session_id}' has no file path")

        path = session.path
        size = path.stat().st_size
        file_key = self._file_key_provider(session.peer, session_id)
        encrypted_sender = EncryptedFileSender(
            path, file_key, transfer_id=session_id, scope_type="dm",
            scope_id=f"webrtc:{session_id}", sender=self._local_user, recipient=session.peer,
        )
        try:
            offer = encrypted_sender.offer_payload()
            log.info("WEBRTC send_file_start session=%s peer=%s filename=%s size=%d",
                     session_id, session.peer, path.name, size)
            session.channel.send(json.dumps({
                "kind": "file-start",
                "transfer_id": session_id,
                **offer,
            }))
            self._emit_progress({
                "direction": "send",
                "peer": session.peer,
                "transfer_id": session_id,
                "filename": path.name,
                "size": size,
                "progress": 0,
            })
            while payload := encrypted_sender.next_payload():
                await self._wait_for_channel_buffer(session.channel)
                session.channel.send(json.dumps({
                    "kind": "file-chunk",
                    "transfer_id": session_id,
                    "index": payload["index"],
                    "total": payload["total"],
                    "encrypted_chunk": payload["encrypted_chunk"],
                }))
                encrypted_sender.acknowledge(payload["index"])
                self._emit_progress({
                    "direction": "send",
                    "peer": session.peer,
                    "transfer_id": session_id,
                    "filename": path.name,
                    "size": size,
                    "progress": int((payload["index"] + 1) / max(payload["total"], 1) * 100),
                })
            session.channel.send(json.dumps({
                "kind": "file-done",
                "transfer_id": session_id,
                **encrypted_sender.done_payload(),
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
        finally:
            encrypted_sender.close()

    def handle_data_message(self, from_user: str, message: str) -> pathlib.Path | None:
        if self._ft_manager is None:
            raise RuntimeError("downloads_dir is required to receive WebRTC files")
        payload = json.loads(message)
        kind = payload.get("kind")
        transfer_id = str(payload.get("transfer_id", ""))

        if kind == "file-start":
            self._cleanup_incoming_receiver(transfer_id)
            try:
                receiver = EncryptedFileReceiver(
                    self._ft_manager._dir,
                    self._file_key_provider(from_user, transfer_id),
                    transfer_id=transfer_id,
                    scope_type="dm",
                    scope_id=f"webrtc:{transfer_id}",
                    sender=from_user,
                    recipient=self._local_user,
                )
                metadata = receiver.begin(
                    payload.get("encrypted_metadata"),
                    int(payload.get("size", 0)),
                    int(payload.get("total", 1)),
                )
            except (FileCryptoError, OSError, TypeError, ValueError) as exc:
                log.warning("WEBRTC recv_file_start_rejected session=%s peer=%s error=%s",
                            transfer_id, from_user, exc)
                self._run_async(self._fail_session(transfer_id, "文件元数据认证失败", from_user))
                return None
            log.info("WEBRTC recv_file_start session=%s peer=%s filename=%s size=%s",
                     transfer_id, from_user, metadata["filename"], metadata["size"])
            self._incoming_receivers[transfer_id] = receiver
            self._incoming_meta[transfer_id] = {
                "from_user": from_user,
                "transfer_id": transfer_id,
                "filename": metadata["filename"],
                "size": metadata["size"],
                "mime": metadata["mime"],
            }
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
            receiver = self._incoming_receivers.get(transfer_id)
            if receiver is None:
                return None
            try:
                index = int(payload.get("index", 0))
                total = int(payload.get("total", 1))
                receiver.add_chunk(index, total, payload.get("encrypted_chunk"))
            except (FileCryptoError, OSError, TypeError, ValueError) as exc:
                log.warning("WEBRTC recv_file_chunk_rejected session=%s peer=%s index=%s error=%s",
                            transfer_id, from_user, payload.get("index"), exc)
                self._cleanup_incoming_receiver(transfer_id)
                self._run_async(self._fail_session(transfer_id, "文件分块认证失败", from_user))
                return None
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
            receiver = self._incoming_receivers.pop(transfer_id, None)
            if receiver is None:
                return None
            try:
                save_path = receiver.finish(payload.get("encrypted_done"))
            except (FileCryptoError, OSError, TypeError, ValueError) as exc:
                log.warning("WEBRTC recv_file_done_rejected session=%s peer=%s error=%s",
                            transfer_id, from_user, exc)
                self._incoming_meta.pop(transfer_id, None)
                receiver.cancel()
                self._run_async(self._fail_session(transfer_id, "文件完成帧认证失败", from_user))
                return None
            meta = self._incoming_meta.pop(transfer_id, None)
            if save_path is not None and self._on_file_received is not None and meta is not None:
                self._on_file_received(save_path, meta)
            log.info("WEBRTC recv_file_done session=%s peer=%s saved=%s",
                     transfer_id, from_user, bool(save_path))
            self._run_async(self.close(transfer_id))
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

        @pc.on("connectionstatechange")
        def _on_connectionstatechange():
            state = getattr(pc, "connectionState", "")
            if state in {"failed", "disconnected", "closed"} and session_id in self._sessions:
                return self._run_async(self._fail_session(session_id, "WebRTC 连接已断开"))

        if session.channel is not None:
            self._bind_channel_message(session_id, session.channel)

    def _bind_channel_message(self, session_id: str, channel: Any) -> None:
        session = self._require_session(session_id)
        if not hasattr(channel, "on"):
            return

        @channel.on("open")
        def _on_open():
            if session_id not in self._sessions:
                return None
            log.info("WEBRTC datachannel_open session=%s peer=%s",
                     session_id, session.peer)
            try:
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
            except Exception as exc:
                return self._run_async(self._fail_session(session_id, f"文件发送失败：{exc}"))
            if session.path is not None:
                return self._run_async(self._send_on_open(session_id))
            return None

        @channel.on("message")
        def _on_message(message):
            try:
                payload = json.loads(str(message))
                if not isinstance(payload, dict) or str(payload.get("transfer_id", "")) != session_id:
                    raise ValueError("文件传输标识不匹配")
                return self.handle_data_message(session.peer, str(message))
            except (TypeError, ValueError, OSError) as exc:
                log.warning("WEBRTC malformed data session=%s error=%s", session_id, exc)
                self._cleanup_incoming_receiver(session_id)
                return self._run_async(self._fail_session(session_id, "文件传输数据无效"))

        @channel.on("close")
        def _on_close():
            log.info("WEBRTC datachannel_close session=%s peer=%s",
                     session_id, session.peer)
            if session_id in self._sessions:
                return self._run_async(self._fail_session(session_id, "DataChannel closed"))

        @channel.on("error")
        def _on_error(error):
            log.warning("WEBRTC datachannel_error session=%s peer=%s error=%s",
                        session_id, session.peer, error)
            if session_id in self._sessions:
                return self._run_async(self._fail_session(session_id, str(error) or "DataChannel error"))

    @staticmethod
    def _run_async(coro):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        return loop.create_task(coro)

    async def _send_on_open(self, session_id: str) -> None:
        try:
            await self.send_file(session_id)
        except Exception as exc:
            # send_file 已完成资源回收与界面通知，事件回调只记录错误。
            log.warning("WEBRTC send failed session=%s error=%s", session_id, exc)

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

    async def _fail_session(self, session_id: str, message: str, peer: str | None = None) -> None:
        peer = self.get_session_peer(session_id) or peer
        try:
            await self.close(session_id)
        finally:
            if peer is not None:
                self._emit_session_closed(session_id, peer, message)

    def _cleanup_incoming_receiver(self, transfer_id: str) -> None:
        """释放 WebRTC 接收端状态并删除未完成的临时文件。"""
        receiver = self._incoming_receivers.pop(transfer_id, None)
        if receiver is not None:
            receiver.cancel()
        self._incoming_meta.pop(transfer_id, None)

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
