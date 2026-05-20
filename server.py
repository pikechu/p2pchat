"""
P2P Chat — Relay Server

Responsibilities:
  - Room lifecycle (create / join / leave / persist permanently until creator deletes)
  - Message routing (broadcast to room members)
  - Username uniqueness enforcement

The server never decrypts messages; E2E encryption lives entirely on clients.
Run:  python server.py [--host 0.0.0.0] [--port 8765]
"""

import asyncio
import argparse
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

MAX_FILE_BYTES = 500 * 1024 * 1024   # 500 MB per room-shared file

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
        # transfer_id → {room_id, from_user, filename, size, mime}
        # Chunks are NOT stored — relayed immediately to avoid memory blowup
        self._load_rooms()
        self._transfer_meta: Dict[str, dict] = {}

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
                await self._broadcast(room, T.USER_LEFT, username=username)
            else:
                log.info("room %s is now empty (persisted)", room_id)
        # Clean up any in-progress transfers started by this user
        stale = [t for t, m in self._transfer_meta.items() if m["from_user"] == username]
        for t in stale:
            self._transfer_meta.pop(t, None)

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
                        await self._send(ws, T.ERROR,
                                         message=f"'{name}' is already taken")
                        continue
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
                                          exclude=username, username=username)
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
                    to_user = str(payload.get("to", ""))
                    if to_user not in self._name_to_ws:
                        await self._send(ws, T.ERROR,
                                         message=f"User '{to_user}' not connected")
                        continue
                    target_ws = self._name_to_ws[to_user]
                    fwd_payload = dict(payload)
                    fwd_payload["from"] = username
                    try:
                        await target_ws.send(pack(
                            T(mtype), **fwd_payload
                        ))
                    except websockets.exceptions.ConnectionClosed:
                        await self._evict(to_user)
                        self._name_to_ws.pop(to_user, None)
                        await self._send(ws, T.ERROR,
                                         message=f"User '{to_user}' disconnected")

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
                    size = int(payload.get("size", 0))
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
                    }
                    # Announce to other room members so they can prepare a card
                    room = self._rooms.get(rid)
                    if room:
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
                    room = self._rooms.get(meta["room_id"])
                    if room:
                        # Relay chunk immediately — no storage
                        await self._broadcast(room, T.FILE_ROOM_CHUNK,
                                              exclude=username,
                                              transfer_id=tid,
                                              index=int(payload.get("index", 0)),
                                              total=int(payload.get("total", 1)),
                                              data=payload.get("data", ""))

                elif mtype == T.FILE_ROOM_DONE:
                    tid  = str(payload.get("transfer_id", ""))
                    meta = self._transfer_meta.pop(tid, None)
                    if meta is None:
                        continue
                    room = self._rooms.get(meta["room_id"])
                    if room:
                        log.info("file '%s' (%d B) shared by %s in room %s",
                                 meta["filename"], meta["size"],
                                 meta["from_user"], meta["room_id"])
                        await self._broadcast(room, T.FILE_ROOM_DONE,
                                              exclude=username,
                                              transfer_id=tid,
                                              sha256=str(payload.get("sha256", "")),
                                              filename=meta["filename"],
                                              size=meta["size"],
                                              mime=meta["mime"],
                                              from_user=meta["from_user"],
                                              room_id=meta["room_id"])

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
    async with websockets.serve(server.handle, host, port,
                                ping_interval=20, ping_timeout=60):
        await asyncio.Future()


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
