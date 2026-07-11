"""
P2P Chat — Relay Server

Responsibilities:
  - Room lifecycle (create / join / leave / persist permanently until creator deletes)
  - Message routing (broadcast to room members)
  - Username uniqueness enforcement

The server never decrypts messages; E2E encryption lives entirely on clients.
Run:  python server.py [--host 0.0.0.0] [--port 8765]
"""

from __future__ import annotations

import asyncio
import argparse
import base64
import contextlib
import hashlib
import json
import logging
import pathlib
import random
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

import websockets
import websockets.exceptions

from protocol import T, pack, unpack
from file_transfer import CHUNK_SIZE

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("server")

# Persist logs to a rotating file (10 MB × 3 backups)
from logging.handlers import RotatingFileHandler as _RFH
_fh = _RFH("server.log", maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s",
                                   datefmt="%Y-%m-%d %H:%M:%S"))
log.addHandler(_fh)

# Characters used for room IDs — omits 0/O and 1/I/L to reduce confusion
_ID_CHARS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_ID_LEN = 6


def _new_room_id(existing: set) -> str:
    while True:
        rid = "".join(random.choices(_ID_CHARS, k=_ID_LEN))
        if rid not in existing:
            return rid


# ── Data model ───────────────────────────────────────────────────────────────

def _hash_password(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()


@dataclass
class Room:
    id: str
    name: str
    creator: str
    locked: bool = False          # True when a password was set at creation
    password_hash: str = ""       # SHA-256 of the creation password; "" = open
    created_at: float = field(default_factory=time.time)
    icon: str = ""                # optional emoji icon set by creator
    seq: int = 0
    # username → websocket
    members: Dict[str, object] = field(default_factory=dict)


# ── Server ───────────────────────────────────────────────────────────────────

MAX_FILE_BYTES = 50 * 1024 * 1024   # 50 MB per room-shared file
TRANSFER_TTL_SECONDS = 10 * 60

_ROOMS_FILE = pathlib.Path.home() / ".p2pchat_rooms.json"


class ChatServer:
    def __init__(self):
        self._rooms: Dict[str, Room] = {}
        # websocket → username  (populated after SET_NAME)
        self._ws_to_name: Dict[object, str] = {}
        # username → websocket
        self._name_to_ws: Dict[str, object] = {}
        # username → room_id
        self._user_room: Dict[str, str] = {}
        # room_id → {seq: sender_username}  — for ack routing
        self._seq_to_sender: Dict[str, Dict[int, str]] = {}
        # username → base64-encoded PNG avatar (set via SET_AVATAR)
        self._user_avatar: Dict[str, str] = {}
        # transfer_id → {room_id, from_user, filename, size, mime}
        # Chunks are NOT stored — relayed immediately to avoid memory blowup
        self._load_rooms()
        self._transfer_meta: Dict[str, dict] = {}
        # transfer_id → {from_user, to_user, filename, size, mime, progress...}
        # Direct FILE_* payloads are also streamed through without buffering.
        self._direct_transfer_meta: Dict[str, dict] = {}

    @staticmethod
    def _safe_int(value, *, default: int | None = None) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _prune_stale_transfers(self, *, now: float | None = None,
                               max_age: float = TRANSFER_TTL_SECONDS) -> list[str]:
        now = time.time() if now is None else now
        stale = [
            tid for tid, meta in self._transfer_meta.items()
            if now - float(meta.get("last_seen", now)) > max_age
        ]
        for tid in stale:
            self._transfer_meta.pop(tid, None)
        direct_stale = [
            tid for tid, meta in self._direct_transfer_meta.items()
            if now - float(meta.get("last_seen", now)) > max_age
        ]
        for tid in direct_stale:
            self._direct_transfer_meta.pop(tid, None)
        return stale + direct_stale

    async def _fail_room_transfer(self, ws, transfer_id: str, message: str):
        meta = self._transfer_meta.pop(transfer_id, None)
        await self._send(ws, T.FILE_ROOM_ERROR,
                         transfer_id=transfer_id, message=message)
        if meta is not None:
            room = self._rooms.get(meta["room_id"])
            if room is not None:
                await self._broadcast(room, T.FILE_ROOM_ERROR,
                                      exclude=meta["from_user"],
                                      transfer_id=transfer_id,
                                      message=message)

    async def _fail_direct_transfer(self, ws, transfer_id: str, to_user: str, message: str):
        meta = self._direct_transfer_meta.pop(transfer_id, None)
        await self._send(ws, T.FILE_ERROR,
                         to=to_user, transfer_id=transfer_id, message=message)
        target = to_user
        if meta is not None:
            target = meta["to_user"] if meta["from_user"] != to_user else meta["from_user"]
        target_ws = self._name_to_ws.get(target)
        if target_ws is not None and target_ws is not ws:
            await self._send(target_ws, T.FILE_ERROR,
                             transfer_id=transfer_id, message=message)

    async def _forward_to_user(self, ws, username: str, to_user: str, msg_type: T, **payload) -> bool:
        if to_user not in self._name_to_ws:
            await self._send(ws, T.ERROR, message=f"User '{to_user}' not connected")
            return False
        target_ws = self._name_to_ws[to_user]
        fwd_payload = dict(payload)
        fwd_payload["from"] = username
        try:
            await target_ws.send(pack(msg_type, **fwd_payload))
            return True
        except websockets.exceptions.ConnectionClosed:
            await self._evict(to_user)
            self._name_to_ws.pop(to_user, None)
            await self._send(ws, T.ERROR, message=f"User '{to_user}' disconnected")
            return False

    async def _handle_direct_file(self, ws, username: str, mtype: str, payload: dict):
        to_user = str(payload.get("to", ""))
        tid = str(payload.get("transfer_id", ""))
        if not to_user:
            await self._send(ws, T.ERROR, message="Missing recipient")
            return
        if not tid:
            await self._send(ws, T.ERROR, message="Missing transfer_id")
            return

        if mtype == T.FILE_OFFER:
            size = self._safe_int(payload.get("size", 0))
            if size is None or size < 0:
                await self._send(ws, T.FILE_ERROR,
                                 to=to_user, transfer_id=tid,
                                 message="非法文件大小")
                return
            if size > MAX_FILE_BYTES:
                await self._send(ws, T.FILE_ERROR,
                                 to=to_user, transfer_id=tid,
                                 message=f"文件过大（最大 {MAX_FILE_BYTES//1024//1024} MB）")
                return
            if to_user not in self._name_to_ws:
                await self._send(ws, T.ERROR, message=f"User '{to_user}' not connected")
                return
            self._direct_transfer_meta[tid] = {
                "from_user": username,
                "to_user": to_user,
                "filename": str(payload.get("filename", "file")),
                "size": size,
                "mime": str(payload.get("mime", "")),
                "total_chunks": max(1, (size + CHUNK_SIZE - 1) // CHUNK_SIZE),
                "next_index": 0,
                "received_bytes": 0,
                "hasher": hashlib.sha256(),
                "last_seen": time.time(),
            }
            await self._forward_to_user(ws, username, to_user, T.FILE_OFFER, **payload)
            return

        meta = self._direct_transfer_meta.get(tid)

        if mtype in (T.FILE_ACCEPT, T.FILE_REJECT):
            if meta is not None and meta["to_user"] != username:
                await self._send(ws, T.FILE_ERROR,
                                 to=to_user, transfer_id=tid,
                                 message="transfer recipient mismatch")
                return
            if mtype == T.FILE_REJECT:
                self._direct_transfer_meta.pop(tid, None)
            await self._forward_to_user(ws, username, to_user, T(mtype), **payload)
            return

        if mtype == T.FILE_ERROR:
            self._direct_transfer_meta.pop(tid, None)
            await self._forward_to_user(ws, username, to_user, T.FILE_ERROR, **payload)
            return

        if meta is None:
            await self._forward_to_user(ws, username, to_user, T(mtype), **payload)
            return
        if meta["from_user"] != username or meta["to_user"] != to_user:
            await self._fail_direct_transfer(ws, tid, to_user, "transfer sender/recipient mismatch")
            return

        if mtype == T.FILE_CHUNK:
            index = self._safe_int(payload.get("index", 0))
            total = self._safe_int(payload.get("total", 1))
            if index is None or total is None or total <= 0 or index < 0 or index >= total:
                await self._fail_direct_transfer(ws, tid, to_user, "invalid index/total")
                return
            if total != meta["total_chunks"]:
                await self._fail_direct_transfer(ws, tid, to_user, "invalid total")
                return
            if index != meta["next_index"]:
                await self._fail_direct_transfer(ws, tid, to_user, "invalid index order")
                return
            data_b64 = str(payload.get("data", ""))
            try:
                chunk = base64.b64decode(data_b64, validate=True)
            except Exception:
                await self._fail_direct_transfer(ws, tid, to_user, "invalid chunk data")
                return
            is_last = index == total - 1
            if (not is_last and len(chunk) != CHUNK_SIZE) or len(chunk) > CHUNK_SIZE:
                await self._fail_direct_transfer(ws, tid, to_user, "invalid chunk size")
                return
            next_size = meta["received_bytes"] + len(chunk)
            if next_size > meta["size"]:
                await self._fail_direct_transfer(ws, tid, to_user, "received bytes exceed file size")
                return
            if is_last and next_size != meta["size"]:
                await self._fail_direct_transfer(ws, tid, to_user, "final chunk size mismatch")
                return
            meta["hasher"].update(chunk)
            meta["received_bytes"] = next_size
            meta["next_index"] = index + 1
            meta["last_seen"] = time.time()
            await self._forward_to_user(ws, username, to_user, T.FILE_CHUNK,
                                        **{**payload, "data": data_b64})
            return

        if mtype == T.FILE_DONE:
            if meta["next_index"] != meta["total_chunks"] or meta["received_bytes"] != meta["size"]:
                await self._fail_direct_transfer(ws, tid, to_user, "file is incomplete")
                return
            sha256_hex = str(payload.get("sha256", ""))
            if meta["hasher"].hexdigest() != sha256_hex:
                await self._fail_direct_transfer(ws, tid, to_user, "sha256 mismatch")
                return
            self._direct_transfer_meta.pop(tid, None)
            await self._forward_to_user(ws, username, to_user, T.FILE_DONE,
                                        **{**payload, "sha256": sha256_hex})

    async def _cleanup_transfer_meta_loop(self):
        while True:
            await asyncio.sleep(60)
            stale = self._prune_stale_transfers()
            if stale:
                log.info("pruned %d stale transfer(s)", len(stale))

    # ── persistence ──────────────────────────────────────────────────────────

    def _load_rooms(self):
        if not _ROOMS_FILE.exists():
            return
        try:
            data = json.loads(_ROOMS_FILE.read_text(encoding="utf-8"))
            for r in data.get("rooms", []):
                room = Room(
                    id=r["id"], name=r["name"], creator=r["creator"],
                    locked=r.get("locked", False),
                    password_hash=r.get("password_hash", ""),
                    created_at=r.get("created_at", time.time()),
                    icon=r.get("icon", ""),
                )
                self._rooms[room.id] = room
            log.info("loaded %d room(s) from %s", len(self._rooms), _ROOMS_FILE)
        except Exception as exc:
            log.error("failed to load rooms: %s", exc)

    def _save_rooms(self):
        try:
            data = {"rooms": [
                {"id": r.id, "name": r.name, "creator": r.creator,
                 "locked": r.locked, "password_hash": r.password_hash,
                 "created_at": r.created_at, "icon": r.icon}
                for r in self._rooms.values()
            ]}
            _ROOMS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                                   encoding="utf-8")
        except Exception as exc:
            log.error("failed to save rooms: %s", exc)

    # ── helpers ──────────────────────────────────────────────────────────────

    async def _send(self, ws, msg_type: T, **payload):
        try:
            await ws.send(pack(msg_type, **payload))
        except websockets.exceptions.ConnectionClosed:
            pass

    async def _broadcast(
        self,
        room: Room,
        msg_type: T,
        *,
        exclude: Optional[str] = None,
        **payload,
    ):
        dead = []
        for uname, ws in list(room.members.items()):
            if uname == exclude:
                continue
            try:
                await ws.send(pack(msg_type, **payload))
            except websockets.exceptions.ConnectionClosed:
                dead.append(uname)
        for u in dead:
            await self._evict(u)

    async def _broadcast_global(self, msg_type: T, **payload):
        """Broadcast to every connected user."""
        for ws in list(self._name_to_ws.values()):
            await self._send(ws, msg_type, **payload)

    async def _evict(self, username: str):
        """Remove a user from their room without sending a LEAVE frame."""
        room_id = self._user_room.pop(username, None)
        if room_id and room_id in self._rooms:
            room = self._rooms[room_id]
            room.members.pop(username, None)
            # Rooms persist even when empty — never auto-delete
            if room.members:
                await self._broadcast(room, T.USER_LEFT, username=username,
                                      room_id=room_id)
            else:
                log.info("room %s is now empty (persisted)", room_id)
        # Clean up any in-progress transfers started by this user
        stale = [t for t, m in self._transfer_meta.items() if m["from_user"] == username]
        for tid in stale:
            meta = self._transfer_meta.pop(tid, None)
            if meta is None:
                continue
            room = self._rooms.get(meta["room_id"])
            if room is not None:
                await self._broadcast(room, T.FILE_ROOM_ERROR,
                                      transfer_id=tid,
                                      message=f"User '{username}' disconnected")
        for tid, meta in list(self._transfer_meta.items()):
            pending = meta.get("pending_receivers")
            if pending and username in pending:
                pending.discard(username)
                meta["last_seen"] = time.time()
                if meta.get("done_sent") and not pending:
                    sender_ws = self._name_to_ws.get(meta["from_user"])
                    self._transfer_meta.pop(tid, None)
                    if sender_ws is not None:
                        await self._send(sender_ws, T.FILE_ROOM_DONE_ACK,
                                         transfer_id=tid)
        direct_stale = [
            (t, m) for t, m in self._direct_transfer_meta.items()
            if m["from_user"] == username or m["to_user"] == username
        ]
        for tid, meta in direct_stale:
            self._direct_transfer_meta.pop(tid, None)
            other = meta["to_user"] if meta["from_user"] == username else meta["from_user"]
            other_ws = self._name_to_ws.get(other)
            if other_ws is not None:
                await self._send(other_ws, T.FILE_ERROR,
                                 transfer_id=tid,
                                 message=f"User '{username}' disconnected")

    async def _leave(self, username: str, ws):
        """Graceful leave — also notifies remaining members."""
        await self._evict(username)
        await self._send(ws, T.ROOM_LEFT)

    # ── connection handler ───────────────────────────────────────────────────

    async def handle(self, ws):
        username: Optional[str] = None
        await self._send(ws, T.WELCOME,
                         message="Connected. Use SET_NAME to identify yourself.")

        try:
            async for raw in ws:
                try:
                    msg = unpack(raw)
                except Exception:
                    await self._send(ws, T.ERROR, message="Malformed frame")
                    continue

                mtype   = msg.get("type", "")
                payload = msg.get("payload", {})

                # ── SET_NAME ─────────────────────────────────────────────────
                if mtype == T.SET_NAME:
                    name = str(payload.get("name", "")).strip()[:32]
                    if not name:
                        await self._send(ws, T.ERROR, message="Name is empty")
                        continue
                    if name in self._name_to_ws and self._name_to_ws[name] is not ws:
                        old_ws = self._name_to_ws[name]
                        log.info("user '%s' reconnect takeover: closing old websocket", name)
                        try:
                            await old_ws.close(code=4000, reason="replaced by new connection")
                        except Exception:
                            pass
                        await self._evict(name)
                        self._ws_to_name.pop(old_ws, None)
                        self._name_to_ws.pop(name, None)
                    # release old name
                    if username and username in self._name_to_ws:
                        del self._name_to_ws[username]
                    username = name
                    self._ws_to_name[ws] = username
                    self._name_to_ws[username] = ws
                    await self._send(ws, T.SYSTEM, message=f"Name set to '{username}'")
                    log.info("user '%s' connected", username)

                # ── CREATE_ROOM ──────────────────────────────────────────────
                elif mtype == T.CREATE_ROOM:
                    if not username:
                        log.warning("CREATE_ROOM rejected: SET_NAME not done (peer %s)", ws.remote_address)
                        await self._send(ws, T.ERROR, message="SET_NAME first")
                        continue
                    room_name = str(payload.get("name", f"{username}'s room"))[:64]
                    pw        = str(payload.get("password", ""))
                    locked    = bool(pw)
                    # leave current room if any
                    if username in self._user_room:
                        await self._leave(username, ws)
                    rid  = _new_room_id(set(self._rooms))
                    room = Room(id=rid, name=room_name, creator=username,
                                locked=locked, password_hash=_hash_password(pw) if pw else "")
                    room.members[username] = ws
                    self._rooms[rid]       = room
                    self._user_room[username] = rid
                    await self._send(ws, T.ROOM_CREATED,
                                     room_id=rid, name=room_name, locked=locked,
                                     creator=username, created_at=room.created_at)
                    self._save_rooms()
                    log.info("room %s '%s' created by %s", rid, room_name, username)

                # ── JOIN_ROOM ────────────────────────────────────────────────
                elif mtype == T.JOIN_ROOM:
                    if not username:
                        await self._send(ws, T.ERROR, message="SET_NAME first")
                        continue
                    rid = str(payload.get("room_id", "")).strip().upper()
                    if rid not in self._rooms:
                        await self._send(ws, T.ERROR,
                                         message=f"Room '{rid}' does not exist")
                        continue
                    room = self._rooms[rid]
                    if room.locked:
                        pw = str(payload.get("password", ""))
                        if _hash_password(pw) != room.password_hash:
                            await self._send(ws, T.ERROR, message="Wrong password")
                            continue
                    if username in self._user_room:
                        await self._leave(username, ws)
                    room.members[username] = ws
                    self._user_room[username] = rid
                    await self._send(ws, T.ROOM_JOINED,
                                     room_id=rid,
                                     name=room.name,
                                     locked=room.locked,
                                     creator=room.creator,
                                     created_at=room.created_at,
                                     icon=room.icon,
                                     members=list(room.members))
                    await self._broadcast(room, T.USER_JOINED,
                                          exclude=username, username=username,
                                          room_id=rid)
                    # Send the joiner's avatar to existing members, and existing
                    # members' avatars to the joiner.
                    if username in self._user_avatar:
                        await self._broadcast(room, T.USER_AVATAR,
                                              exclude=username,
                                              name=username,
                                              data=self._user_avatar[username])
                    for member, member_ws in room.members.items():
                        if member != username and member in self._user_avatar:
                            await self._send(ws, T.USER_AVATAR,
                                             name=member,
                                             data=self._user_avatar[member])
                    log.info("%s joined room %s", username, rid)

                # ── LEAVE_ROOM ───────────────────────────────────────────────
                elif mtype == T.LEAVE_ROOM:
                    if username:
                        await self._leave(username, ws)

                # ── SEND_MSG ─────────────────────────────────────────────────
                elif mtype == T.SEND_MSG:
                    if not username:
                        await self._send(ws, T.ERROR, message="SET_NAME first")
                        continue
                    rid = self._user_room.get(username)
                    if not rid or rid not in self._rooms:
                        await self._send(ws, T.ERROR, message="Not in a room")
                        continue
                    room = self._rooms[rid]
                    text = str(payload.get("text", ""))
                    if not text:
                        continue
                    room.seq += 1
                    # Track seq → sender for ack routing
                    self._seq_to_sender.setdefault(rid, {})[room.seq] = username
                    reply_to  = payload.get("reply_to")
                    client_mid = payload.get("client_mid", "")
                    extra = {"reply_to": reply_to} if reply_to else {}
                    # Relay to everyone else; sender displays locally
                    await self._broadcast(room, T.NEW_MSG,
                                          exclude=username,
                                          sender=username,
                                          text=text,
                                          encrypted=bool(payload.get("encrypted")),
                                          seq=room.seq,
                                          **extra)
                    # Echo seq + client_mid back to sender for bubble tracking
                    await self._send(ws, T.SEND_ACK, seq=room.seq, client_mid=client_mid)

                # ── TYPING ───────────────────────────────────────────────────
                elif mtype == T.TYPING:
                    if username:
                        rid = self._user_room.get(username)
                        if rid and rid in self._rooms:
                            room = self._rooms[rid]
                            await self._broadcast(room, T.USER_TYPING,
                                                  exclude=username,
                                                  username=username,
                                                  room_id=rid,
                                                  typing=bool(payload.get("typing", False)))

                # ── MSG_ACK ──────────────────────────────────────────────────
                elif mtype == T.MSG_ACK:
                    if username:
                        rid = self._user_room.get(username)
                        seq = payload.get("seq")
                        status = str(payload.get("status", "delivered"))
                        if rid and seq is not None:
                            sender_name = self._seq_to_sender.get(rid, {}).get(int(seq))
                            if sender_name and sender_name != username \
                                    and sender_name in self._name_to_ws:
                                await self._send(
                                    self._name_to_ws[sender_name],
                                    T.MSG_STATUS,
                                    seq=int(seq), room_id=rid,
                                    status=status, from_user=username,
                                )

                # ── FILE_* (user-to-user routing) ────────────────────────
                elif mtype in (T.FILE_OFFER, T.FILE_ACCEPT, T.FILE_REJECT,
                               T.FILE_CHUNK, T.FILE_DONE, T.FILE_ERROR):
                    if not username:
                        await self._send(ws, T.ERROR, message="SET_NAME first")
                        continue
                    await self._handle_direct_file(ws, username, mtype, payload)

                elif mtype in (T.CALL_OFFER, T.CALL_ANSWER, T.CALL_REJECT,
                               T.CALL_HANGUP, T.CALL_ICE, T.VOICE_CHUNK,
                               T.WEBRTC_OFFER, T.WEBRTC_ANSWER, T.WEBRTC_ICE,
                               T.WEBRTC_CLOSE, T.WEBRTC_ERROR):
                    if not username:
                        await self._send(ws, T.ERROR, message="SET_NAME first")
                        continue
                    to_user = str(payload.get("to", ""))
                    await self._forward_to_user(ws, username, to_user, T(mtype), **payload)

                # ── FILE_ROOM_* (room-broadcast file sharing) ────────────
                # Chunks are relayed immediately — never buffered server-side.
                elif mtype == T.FILE_ROOM_SHARE:
                    if not username:
                        await self._send(ws, T.ERROR, message="SET_NAME first")
                        continue
                    rid = self._user_room.get(username, "")
                    if not rid:
                        await self._send(ws, T.FILE_ROOM_ERROR,
                                         transfer_id=payload.get("transfer_id", ""),
                                         message="Not in a room")
                        continue
                    size = self._safe_int(payload.get("size", 0))
                    if size is None or size < 0:
                        await self._send(ws, T.FILE_ROOM_ERROR,
                                         transfer_id=payload.get("transfer_id", ""),
                                         message="非法文件大小")
                        continue
                    if size > MAX_FILE_BYTES:
                        await self._send(ws, T.FILE_ROOM_ERROR,
                                         transfer_id=payload.get("transfer_id", ""),
                                         message=f"文件过大（最大 {MAX_FILE_BYTES//1024//1024} MB）")
                        continue
                    tid = str(payload.get("transfer_id", ""))
                    fname = str(payload.get("filename", "file"))
                    mime  = str(payload.get("mime", ""))
                    self._transfer_meta[tid] = {
                        "room_id":   rid,
                        "from_user": username,
                        "filename":  fname,
                        "size":      size,
                        "mime":      mime,
                        "total_chunks": max(1, (size + CHUNK_SIZE - 1) // CHUNK_SIZE),
                        "next_index": 0,
                        "received_bytes": 0,
                        "hasher": hashlib.sha256(),
                        "pending_receivers": set(),
                        "done_sent": False,
                        "created_at": time.time(),
                        "last_seen": time.time(),
                    }
                    # Announce to other room members so they can prepare a card
                    room = self._rooms.get(rid)
                    if room:
                        self._transfer_meta[tid]["pending_receivers"] = set(room.members) - {username}
                        await self._broadcast(room, T.FILE_ROOM_SHARE,
                                              exclude=username,
                                              transfer_id=tid, filename=fname,
                                              size=size, mime=mime,
                                              from_user=username, room_id=rid)

                elif mtype == T.FILE_ROOM_CHUNK:
                    tid  = str(payload.get("transfer_id", ""))
                    meta = self._transfer_meta.get(tid)
                    if meta is None:
                        continue
                    if meta["from_user"] != username:
                        continue
                    index = self._safe_int(payload.get("index", 0))
                    total = self._safe_int(payload.get("total", 1))
                    if index is None or total is None or total <= 0 or index < 0 or index >= total:
                        await self._fail_room_transfer(ws, tid, "invalid index/total")
                        continue
                    if total != meta["total_chunks"]:
                        await self._fail_room_transfer(ws, tid, "invalid total")
                        continue
                    if index != meta["next_index"]:
                        await self._fail_room_transfer(ws, tid, "invalid index order")
                        continue
                    data_b64 = str(payload.get("data", ""))
                    try:
                        chunk = base64.b64decode(data_b64, validate=True)
                    except Exception:
                        await self._fail_room_transfer(ws, tid, "invalid chunk data")
                        continue
                    is_last = index == total - 1
                    if (not is_last and len(chunk) != CHUNK_SIZE) or len(chunk) > CHUNK_SIZE:
                        await self._fail_room_transfer(ws, tid, "invalid chunk size")
                        continue
                    next_size = meta["received_bytes"] + len(chunk)
                    if next_size > meta["size"]:
                        await self._fail_room_transfer(ws, tid, "received bytes exceed file size")
                        continue
                    if is_last and next_size != meta["size"]:
                        await self._fail_room_transfer(ws, tid, "final chunk size mismatch")
                        continue
                    meta["hasher"].update(chunk)
                    meta["received_bytes"] = next_size
                    meta["next_index"] = index + 1
                    meta["last_seen"] = time.time()
                    room = self._rooms.get(meta["room_id"])
                    if room:
                        # Relay chunk immediately — no storage
                        await self._broadcast(room, T.FILE_ROOM_CHUNK,
                                              exclude=username,
                                              transfer_id=tid,
                                              index=index,
                                              total=total,
                                              data=data_b64)
                        await self._send(ws, T.FILE_ROOM_CHUNK_ACK,
                                         transfer_id=tid, index=index)

                elif mtype == T.FILE_ROOM_DONE:
                    tid  = str(payload.get("transfer_id", ""))
                    meta = self._transfer_meta.get(tid)
                    if meta is None:
                        continue
                    if meta["from_user"] != username:
                        continue
                    if meta["next_index"] != meta["total_chunks"] or meta["received_bytes"] != meta["size"]:
                        await self._fail_room_transfer(ws, tid, "file is incomplete")
                        continue
                    sha256_hex = str(payload.get("sha256", ""))
                    if meta["hasher"].hexdigest() != sha256_hex:
                        await self._fail_room_transfer(ws, tid, "sha256 mismatch")
                        continue
                    meta["last_seen"] = time.time()
                    room = self._rooms.get(meta["room_id"])
                    if room:
                        meta["done_sent"] = True
                        log.info("file '%s' (%d B) shared by %s in room %s",
                                 meta["filename"], meta["size"],
                                 meta["from_user"], meta["room_id"])
                        await self._broadcast(room, T.FILE_ROOM_DONE,
                                              exclude=username,
                                              transfer_id=tid,
                                              sha256=sha256_hex,
                                              filename=meta["filename"],
                                              size=meta["size"],
                                              mime=meta["mime"],
                                              from_user=meta["from_user"],
                                              room_id=meta["room_id"])
                        if not meta["pending_receivers"]:
                            self._transfer_meta.pop(tid, None)
                            await self._send(ws, T.FILE_ROOM_DONE_ACK,
                                             transfer_id=tid)

                elif mtype == T.FILE_ROOM_RECEIVED:
                    tid = str(payload.get("transfer_id", ""))
                    meta = self._transfer_meta.get(tid)
                    if meta is None:
                        continue
                    if username == meta["from_user"]:
                        continue
                    if username not in meta["pending_receivers"]:
                        continue
                    meta["pending_receivers"].discard(username)
                    meta["last_seen"] = time.time()
                    if meta["done_sent"] and not meta["pending_receivers"]:
                        sender_ws = self._name_to_ws.get(meta["from_user"])
                        self._transfer_meta.pop(tid, None)
                        if sender_ws is not None:
                            await self._send(sender_ws, T.FILE_ROOM_DONE_ACK,
                                             transfer_id=tid)

                # ── LIST_USERS ──────────────────────────────────────────────
                elif mtype == T.LIST_USERS:
                    users = sorted(self._name_to_ws.keys())   # includes self
                    await self._send(ws, T.USER_LIST, users=users)

                # ── SEND_DM (user-to-user direct message) ───────────────────
                elif mtype == T.SEND_DM:
                    if not username:
                        await self._send(ws, T.ERROR, message="SET_NAME first")
                        continue
                    to_user = str(payload.get("to", ""))
                    if to_user not in self._name_to_ws:
                        await self._send(ws, T.ERROR,
                                         message=f"User '{to_user}' not online")
                        continue
                    text       = str(payload.get("text", ""))
                    client_mid = payload.get("client_mid", "")
                    target_ws  = self._name_to_ws[to_user]
                    try:
                        await target_ws.send(pack(
                            T.RECV_DM,
                            **{"from": username, "text": text, "client_mid": client_mid}
                        ))
                        await self._send(ws, T.DM_ACK,
                                         client_mid=client_mid, to=to_user)
                    except websockets.exceptions.ConnectionClosed:
                        await self._evict(to_user)
                        self._name_to_ws.pop(to_user, None)
                        await self._send(ws, T.ERROR,
                                         message=f"User '{to_user}' disconnected")

                # ── LIST_ROOMS ───────────────────────────────────────────────
                elif mtype == T.LIST_ROOMS:
                    rooms = [
                        {
                            "id":         r.id,
                            "name":       r.name,
                            "creator":    r.creator,
                            "members":    len(r.members),
                            "locked":     r.locked,
                            "created_at": r.created_at,
                            "icon":       r.icon,
                        }
                        for r in self._rooms.values()
                    ]
                    await self._send(ws, T.ROOM_LIST, rooms=rooms)

                # ── SET_AVATAR ───────────────────────────────────────────────
                elif mtype == T.SET_AVATAR:
                    if not username:
                        await self._send(ws, T.ERROR, message="SET_NAME first")
                        continue
                    data = str(payload.get("data", ""))
                    if data:
                        self._user_avatar[username] = data
                        # Broadcast to everyone in the user's current room
                        rid = self._user_room.get(username)
                        if rid and rid in self._rooms:
                            await self._broadcast(self._rooms[rid], T.USER_AVATAR,
                                                  exclude=username,
                                                  name=username, data=data)

                # ── DELETE_ROOM ──────────────────────────────────────────────
                elif mtype == T.DELETE_ROOM:
                    if not username:
                        await self._send(ws, T.ERROR, message="SET_NAME first")
                        continue
                    rid = str(payload.get("room_id", "")).strip().upper()
                    room = self._rooms.get(rid)
                    if room is None:
                        await self._send(ws, T.ERROR,
                                         message=f"Room '{rid}' does not exist")
                        continue
                    if room.creator != username:
                        await self._send(ws, T.ERROR,
                                         message="Only the creator can delete this room")
                        continue
                    # Kick all current members out
                    for uname, mws in list(room.members.items()):
                        self._user_room.pop(uname, None)
                        await self._send(mws, T.ROOM_LEFT)
                    del self._rooms[rid]
                    self._seq_to_sender.pop(rid, None)
                    self._save_rooms()
                    log.info("room %s deleted by creator %s", rid, username)
                    await self._broadcast_global(T.ROOM_DELETED, room_id=rid)

                # ── SET_ROOM_NAME ────────────────────────────────────────────
                elif mtype == T.SET_ROOM_NAME:
                    if not username:
                        await self._send(ws, T.ERROR, message="SET_NAME first")
                        continue
                    rid = str(payload.get("room_id", "")).strip().upper()
                    new_name = str(payload.get("name", "")).strip()
                    room = self._rooms.get(rid)
                    if room is None:
                        await self._send(ws, T.ERROR,
                                         message=f"Room '{rid}' does not exist")
                        continue
                    if room.creator != username:
                        await self._send(ws, T.ERROR,
                                         message="Only the creator can rename this room")
                        continue
                    if not new_name:
                        await self._send(ws, T.ERROR, message="Room name cannot be empty")
                        continue
                    room.name = new_name
                    self._save_rooms()
                    log.info("room %s renamed to '%s' by %s", rid, new_name, username)
                    await self._broadcast_global(T.ROOM_NAME_UPDATED,
                                                 room_id=rid, name=new_name)

                # ── SET_ROOM_ICON ────────────────────────────────────────────
                elif mtype == T.SET_ROOM_ICON:
                    if not username:
                        await self._send(ws, T.ERROR, message="SET_NAME first")
                        continue
                    rid = str(payload.get("room_id", "")).strip().upper()
                    icon = str(payload.get("icon", "")).strip()
                    room = self._rooms.get(rid)
                    if room is None:
                        await self._send(ws, T.ERROR,
                                         message=f"Room '{rid}' does not exist")
                        continue
                    if room.creator != username:
                        await self._send(ws, T.ERROR,
                                         message="Only the creator can change this room's icon")
                        continue
                    room.icon = icon
                    self._save_rooms()
                    log.info("room %s icon set to '%s' by %s", rid, icon, username)
                    await self._broadcast_global(T.ROOM_ICON_UPDATED,
                                                 room_id=rid, icon=icon)

                else:
                    await self._send(ws, T.ERROR, message=f"Unknown type '{mtype}'")

        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            if username:
                await self._evict(username)
                self._name_to_ws.pop(username, None)
            self._ws_to_name.pop(ws, None)
            log.info("user '%s' disconnected", username or "<anon>")


# ── Entry point ───────────────────────────────────────────────────────────────

async def _main(host: str, port: int):
    server = ChatServer()
    log.info("═" * 50)
    log.info("  P2P Chat Server  —  ws://%s:%d", host, port)
    log.info("═" * 50)
    cleanup_task = asyncio.create_task(server._cleanup_transfer_meta_loop())
    async with websockets.serve(server.handle, host, port,
                                ping_interval=20, ping_timeout=60):
        try:
            await asyncio.Future()
        finally:
            cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cleanup_task


if __name__ == "__main__":
    import os
    parser = argparse.ArgumentParser(description="P2P Chat relay server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", default=int(os.environ.get("PORT", 8765)), type=int)
    args = parser.parse_args()
    try:
        asyncio.run(_main(args.host, args.port))
    except KeyboardInterrupt:
        log.info("Server stopped")
