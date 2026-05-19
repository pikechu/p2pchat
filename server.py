"""
P2P Chat — Relay Server

Responsibilities:
  - Room lifecycle (create / join / leave / auto-destroy when empty)
  - Message routing (broadcast to room members)
  - Username uniqueness enforcement

The server never decrypts messages; E2E encryption lives entirely on clients.
Run:  python server.py [--host 0.0.0.0] [--port 8765]
"""

import asyncio
import argparse
import logging
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

# Characters used for room IDs — omits 0/O and 1/I/L to reduce confusion
_ID_CHARS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_ID_LEN = 6


def _new_room_id(existing: set) -> str:
    while True:
        rid = "".join(random.choices(_ID_CHARS, k=_ID_LEN))
        if rid not in existing:
            return rid


# ── Data model ───────────────────────────────────────────────────────────────

@dataclass
class Room:
    id: str
    name: str
    creator: str
    locked: bool = False          # True when a password was set at creation
    created_at: float = field(default_factory=time.time)
    seq: int = 0
    # username → websocket
    members: Dict[str, object] = field(default_factory=dict)


# ── Server ───────────────────────────────────────────────────────────────────

class ChatServer:
    def __init__(self):
        self._rooms: Dict[str, Room] = {}
        # websocket → username  (populated after SET_NAME)
        self._ws_to_name: Dict[object, str] = {}
        # username → websocket
        self._name_to_ws: Dict[str, object] = {}
        # username → room_id
        self._user_room: Dict[str, str] = {}

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

    async def _evict(self, username: str):
        """Remove a user from their room without sending a LEAVE frame."""
        room_id = self._user_room.pop(username, None)
        if room_id and room_id in self._rooms:
            room = self._rooms[room_id]
            room.members.pop(username, None)
            if not room.members:
                del self._rooms[room_id]
                log.info("room %s dissolved (empty)", room_id)
            else:
                await self._broadcast(room, T.USER_LEFT, username=username)

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
                        await self._send(ws, T.ERROR, message="SET_NAME first")
                        continue
                    room_name = str(payload.get("name", f"{username}'s room"))[:64]
                    locked    = bool(payload.get("password", ""))
                    # leave current room if any
                    if username in self._user_room:
                        await self._leave(username, ws)
                    rid  = _new_room_id(set(self._rooms))
                    room = Room(id=rid, name=room_name, creator=username, locked=locked)
                    room.members[username] = ws
                    self._rooms[rid]       = room
                    self._user_room[username] = rid
                    await self._send(ws, T.ROOM_CREATED,
                                     room_id=rid, name=room_name, locked=locked)
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
                    if username in self._user_room:
                        await self._leave(username, ws)
                    room.members[username] = ws
                    self._user_room[username] = rid
                    await self._send(ws, T.ROOM_JOINED,
                                     room_id=rid,
                                     name=room.name,
                                     locked=room.locked,
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
                    # Relay to everyone else; sender displays locally
                    await self._broadcast(room, T.NEW_MSG,
                                          exclude=username,
                                          sender=username,
                                          text=text,
                                          encrypted=bool(payload.get("encrypted")),
                                          seq=room.seq)

                # ── LIST_ROOMS ───────────────────────────────────────────────
                elif mtype == T.LIST_ROOMS:
                    rooms = [
                        {
                            "id":      r.id,
                            "name":    r.name,
                            "creator": r.creator,
                            "members": len(r.members),
                            "locked":  r.locked,
                        }
                        for r in self._rooms.values()
                        if r.members
                    ]
                    await self._send(ws, T.ROOM_LIST, rooms=rooms)

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
