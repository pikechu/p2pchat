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
import binascii
import contextlib
import hashlib
import hmac
import json
import logging
import pathlib
import random
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

import websockets
import websockets.exceptions

from protocol import CLIENT_VERSION, PROTOCOL_VERSION, REQUIRED_CAPABILITIES, SERVER_CAPABILITIES, T, TTL_VALUES, pack, unpack
from file_transfer import CHUNK_SIZE
from voice_crypto import is_encrypted_voice_payload

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
_REQUIRED_CAPABILITIES = frozenset(REQUIRED_CAPABILITIES)


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
    locked: bool = True
    salt: str = ""
    encrypted_access_token: dict | None = None
    access_token_hash: str = ""
    created_at: float = field(default_factory=time.time)
    icon: str = ""                # optional emoji icon set by creator
    seq: int = 0
    # username → websocket
    members: Dict[str, object] = field(default_factory=dict)


# ── Server ───────────────────────────────────────────────────────────────────

MAX_FILE_BYTES = 50 * 1024 * 1024   # 50 MB per room-shared file
AEAD_TAG_BYTES = 16
TRANSFER_TTL_SECONDS = 10 * 60
DEFAULT_ROOM_MESSAGE_TTL_SECONDS = 7 * 24 * 60 * 60
DEFAULT_DM_MESSAGE_TTL_SECONDS = 7 * 24 * 60 * 60

_ROOMS_FILE = pathlib.Path.home() / ".p2pchat_rooms.json"
_MESSAGE_DB_FILE = pathlib.Path.home() / ".beamchat" / "beam_server.db"


class ChatServer:
    def __init__(
        self,
        *,
        message_db_path: str | Path | None = None,
        enable_message_persistence: bool = True,
        default_room_message_ttl_seconds: int | None = DEFAULT_ROOM_MESSAGE_TTL_SECONDS,
        default_dm_message_ttl_seconds: int | None = DEFAULT_DM_MESSAGE_TTL_SECONDS,
        min_message_ttl_seconds: int = 60,
        max_message_ttl_seconds: int = 365 * 24 * 60 * 60,
    ):
        self._rooms: Dict[str, Room] = {}
        # websocket → username  (populated after SET_NAME)
        self._ws_to_name: Dict[object, str] = {}
        # username → websocket
        self._name_to_ws: Dict[str, object] = {}
        # 仅保存已就绪在线用户的公开密钥包，连接关闭时立即删除。
        self._public_key_directory: Dict[str, dict] = {}
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
        self._message_db_path = Path(message_db_path) if message_db_path else _MESSAGE_DB_FILE
        self._enable_message_persistence = enable_message_persistence
        self._default_room_message_ttl_seconds = default_room_message_ttl_seconds
        self._default_dm_message_ttl_seconds = default_dm_message_ttl_seconds
        self._min_message_ttl_seconds = min_message_ttl_seconds
        self._max_message_ttl_seconds = max_message_ttl_seconds
        if self._enable_message_persistence:
            self._init_message_db()

    @staticmethod
    def _dm_scope_id(user_a: str, user_b: str) -> str:
        pair = "\x00".join(sorted([user_a, user_b]))
        return hashlib.sha256(pair.encode("utf-8")).hexdigest()

    def _init_message_db(self):
        self._message_db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._message_db_path) as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  scope_type TEXT NOT NULL,
                  scope_id TEXT NOT NULL,
                  sender_name TEXT NOT NULL,
                  recipient_name TEXT,
                  sender_device_id TEXT,
                  client_msg_id TEXT,
                  msg_type TEXT NOT NULL,
                  ciphertext TEXT NOT NULL,
                  crypto_meta TEXT NOT NULL,
                  created_at INTEGER NOT NULL,
                  expires_at INTEGER,
                  deleted_at INTEGER
                )
            """)
            db.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_scope_id
                ON messages(scope_type, scope_id, id)
            """)
            db.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_expires
                ON messages(expires_at)
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS room_settings (
                  room_id TEXT PRIMARY KEY,
                  message_ttl_seconds INTEGER,
                  persist_messages INTEGER NOT NULL DEFAULT 1,
                  updated_at INTEGER NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS dm_settings (
                  dm_id TEXT PRIMARY KEY,
                  message_ttl_seconds INTEGER,
                  persist_messages INTEGER NOT NULL DEFAULT 1,
                  updated_at INTEGER NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS dm_ttl_requests (
                  dm_id TEXT NOT NULL,
                  username TEXT NOT NULL,
                  ttl_seconds INTEGER NOT NULL,
                  updated_at INTEGER NOT NULL,
                  PRIMARY KEY (dm_id, username)
                )
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(messages)").fetchall()}
            if "recipient_name" not in columns:
                db.execute("ALTER TABLE messages ADD COLUMN recipient_name TEXT")

    def _clamp_ttl(self, ttl_seconds: int | None) -> int | None:
        if ttl_seconds is None:
            return None
        ttl = int(ttl_seconds)
        if ttl <= 0:
            return None
        return max(self._min_message_ttl_seconds, min(self._max_message_ttl_seconds, ttl))

    def _scope_ttl(self, scope_type: str, scope_id: str) -> int | None:
        default = (
            self._default_room_message_ttl_seconds
            if scope_type == "room"
            else self._default_dm_message_ttl_seconds
        )
        table = "room_settings" if scope_type == "room" else "dm_settings"
        key_col = "room_id" if scope_type == "room" else "dm_id"
        try:
            with sqlite3.connect(self._message_db_path) as db:
                row = db.execute(
                    f"SELECT message_ttl_seconds, persist_messages FROM {table} WHERE {key_col} = ?",
                    (scope_id,),
                ).fetchone()
        except sqlite3.Error:
            row = None
        if row is not None and not row[1]:
            return None
        if row is not None and row[0] == 0:
            return 0
        ttl = row[0] if row is not None and row[0] is not None else default
        return self._clamp_ttl(ttl)

    @staticmethod
    def _validate_message_ttl(ttl_seconds: int) -> int:
        ttl = int(ttl_seconds)
        if ttl not in set(TTL_VALUES.values()):
            raise ValueError("INVALID_TTL")
        return ttl

    def _default_ttl_value(self, scope_type: str) -> int:
        default = (
            self._default_room_message_ttl_seconds
            if scope_type == "room"
            else self._default_dm_message_ttl_seconds
        )
        if default is None:
            return 0
        return self._validate_message_ttl(int(default))

    def _effective_dm_ttl(self, dm_id: str) -> int:
        default = self._default_ttl_value("dm")
        with sqlite3.connect(self._message_db_path) as db:
            rows = db.execute(
                "SELECT ttl_seconds FROM dm_ttl_requests WHERE dm_id = ?",
                (dm_id,),
            ).fetchall()
        requested = [int(row[0]) for row in rows]
        if len(requested) >= 2 and all(ttl == 0 for ttl in requested):
            return 0
        finite = [ttl for ttl in requested if ttl > 0]
        if len(requested) < 2 and default > 0:
            finite.append(default)
        if finite:
            return min(finite)
        return default

    def _delete_messages_expired_by_ttl(
        self,
        scope_type: str,
        scope_id: str,
        ttl_seconds: int,
        *,
        now: int | float | None = None,
    ) -> int:
        if not self._enable_message_persistence or ttl_seconds <= 0:
            return 0
        now_i = int(time.time() if now is None else now)
        cutoff = now_i - int(ttl_seconds)
        with sqlite3.connect(self._message_db_path) as db:
            cur = db.execute(
                """
                UPDATE messages
                SET deleted_at = ?
                WHERE scope_type = ?
                  AND scope_id = ?
                  AND deleted_at IS NULL
                  AND created_at <= ?
                """,
                (now_i, scope_type, scope_id, cutoff),
            )
            return int(cur.rowcount or 0)

    def _set_scope_ttl(
        self,
        scope_type: str,
        scope_id: str,
        ttl_seconds: int,
        *,
        username: str | None = None,
        now: int | float | None = None,
    ) -> int:
        if scope_type not in {"room", "dm"}:
            raise ValueError("scope_type must be room or dm")
        ttl = self._validate_message_ttl(ttl_seconds)
        now_i = int(time.time() if now is None else now)
        self._init_message_db()
        with sqlite3.connect(self._message_db_path) as db:
            if scope_type == "room":
                db.execute(
                    """
                    INSERT INTO room_settings (room_id, message_ttl_seconds, persist_messages, updated_at)
                    VALUES (?, ?, 1, ?)
                    ON CONFLICT(room_id) DO UPDATE SET
                      message_ttl_seconds = excluded.message_ttl_seconds,
                      persist_messages = 1,
                      updated_at = excluded.updated_at
                    """,
                    (scope_id, ttl, now_i),
                )
                effective_ttl = ttl
            else:
                if not username:
                    raise ValueError("username is required for dm TTL")
                db.execute(
                    """
                    INSERT INTO dm_ttl_requests (dm_id, username, ttl_seconds, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(dm_id, username) DO UPDATE SET
                      ttl_seconds = excluded.ttl_seconds,
                      updated_at = excluded.updated_at
                    """,
                    (scope_id, username, ttl, now_i),
                )
                rows = db.execute(
                    "SELECT ttl_seconds FROM dm_ttl_requests WHERE dm_id = ?",
                    (scope_id,),
                ).fetchall()
                requested = [int(row[0]) for row in rows]
                default = self._default_ttl_value("dm")
                if len(requested) >= 2 and all(item == 0 for item in requested):
                    effective_ttl = 0
                else:
                    finite = [item for item in requested if item > 0]
                    if len(requested) < 2 and default > 0:
                        finite.append(default)
                    effective_ttl = min(finite) if finite else default
                db.execute(
                    """
                    INSERT INTO dm_settings (dm_id, message_ttl_seconds, persist_messages, updated_at)
                    VALUES (?, ?, 1, ?)
                    ON CONFLICT(dm_id) DO UPDATE SET
                      message_ttl_seconds = excluded.message_ttl_seconds,
                      persist_messages = 1,
                      updated_at = excluded.updated_at
                    """,
                    (scope_id, effective_ttl, now_i),
                )
        self._delete_messages_expired_by_ttl(scope_type, scope_id, effective_ttl, now=now_i)
        return int(effective_ttl)

    def _store_encrypted_message(
        self,
        *,
        scope_type: str,
        scope_id: str,
        sender_name: str,
        recipient_name: str | None = None,
        client_msg_id: str = "",
        msg_type: str = "text",
        ciphertext: str,
        crypto_meta: dict | None = None,
        sender_device_id: str | None = None,
        ttl_seconds: int | None = None,
        now: int | float | None = None,
    ) -> dict:
        if not self._enable_message_persistence:
            raise RuntimeError("message persistence is disabled")
        if scope_type not in {"room", "dm"}:
            raise ValueError("scope_type must be room or dm")
        created_at = int(time.time() if now is None else now)
        ttl = self._clamp_ttl(ttl_seconds)
        expires_at = created_at + ttl if ttl else None
        meta_json = json.dumps(crypto_meta or {}, ensure_ascii=False, sort_keys=True)
        with sqlite3.connect(self._message_db_path) as db:
            cur = db.execute(
                """
                INSERT INTO messages (
                  scope_type, scope_id, sender_name, recipient_name, sender_device_id, client_msg_id,
                  msg_type, ciphertext, crypto_meta, created_at, expires_at, deleted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    scope_type,
                    scope_id,
                    sender_name,
                    recipient_name,
                    sender_device_id,
                    client_msg_id,
                    msg_type,
                    ciphertext,
                    meta_json,
                    created_at,
                    expires_at,
                ),
            )
            message_id = int(cur.lastrowid)
        return {
            "message_id": message_id,
            "scope_type": scope_type,
            "scope_id": scope_id,
            "sender_name": sender_name,
            "recipient_name": recipient_name,
            "sender_device_id": sender_device_id,
            "client_msg_id": client_msg_id,
            "msg_type": msg_type,
            "ciphertext": ciphertext,
            "crypto_meta": crypto_meta or {},
            "created_at": created_at,
            "expires_at": expires_at,
        }

    def _maybe_persist_room_message(
        self,
        *,
        room_id: str,
        sender_name: str,
        payload: dict,
        now: int | float | None = None,
    ) -> dict | None:
        if not self._enable_message_persistence or not payload.get("encrypted"):
            return None
        text = str(payload.get("text", ""))
        if not text:
            return None
        ttl = self._scope_ttl("room", room_id)
        if ttl is None:
            return None
        return self._store_encrypted_message(
            scope_type="room",
            scope_id=room_id,
            sender_name=sender_name,
            client_msg_id=str(payload.get("client_mid", "")),
            msg_type=str(payload.get("msg_type", "text")),
            ciphertext=text,
            crypto_meta=payload.get("crypto_meta") or {"alg": "Fernet", "legacy_payload": True},
            ttl_seconds=ttl,
            now=now,
        )

    def _maybe_persist_dm_message(
        self,
        *,
        sender_name: str,
        to_user: str,
        payload: dict,
        now: int | float | None = None,
    ) -> dict | None:
        if not self._enable_message_persistence or not payload.get("encrypted"):
            return None
        text = str(payload.get("text", ""))
        if not text:
            return None
        dm_id = str(payload.get("scope_id") or self._dm_scope_id(sender_name, to_user))
        ttl = self._scope_ttl("dm", dm_id)
        if ttl is None:
            return None
        return self._store_encrypted_message(
            scope_type="dm",
            scope_id=dm_id,
            sender_name=sender_name,
            recipient_name=to_user,
            client_msg_id=str(payload.get("client_mid", "")),
            msg_type=str(payload.get("msg_type", "text")),
            ciphertext=text,
            crypto_meta=payload.get("crypto_meta") or {"alg": "unspecified"},
            ttl_seconds=ttl,
            now=now,
        )

    def _load_messages_for_sync(
        self,
        scopes: list[dict],
        *,
        limit: int = 200,
        requester: str | None = None,
        now: int | float | None = None,
    ) -> list[dict]:
        if not self._enable_message_persistence:
            return []
        now_i = int(time.time() if now is None else now)
        limit_i = max(1, min(int(limit or 200), 500))
        out: list[dict] = []
        with sqlite3.connect(self._message_db_path) as db:
            db.row_factory = sqlite3.Row
            for scope in scopes[:50]:
                scope_type = str(scope.get("scope_type", ""))
                scope_id = str(scope.get("scope_id", ""))
                after = self._safe_int(scope.get("after_message_id", 0), default=0) or 0
                if scope_type not in {"room", "dm"} or not scope_id:
                    continue
                if scope_type == "dm" and requester:
                    if after <= 0:
                        rows = db.execute(
                            """
                            SELECT * FROM (
                              SELECT * FROM messages
                              WHERE scope_type = ?
                                AND scope_id = ?
                                AND deleted_at IS NULL
                                AND (expires_at IS NULL OR expires_at > ?)
                                AND (sender_name = ? OR recipient_name = ?)
                              ORDER BY id DESC
                              LIMIT ?
                            ) ORDER BY id ASC
                            """,
                            (scope_type, scope_id, now_i, requester, requester, limit_i - len(out)),
                        ).fetchall()
                    else:
                        rows = db.execute(
                            """
                            SELECT * FROM messages
                            WHERE scope_type = ?
                              AND scope_id = ?
                              AND id > ?
                              AND deleted_at IS NULL
                              AND (expires_at IS NULL OR expires_at > ?)
                              AND (sender_name = ? OR recipient_name = ?)
                            ORDER BY id ASC
                            LIMIT ?
                            """,
                            (scope_type, scope_id, after, now_i, requester, requester, limit_i - len(out)),
                        ).fetchall()
                else:
                    if after <= 0:
                        rows = db.execute(
                            """
                            SELECT * FROM (
                              SELECT * FROM messages
                              WHERE scope_type = ?
                                AND scope_id = ?
                                AND deleted_at IS NULL
                                AND (expires_at IS NULL OR expires_at > ?)
                              ORDER BY id DESC
                              LIMIT ?
                            ) ORDER BY id ASC
                            """,
                            (scope_type, scope_id, now_i, limit_i - len(out)),
                        ).fetchall()
                    else:
                        rows = db.execute(
                            """
                            SELECT * FROM messages
                            WHERE scope_type = ?
                              AND scope_id = ?
                              AND id > ?
                              AND deleted_at IS NULL
                              AND (expires_at IS NULL OR expires_at > ?)
                            ORDER BY id ASC
                            LIMIT ?
                            """,
                            (scope_type, scope_id, after, now_i, limit_i - len(out)),
                        ).fetchall()
                for row in rows:
                    try:
                        crypto_meta = json.loads(row["crypto_meta"] or "{}")
                    except json.JSONDecodeError:
                        crypto_meta = {}
                    out.append({
                        "message_id": int(row["id"]),
                        "scope_type": row["scope_type"],
                        "scope_id": row["scope_id"],
                        "sender_name": row["sender_name"],
                        "recipient_name": row["recipient_name"],
                        "sender_device_id": row["sender_device_id"],
                        "client_msg_id": row["client_msg_id"] or "",
                        "msg_type": row["msg_type"],
                        "ciphertext": row["ciphertext"],
                        "crypto_meta": crypto_meta,
                        "created_at": int(row["created_at"]),
                        "expires_at": row["expires_at"],
                    })
                    if len(out) >= limit_i:
                        return out
        return out

    def _delete_expired_messages(self, *, now: int | float | None = None) -> int:
        if not self._enable_message_persistence:
            return 0
        now_i = int(time.time() if now is None else now)
        with sqlite3.connect(self._message_db_path) as db:
            cur = db.execute(
                "DELETE FROM messages WHERE expires_at IS NOT NULL AND expires_at <= ?",
                (now_i,),
            )
            return int(cur.rowcount or 0)

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
            error_payload = {"message": f"User '{to_user}' not connected"}
            if msg_type in {
                T.CALL_OFFER, T.CALL_ANSWER, T.CALL_REJECT, T.CALL_HANGUP,
                T.CALL_ICE, T.CALL_MEDIA_READY, T.CALL_MUTE_STATE, T.VOICE_CHUNK,
            }:
                error_payload.update(
                    code="CALL_UNREACHABLE",
                    call_id=str(payload.get("call_id", "")),
                    peer=to_user,
                    recoverable=True,
                )
            await self._send(ws, T.ERROR, **error_payload)
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
            error_payload = {"message": f"User '{to_user}' disconnected"}
            if msg_type in {
                T.CALL_OFFER, T.CALL_ANSWER, T.CALL_REJECT, T.CALL_HANGUP,
                T.CALL_ICE, T.CALL_MEDIA_READY, T.CALL_MUTE_STATE, T.VOICE_CHUNK,
            }:
                error_payload.update(
                    code="CALL_UNREACHABLE",
                    call_id=str(payload.get("call_id", "")),
                    peer=to_user,
                    recoverable=True,
                )
            await self._send(ws, T.ERROR, **error_payload)
            return False

    @staticmethod
    def _has_legacy_file_fields(payload: dict, fields: set[str]) -> bool:
        return any(field in payload for field in fields)

    @staticmethod
    def _valid_envelope(envelope) -> bool:
        try:
            ChatServer._envelope_ciphertext_size(envelope)
            return True
        except ValueError:
            return False

    @staticmethod
    def _envelope_ciphertext_size(envelope) -> int:
        if not isinstance(envelope, dict) or set(envelope) != {"version", "nonce", "ciphertext"}:
            raise ValueError("invalid envelope")
        if envelope.get("version") != 1:
            raise ValueError("invalid envelope version")
        try:
            nonce = base64.b64decode(envelope["nonce"], validate=True)
            ciphertext = base64.b64decode(envelope["ciphertext"], validate=True)
        except (binascii.Error, TypeError, ValueError) as exc:
            raise ValueError("invalid envelope encoding") from exc
        if len(nonce) != 12:
            raise ValueError("invalid nonce")
        if len(ciphertext) < AEAD_TAG_BYTES:
            raise ValueError("invalid ciphertext")
        return len(ciphertext)

    @staticmethod
    def _expected_chunk_ciphertext_size(size: int, index: int, total: int) -> int:
        if total <= 0 or index < 0 or index >= total:
            raise ValueError("invalid chunk position")
        if size == 0:
            plaintext_size = 0
        elif index < total - 1:
            plaintext_size = CHUNK_SIZE
        else:
            plaintext_size = size - CHUNK_SIZE * (total - 1)
        if plaintext_size < 0 or plaintext_size > CHUNK_SIZE:
            raise ValueError("invalid declared size")
        return plaintext_size + AEAD_TAG_BYTES

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
            if self._has_legacy_file_fields(payload, {"filename", "mime", "sha256", "data"}):
                await self._send(ws, T.FILE_ERROR,
                                 to=to_user, transfer_id=tid,
                                 code="PLAINTEXT_FORBIDDEN",
                                 message="PLAINTEXT_FORBIDDEN")
                return
            size = self._safe_int(payload.get("size", 0))
            total = self._safe_int(payload.get("total", 0))
            ciphertext_size = self._safe_int(payload.get("ciphertext_size", 0))
            if size is None or size < 0:
                await self._send(ws, T.FILE_ERROR,
                                 to=to_user, transfer_id=tid,
                                 message="非法文件大小")
                return
            if total is None or total != max(1, (size + CHUNK_SIZE - 1) // CHUNK_SIZE):
                await self._send(ws, T.FILE_ERROR,
                                 to=to_user, transfer_id=tid,
                                 message="invalid total")
                return
            expected_ciphertext_size = size + total * AEAD_TAG_BYTES
            if ciphertext_size is None or ciphertext_size != expected_ciphertext_size:
                await self._send(ws, T.FILE_ERROR,
                                 to=to_user, transfer_id=tid,
                                 message="invalid ciphertext_size")
                return
            if not self._valid_envelope(payload.get("encrypted_metadata")):
                await self._send(ws, T.FILE_ERROR,
                                 to=to_user, transfer_id=tid,
                                 message="invalid encrypted_metadata")
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
                "size": size,
                "ciphertext_size": ciphertext_size,
                "total_chunks": total,
                "next_index": 0,
                "received_ciphertext_bytes": 0,
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
            if mtype in (T.FILE_CHUNK, T.FILE_DONE):
                await self._send(ws, T.FILE_ERROR,
                                 to=to_user, transfer_id=tid,
                                 message="unknown transfer")
                return
            await self._forward_to_user(ws, username, to_user, T(mtype), **payload)
            return
        if meta["from_user"] != username or meta["to_user"] != to_user:
            await self._fail_direct_transfer(ws, tid, to_user, "transfer sender/recipient mismatch")
            return

        if mtype == T.FILE_CHUNK:
            if self._has_legacy_file_fields(payload, {"filename", "mime", "sha256", "data"}):
                await self._fail_direct_transfer(ws, tid, to_user, "PLAINTEXT_FORBIDDEN")
                return
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
            try:
                chunk_ciphertext_size = self._envelope_ciphertext_size(payload.get("encrypted_chunk"))
            except ValueError:
                await self._fail_direct_transfer(ws, tid, to_user, "invalid encrypted_chunk")
                return
            expected_size = self._expected_chunk_ciphertext_size(meta["size"], index, total)
            if chunk_ciphertext_size != expected_size:
                await self._fail_direct_transfer(ws, tid, to_user, "invalid encrypted chunk size")
                return
            next_size = meta["received_ciphertext_bytes"] + chunk_ciphertext_size
            if next_size > meta["ciphertext_size"]:
                await self._fail_direct_transfer(ws, tid, to_user, "ciphertext bytes exceed declared size")
                return
            meta["received_ciphertext_bytes"] = next_size
            meta["next_index"] = index + 1
            meta["last_seen"] = time.time()
            await self._forward_to_user(ws, username, to_user, T.FILE_CHUNK,
                                        **payload)
            return

        if mtype == T.FILE_DONE:
            if self._has_legacy_file_fields(payload, {"filename", "mime", "sha256", "data"}):
                await self._fail_direct_transfer(ws, tid, to_user, "PLAINTEXT_FORBIDDEN")
                return
            if meta["next_index"] != meta["total_chunks"] or meta["received_ciphertext_bytes"] != meta["ciphertext_size"]:
                await self._fail_direct_transfer(ws, tid, to_user, "file is incomplete")
                return
            if not self._valid_envelope(payload.get("encrypted_done")):
                await self._fail_direct_transfer(ws, tid, to_user, "invalid encrypted_done")
                return
            self._direct_transfer_meta.pop(tid, None)
            await self._forward_to_user(ws, username, to_user, T.FILE_DONE,
                                        **payload)

    async def _cleanup_transfer_meta_loop(self):
        while True:
            await asyncio.sleep(60)
            stale = self._prune_stale_transfers()
            if stale:
                log.info("pruned %d stale transfer(s)", len(stale))
            expired = self._delete_expired_messages()
            if expired:
                log.info("deleted %d expired encrypted message(s)", expired)

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
                    salt=r.get("salt", ""),
                    encrypted_access_token=r.get("encrypted_access_token") or {},
                    access_token_hash=r.get("access_token_hash", ""),
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
                 "locked": r.locked, "salt": r.salt,
                 "encrypted_access_token": r.encrypted_access_token or {},
                 "access_token_hash": r.access_token_hash,
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

    async def _rename_ready_user(self, old_name: str, new_name: str, ws) -> None:
        """同步已就绪连接改名后依赖用户名索引的内存状态。"""
        if old_name == new_name:
            return

        self._ws_to_name[ws] = new_name
        self._name_to_ws.pop(old_name, None)
        self._name_to_ws[new_name] = ws

        if old_name in self._public_key_directory:
            self._public_key_directory[new_name] = self._public_key_directory.pop(old_name)
        if old_name in self._user_avatar:
            self._user_avatar[new_name] = self._user_avatar.pop(old_name)

        room_id = self._user_room.pop(old_name, None)
        if room_id:
            self._user_room[new_name] = room_id
            room = self._rooms.get(room_id)
            if room is not None:
                was_member = old_name in room.members
                if old_name in room.members:
                    room.members.pop(old_name, None)
                    room.members[new_name] = ws
                if room.creator == old_name:
                    room.creator = new_name
                    self._save_rooms()
                if was_member:
                    await self._broadcast(room, T.USER_LEFT, exclude=new_name,
                                          username=old_name, room_id=room_id)
                    await self._broadcast(room, T.USER_JOINED, exclude=new_name,
                                          username=new_name, room_id=room_id)

        for room_senders in self._seq_to_sender.values():
            for seq, sender in list(room_senders.items()):
                if sender == old_name:
                    room_senders[seq] = new_name

        for meta in self._transfer_meta.values():
            if meta.get("from_user") == old_name:
                meta["from_user"] = new_name
            pending = meta.get("pending_receivers")
            if pending and old_name in pending:
                pending.discard(old_name)
                pending.add(new_name)
        for meta in self._direct_transfer_meta.values():
            if meta.get("from_user") == old_name:
                meta["from_user"] = new_name
            if meta.get("to_user") == old_name:
                meta["to_user"] = new_name

    @staticmethod
    def _valid_key_bundle_format(bundle) -> bool:
        """只检查公开密钥包的编码与长度，不在服务端验证身份签名。"""
        expected_lengths = {
            "identity_public": 32, "prekey_public": 32, "ephemeral_public": 32,
            "prekey_signature": 64, "ephemeral_signature": 64,
        }
        if not isinstance(bundle, dict) or set(bundle) != set(expected_lengths):
            return False
        try:
            return all(
                isinstance(bundle[field], str)
                and len(base64.b64decode(bundle[field], validate=True)) == length
                for field, length in expected_lengths.items()
            )
        except (binascii.Error, ValueError, TypeError):
            return False

    async def _handshake_error(self, ws, code: str, message: str, *, recoverable: bool = False):
        await self._send(ws, T.ERROR, code=code, message=message, recoverable=recoverable)

    # ── connection handler ───────────────────────────────────────────────────

    async def handle(self, ws):
        username: Optional[str] = None
        state = "HELLO"
        key_bundle: dict | None = None

        try:
            async for raw in ws:
                try:
                    msg = unpack(raw)
                except Exception:
                    await self._send(ws, T.ERROR, message="Malformed frame")
                    continue

                mtype   = msg.get("type", "")
                payload = msg.get("payload", {})

                if not isinstance(payload, dict):
                    await self._handshake_error(ws, "PROTOCOL_INCOMPATIBLE", "协议载荷格式无效")
                    await ws.close()
                    break
                if state == "HELLO" and mtype != T.CLIENT_HELLO:
                    await self._handshake_error(ws, "PROTOCOL_INCOMPATIBLE", "首帧必须为 CLIENT_HELLO")
                    await ws.close()
                    break

                # ── CLIENT_HELLO ────────────────────────────────────────────
                if mtype == T.CLIENT_HELLO:
                    client_protocol = self._safe_int(payload.get("protocol_version"), default=0)
                    capabilities = payload.get("capabilities")
                    if state != "HELLO" or client_protocol != PROTOCOL_VERSION or not isinstance(capabilities, list) \
                            or not _REQUIRED_CAPABILITIES.issubset(capabilities) \
                            or not self._valid_key_bundle_format(payload.get("key_bundle")):
                        await self._handshake_error(ws, "PROTOCOL_INCOMPATIBLE", "客户端协议版本、能力或密钥包不兼容")
                        await ws.close()
                        break
                    key_bundle = dict(payload["key_bundle"])
                    state = "IDENTIFYING"
                    await self._send(
                        ws,
                        T.SERVER_HELLO,
                        server_version=CLIENT_VERSION,
                        protocol_version=PROTOCOL_VERSION,
                        capabilities=SERVER_CAPABILITIES,
                    )
                    continue

                # ── SET_NAME ─────────────────────────────────────────────────
                if mtype == T.SET_NAME:
                    if state not in ("IDENTIFYING", "READY"):
                        await self._handshake_error(ws, "HANDSHAKE_NOT_READY", "请先完成 CLIENT_HELLO")
                        continue
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
                    old_username = username
                    username = name
                    if old_username:
                        await self._rename_ready_user(old_username, username, ws)
                    else:
                        self._ws_to_name[ws] = username
                        self._name_to_ws[username] = ws
                    self._public_key_directory[username] = key_bundle
                    state = "READY"
                    await self._send(ws, T.READY, name=username)
                    log.info("user '%s' connected", username)

                elif state != "READY":
                    await self._handshake_error(ws, "HANDSHAKE_NOT_READY", "连接尚未就绪", recoverable=True)
                    continue

                elif mtype == T.GET_PEER_KEY:
                    peer_name = str(payload.get("name", "")).strip()[:32]
                    peer_bundle = self._public_key_directory.get(peer_name)
                    if peer_bundle is None:
                        await self._send(
                            ws,
                            T.ERROR,
                            code="PEER_KEY_UNAVAILABLE",
                            name=peer_name,
                            message="对端不在线或无公开密钥包",
                            recoverable=True,
                        )
                        continue
                    await self._send(ws, T.PEER_KEY_BUNDLE, name=peer_name, key_bundle=peer_bundle)

                # ── CREATE_ROOM ──────────────────────────────────────────────
                elif mtype == T.CREATE_ROOM:
                    if not username:
                        log.warning("CREATE_ROOM rejected: SET_NAME not done (peer %s)", ws.remote_address)
                        await self._send(ws, T.ERROR, message="SET_NAME first")
                        continue
                    room_name = str(payload.get("name", f"{username}'s room"))[:64]
                    requested_id = str(payload.get("room_id", "")).strip().upper()
                    salt = payload.get("salt")
                    encrypted_access_token = payload.get("encrypted_access_token")
                    access_token_hash = str(payload.get("access_token_hash", ""))
                    locked = bool(payload.get("locked", True))
                    if (
                        len(requested_id) != _ID_LEN
                        or any(char not in _ID_CHARS for char in requested_id)
                        or requested_id in self._rooms
                        or not isinstance(salt, str)
                        or not isinstance(encrypted_access_token, dict)
                        or len(access_token_hash) != 64
                        or any(char not in "0123456789abcdef" for char in access_token_hash)
                    ):
                        await self._send(ws, T.ERROR, code="INVALID_ROOM_METADATA", message="房间加密元数据无效")
                        continue
                    # leave current room if any
                    if username in self._user_room:
                        await self._leave(username, ws)
                    rid = requested_id
                    room = Room(id=rid, name=room_name, creator=username,
                                locked=locked, salt=salt,
                                encrypted_access_token=encrypted_access_token,
                                access_token_hash=access_token_hash)
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
                    access_token = str(payload.get("access_token", ""))
                    supplied_hash = hashlib.sha256(access_token.encode("utf-8")).hexdigest()
                    if not access_token or not hmac.compare_digest(supplied_hash, room.access_token_hash):
                        await self._send(ws, T.ERROR, code="ROOM_ACCESS_DENIED", message="房间访问令牌无效")
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
                    await self._send(ws, T.ERROR, code="PLAINTEXT_FORBIDDEN", message="房间消息必须使用 AEAD 加密", recoverable=False)

                # ── SEND_ENCRYPTED_MSG ──────────────────────────────────────
                elif mtype == T.SEND_ENCRYPTED_MSG:
                    if not username:
                        await self._send(ws, T.ERROR, message="SET_NAME first")
                        continue
                    scope_type = str(payload.get("scope_type", ""))
                    scope_id = str(payload.get("scope_id", ""))
                    ciphertext = str(payload.get("ciphertext", ""))
                    if scope_type not in {"room", "dm"} or not scope_id or not ciphertext:
                        await self._send(ws, T.ERROR, message="Invalid encrypted message payload")
                        continue
                    recipient_name = None
                    if scope_type == "room" and self._user_room.get(username) != scope_id:
                        await self._send(ws, T.ERROR, message="Not in requested room")
                        continue
                    if scope_type == "dm":
                        recipient_name = str(payload.get("to", "")).strip()[:32]
                        if not recipient_name:
                            await self._send(ws, T.ERROR, message="DM encrypted messages require a recipient")
                            continue
                        canonical_scope_id = self._dm_scope_id(username, recipient_name)
                        if scope_id != canonical_scope_id:
                            await self._send(ws, T.ERROR, message="DM scope_id does not match sender/recipient")
                            continue
                    ttl = self._scope_ttl(scope_type, scope_id)
                    stored = None
                    if self._enable_message_persistence and ttl is not None:
                        stored = self._store_encrypted_message(
                            scope_type=scope_type,
                            scope_id=scope_id,
                            sender_name=username,
                            recipient_name=recipient_name,
                            client_msg_id=str(payload.get("client_msg_id", "")),
                            msg_type=str(payload.get("msg_type", "text")),
                            ciphertext=ciphertext,
                            crypto_meta=payload.get("crypto_meta") or {},
                            ttl_seconds=ttl,
                        )
                    event_payload = dict(stored or {
                        "scope_type": scope_type,
                        "scope_id": scope_id,
                        "sender_name": username,
                        "recipient_name": recipient_name,
                        "client_msg_id": str(payload.get("client_msg_id", "")),
                        "content_type": str(payload.get("msg_type", "text")),
                        "ciphertext": ciphertext,
                        "crypto_meta": payload.get("crypto_meta") or {},
                        "created_at": int(time.time()),
                        "expires_at": None,
                    })
                    if "msg_type" in event_payload:
                        event_payload["content_type"] = event_payload.pop("msg_type")
                    if scope_type == "room":
                        room = self._rooms.get(scope_id)
                        if room is not None:
                            await self._broadcast(
                                room,
                                T.NEW_ENCRYPTED_MSG,
                                exclude=username,
                                **event_payload,
                            )
                    elif recipient_name:
                        target_ws = self._name_to_ws.get(recipient_name)
                        if target_ws is not None:
                            try:
                                await target_ws.send(pack(T.NEW_ENCRYPTED_MSG, **event_payload))
                            except websockets.exceptions.ConnectionClosed:
                                await self._evict(recipient_name)
                                self._name_to_ws.pop(recipient_name, None)
                    await self._send(
                        ws,
                        T.SEND_ACK,
                        seq=stored["message_id"] if stored else 0,
                        client_mid=payload.get("client_msg_id", ""),
                        message_id=stored["message_id"] if stored else 0,
                        scope_type=scope_type,
                        scope_id=scope_id,
                    )

                # ── SYNC_MESSAGES ───────────────────────────────────────────
                elif mtype == T.SYNC_MESSAGES:
                    if not username:
                        await self._send(ws, T.ERROR, message="SET_NAME first")
                        continue
                    requested = payload.get("scopes", [])
                    if not isinstance(requested, list):
                        await self._send(ws, T.ERROR, message="Invalid sync scopes")
                        continue
                    allowed = []
                    for scope in requested:
                        if not isinstance(scope, dict):
                            continue
                        scope_type = str(scope.get("scope_type", ""))
                        scope_id = str(scope.get("scope_id", ""))
                        if scope_type == "room" and scope_id in self._rooms:
                            room = self._rooms[scope_id]
                            if username in room.members:
                                allowed.append(scope)
                        elif scope_type == "dm":
                            allowed.append(scope)
                    messages = self._load_messages_for_sync(
                        allowed,
                        limit=self._safe_int(payload.get("limit"), default=200) or 200,
                        requester=username,
                    )
                    await self._send(
                        ws,
                        T.SYNC_MESSAGES_RESULT,
                        messages=messages,
                        has_more=False,
                    )

                # ── SET_MESSAGE_TTL / GET_MESSAGE_TTL ─────────────────────
                elif mtype in (T.SET_MESSAGE_TTL, T.GET_MESSAGE_TTL):
                    if not username:
                        await self._send(ws, T.ERROR, message="SET_NAME first")
                        continue
                    if not self._enable_message_persistence:
                        await self._send(
                            ws,
                            T.ERROR,
                            code="PERSISTENCE_DISABLED",
                            message="PERSISTENCE_DISABLED",
                        )
                        continue
                    scope_type = str(payload.get("scope_type", ""))
                    scope_id = str(payload.get("scope_id", ""))
                    if scope_type not in {"room", "dm"} or not scope_id:
                        await self._send(ws, T.ERROR, message="Invalid TTL scope")
                        continue
                    peer_name = str(payload.get("to", "")).strip()[:32]
                    if scope_type == "room":
                        room = self._rooms.get(scope_id)
                        if room is None or username not in room.members:
                            await self._send(ws, T.ERROR, message="Not in requested room")
                            continue
                        if mtype == T.SET_MESSAGE_TTL and room.creator != username:
                            await self._send(ws, T.ERROR, code="FORBIDDEN", message="Only room creator can update TTL")
                            continue
                    else:
                        if not peer_name or self._dm_scope_id(username, peer_name) != scope_id:
                            await self._send(ws, T.ERROR, message="DM scope_id does not match sender/recipient")
                            continue
                    requested_ttl = None
                    if mtype == T.SET_MESSAGE_TTL:
                        ttl_seconds = self._safe_int(payload.get("ttl_seconds", -1))
                        if ttl_seconds is None:
                            await self._send(ws, T.ERROR, code="INVALID_TTL", message="INVALID_TTL")
                            continue
                        try:
                            requested_ttl = self._validate_message_ttl(ttl_seconds)
                            stored_ttl = self._set_scope_ttl(
                                scope_type,
                                scope_id,
                                requested_ttl,
                                username=username if scope_type == "dm" else None,
                            )
                        except ValueError:
                            await self._send(ws, T.ERROR, code="INVALID_TTL", message="INVALID_TTL")
                            continue
                        except sqlite3.Error:
                            await self._send(ws, T.ERROR, message="Failed to update TTL settings")
                            continue
                    else:
                        stored_ttl = self._scope_ttl(scope_type, scope_id)
                        if stored_ttl is None:
                            stored_ttl = 0
                    event = {
                        "scope_type": scope_type,
                        "scope_id": scope_id,
                        "ttl_seconds": stored_ttl,
                        "updated_by": username,
                    }
                    if requested_ttl is not None:
                        event["requested_ttl_seconds"] = requested_ttl
                    await self._send(ws, T.MESSAGE_TTL_UPDATED, **event)
                    if mtype == T.SET_MESSAGE_TTL and scope_type == "room":
                        room = self._rooms.get(scope_id)
                        if room is not None:
                            await self._broadcast(room, T.MESSAGE_TTL_UPDATED,
                                                  exclude=username, **event)
                    elif mtype == T.SET_MESSAGE_TTL and peer_name:
                        target_ws = self._name_to_ws.get(peer_name)
                        if target_ws is not None:
                            try:
                                await target_ws.send(pack(T.MESSAGE_TTL_UPDATED, **event))
                            except websockets.exceptions.ConnectionClosed:
                                await self._evict(peer_name)
                                self._name_to_ws.pop(peer_name, None)

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
                               T.CALL_HANGUP, T.CALL_ICE, T.CALL_MEDIA_READY,
                               T.CALL_MUTE_STATE, T.VOICE_CHUNK,
                               T.WEBRTC_OFFER, T.WEBRTC_ANSWER, T.WEBRTC_ICE,
                               T.WEBRTC_CLOSE, T.WEBRTC_ERROR):
                    if not username:
                        await self._send(ws, T.ERROR, message="SET_NAME first")
                        continue
                    to_user = str(payload.get("to", ""))
                    if mtype == T.VOICE_CHUNK and not is_encrypted_voice_payload(payload.get("voice")):
                        await self._send(
                            ws,
                            T.ERROR,
                            code="PLAINTEXT_FORBIDDEN",
                            message="语音帧必须使用 AEAD 加密",
                            recoverable=False,
                        )
                        continue
                    if mtype == T.VOICE_CHUNK:
                        voice = payload.get("voice", {})
                        if (
                            voice.get("sender") != username
                            or voice.get("recipient") != to_user
                            or voice.get("direction") != f"{username}->{to_user}"
                            or (
                                payload.get("call_id")
                                and voice.get("call_id") != payload.get("call_id")
                            )
                        ):
                            await self._send(
                                ws,
                                T.ERROR,
                                code="VOICE_CONTEXT_MISMATCH",
                                message="语音帧身份上下文不匹配",
                                recoverable=False,
                            )
                            continue
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
                    if self._has_legacy_file_fields(payload, {"filename", "mime", "sha256", "data"}):
                        await self._send(ws, T.FILE_ROOM_ERROR,
                                         transfer_id=payload.get("transfer_id", ""),
                                         code="PLAINTEXT_FORBIDDEN",
                                         message="PLAINTEXT_FORBIDDEN")
                        continue
                    size = self._safe_int(payload.get("size", 0))
                    total = self._safe_int(payload.get("total", 0))
                    ciphertext_size = self._safe_int(payload.get("ciphertext_size", 0))
                    if size is None or size < 0:
                        await self._send(ws, T.FILE_ROOM_ERROR,
                                         transfer_id=payload.get("transfer_id", ""),
                                         message="非法文件大小")
                        continue
                    if total is None or total != max(1, (size + CHUNK_SIZE - 1) // CHUNK_SIZE):
                        await self._send(ws, T.FILE_ROOM_ERROR,
                                         transfer_id=payload.get("transfer_id", ""),
                                         message="invalid total")
                        continue
                    expected_ciphertext_size = size + total * AEAD_TAG_BYTES
                    if ciphertext_size is None or ciphertext_size != expected_ciphertext_size:
                        await self._send(ws, T.FILE_ROOM_ERROR,
                                         transfer_id=payload.get("transfer_id", ""),
                                         message="invalid ciphertext_size")
                        continue
                    if not self._valid_envelope(payload.get("encrypted_metadata")):
                        await self._send(ws, T.FILE_ROOM_ERROR,
                                         transfer_id=payload.get("transfer_id", ""),
                                         message="invalid encrypted_metadata")
                        continue
                    if size > MAX_FILE_BYTES:
                        await self._send(ws, T.FILE_ROOM_ERROR,
                                         transfer_id=payload.get("transfer_id", ""),
                                         message=f"文件过大（最大 {MAX_FILE_BYTES//1024//1024} MB）")
                        continue
                    tid = str(payload.get("transfer_id", ""))
                    self._transfer_meta[tid] = {
                        "room_id":   rid,
                        "from_user": username,
                        "size":      size,
                        "ciphertext_size": ciphertext_size,
                        "total_chunks": total,
                        "next_index": 0,
                        "received_ciphertext_bytes": 0,
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
                                              transfer_id=tid,
                                              encrypted_metadata=payload.get("encrypted_metadata"),
                                              size=size, total=total,
                                              ciphertext_size=ciphertext_size,
                                              from_user=username, room_id=rid)

                elif mtype == T.FILE_ROOM_CHUNK:
                    tid  = str(payload.get("transfer_id", ""))
                    meta = self._transfer_meta.get(tid)
                    if meta is None:
                        continue
                    if meta["from_user"] != username:
                        continue
                    if self._has_legacy_file_fields(payload, {"filename", "mime", "sha256", "data"}):
                        await self._fail_room_transfer(ws, tid, "PLAINTEXT_FORBIDDEN")
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
                    try:
                        chunk_ciphertext_size = self._envelope_ciphertext_size(payload.get("encrypted_chunk"))
                    except ValueError:
                        await self._fail_room_transfer(ws, tid, "invalid encrypted_chunk")
                        continue
                    expected_size = self._expected_chunk_ciphertext_size(meta["size"], index, total)
                    if chunk_ciphertext_size != expected_size:
                        await self._fail_room_transfer(ws, tid, "invalid encrypted chunk size")
                        continue
                    next_size = meta["received_ciphertext_bytes"] + chunk_ciphertext_size
                    if next_size > meta["ciphertext_size"]:
                        await self._fail_room_transfer(ws, tid, "ciphertext bytes exceed declared size")
                        continue
                    meta["received_ciphertext_bytes"] = next_size
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
                                              encrypted_chunk=payload.get("encrypted_chunk"))
                        await self._send(ws, T.FILE_ROOM_CHUNK_ACK,
                                         transfer_id=tid, index=index)

                elif mtype == T.FILE_ROOM_DONE:
                    tid  = str(payload.get("transfer_id", ""))
                    meta = self._transfer_meta.get(tid)
                    if meta is None:
                        continue
                    if meta["from_user"] != username:
                        continue
                    if self._has_legacy_file_fields(payload, {"filename", "mime", "sha256", "data"}):
                        await self._fail_room_transfer(ws, tid, "PLAINTEXT_FORBIDDEN")
                        continue
                    if meta["next_index"] != meta["total_chunks"] or meta["received_ciphertext_bytes"] != meta["ciphertext_size"]:
                        await self._fail_room_transfer(ws, tid, "file is incomplete")
                        continue
                    if not self._valid_envelope(payload.get("encrypted_done")):
                        await self._fail_room_transfer(ws, tid, "invalid encrypted_done")
                        continue
                    meta["last_seen"] = time.time()
                    room = self._rooms.get(meta["room_id"])
                    if room:
                        meta["done_sent"] = True
                        log.info("encrypted file (%d B) shared by %s in room %s",
                                 meta["size"], meta["from_user"], meta["room_id"])
                        await self._broadcast(room, T.FILE_ROOM_DONE,
                                              exclude=username,
                                              transfer_id=tid,
                                              encrypted_done=payload.get("encrypted_done"),
                                              size=meta["size"],
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
                    await self._send(
                        ws,
                        T.ERROR,
                        code="PLAINTEXT_FORBIDDEN",
                        message="明文私聊已禁用，请使用加密私聊协议",
                        recoverable=False,
                    )

                # ── LIST_ROOMS ───────────────────────────────────────────────
                elif mtype == T.LIST_ROOMS:
                    rooms = [
                        {
                            "id":         r.id,
                            "name":       r.name,
                            "creator":    r.creator,
                            "members":    len(r.members),
                            "locked":     r.locked,
                            "salt":       r.salt,
                            "encrypted_access_token": r.encrypted_access_token or {},
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
                if self._name_to_ws.get(username) is ws:
                    self._name_to_ws.pop(username, None)
                    self._public_key_directory.pop(username, None)
            self._ws_to_name.pop(ws, None)
            log.info("user '%s' disconnected", username or "<anon>")


# ── Entry point ───────────────────────────────────────────────────────────────

async def _main(
    host: str,
    port: int,
    *,
    message_db_path: str | None = None,
    enable_message_persistence: bool = True,
    default_room_message_ttl_seconds: int | None = DEFAULT_ROOM_MESSAGE_TTL_SECONDS,
    default_dm_message_ttl_seconds: int | None = DEFAULT_DM_MESSAGE_TTL_SECONDS,
):
    server = ChatServer(
        message_db_path=message_db_path,
        enable_message_persistence=enable_message_persistence,
        default_room_message_ttl_seconds=default_room_message_ttl_seconds,
        default_dm_message_ttl_seconds=default_dm_message_ttl_seconds,
    )
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
    parser.add_argument("--message-db", default=os.environ.get("BEAM_MESSAGE_DB", ""),
                        help="SQLite path for encrypted message persistence")
    parser.add_argument("--no-message-persistence", action="store_true",
                        help="Disable server-side encrypted message persistence")
    parser.add_argument("--default-room-message-ttl", type=int,
                        default=int(os.environ.get("BEAM_DEFAULT_ROOM_MESSAGE_TTL", DEFAULT_ROOM_MESSAGE_TTL_SECONDS)),
                        help="Default room encrypted message TTL in seconds")
    parser.add_argument("--default-dm-message-ttl", type=int,
                        default=int(os.environ.get("BEAM_DEFAULT_DM_MESSAGE_TTL", DEFAULT_DM_MESSAGE_TTL_SECONDS)),
                        help="Default DM encrypted message TTL in seconds")
    args = parser.parse_args()
    try:
        asyncio.run(_main(
            args.host,
            args.port,
            message_db_path=args.message_db or None,
            enable_message_persistence=not args.no_message_persistence,
            default_room_message_ttl_seconds=args.default_room_message_ttl,
            default_dm_message_ttl_seconds=args.default_dm_message_ttl,
        ))
    except KeyboardInterrupt:
        log.info("Server stopped")
