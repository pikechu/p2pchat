"""
P2P Chat — Terminal Client

Architecture: clients only make *outbound* WebSocket connections to the relay server.
No public IP or open port is required on the client side. NAT-transparent by design.

Run:  python client.py [--server ws://HOST:8765]

Commands (in-app):
  /name <username>              — set display name (required first)
  /create [room-name] [passwd]  — create a room; passwd enables E2E encryption
  /join  <ROOM-ID> [passwd]     — join a room by 6-char ID
  /leave                        — leave current room
  /rooms                        — list active rooms
  /me                           — show own status
  /log  [N]                     — show last N lines of client.log (default 30)
  /help                         — show command list
  /quit                         — exit
  <anything else>               — send as message to current room
"""

import asyncio
import argparse
import json
import logging
import logging.handlers
import random
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional

# Windows cmd/PowerShell 默认 GBK 编码，强制切换到 UTF-8 避免崩溃
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import websockets
import websockets.exceptions
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from protocol import CLIENT_CAPABILITIES, CLIENT_VERSION, PROTOCOL_VERSION, T, TTL_VALUES, pack, unpack
from crypto import (
    create_room_access_metadata,
    decode_room_envelope,
    decrypt_room_access_token,
    decrypt_room_message,
    encode_room_envelope,
    encrypt_room_message,
    decrypt,
)
from identity import IdentityStore, TrustStore, sign_key_bundle
from secure_session import SecureSessionError, SecureSessionManager
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

# ── Logging setup ─────────────────────────────────────────────────────────────

LOG_FILE = Path(__file__).parent / "client.log"
STATE_FILE = Path.home() / ".beamchat" / "client_state.json"

_file_handler = logging.handlers.RotatingFileHandler(
    LOG_FILE,
    maxBytes=1024 * 1024,   # 1 MB per file
    backupCount=3,
    encoding="utf-8",
)
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(logging.Formatter(
    fmt="%(asctime)s.%(msecs)03d  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))

log = logging.getLogger("client")
log.setLevel(logging.DEBUG)
log.addHandler(_file_handler)
log.propagate = False   # never bubble up to root logger / terminal

# ── Rich console ──────────────────────────────────────────────────────────────

console = Console(highlight=False)


# ── Display helpers ───────────────────────────────────────────────────────────

def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _info(msg: str):
    console.print(f"[dim]{_ts()}  {msg}[/dim]")


def _sys(msg: str):
    console.print(f"[dim]{_ts()}  * {msg}[/dim]")


def _err(msg: str):
    console.print(f"[dim]{_ts()}[/dim]  [bold red]! {msg}[/bold red]")


# ── Client ────────────────────────────────────────────────────────────────────

class ChatClient:
    def __init__(self, server_url: str):
        self._url             = server_url
        self._ws              = None
        self._username: Optional[str]  = None
        self._room_id: Optional[str]   = None
        self._room_name: Optional[str] = None
        self._crypto_key: Optional[bytes] = None
        self._pending_pw      = ""
        self._room_salt = ""
        self._room_access_token = ""
        self._pending_room_metadata = None
        self._known_room_metadata: dict[str, dict] = {}
        self._running         = True
        self._offsets         = self._load_offsets()
        self._identity = IdentityStore(Path.home() / ".beamchat" / "identity.json").load_or_create()
        self._secure_sessions = SecureSessionManager(
            self._identity, TrustStore(Path.home() / ".beamchat" / "trust.json")
        )
        self._pending_dms: dict[str, list[tuple[str, str]]] = {}
        self._dm_peers: set[str] = self._load_dm_peers()
        self._server_hello = False
        self._ready = False
        self._ephemeral_private: X25519PrivateKey | None = None
        log.info("Client initialised  server=%s", server_url)

    def _load_offsets(self) -> dict[str, int]:
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            offsets = data.get("offsets", {})
            return {str(k): int(v) for k, v in offsets.items()}
        except Exception:
            return {}

    def _load_dm_peers(self) -> set[str]:
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            return {str(peer) for peer in data.get("dm_peers", []) if str(peer).strip()}
        except Exception:
            return set()

    def _save_offsets(self):
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(
                json.dumps(
                    {"offsets": self._offsets, "dm_peers": sorted(getattr(self, "_dm_peers", set()))},
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception as exc:
            log.warning("failed to save offsets: %s", exc)

    def _offset_key(self, scope_type: str, scope_id: str) -> str:
        return f"{scope_type}:{scope_id}"

    def _update_offset(self, scope_type: str, scope_id: str, message_id) -> None:
        mid = int(message_id or 0)
        if mid <= 0:
            return
        key = self._offset_key(scope_type, scope_id)
        if mid > self._offsets.get(key, 0):
            self._offsets[key] = mid
            self._save_offsets()

    async def _sync_room_messages(self):
        if not self._room_id:
            return
        await self._send(
            T.SYNC_MESSAGES,
            scopes=[{
                "scope_type": "room",
                "scope_id": self._room_id,
                "after_message_id": self._offsets.get(self._offset_key("room", self._room_id), 0),
            }],
            limit=200,
        )

    async def _sync_dm_messages(self):
        scopes = []
        for peer in sorted(self._dm_peers):
            scope_id = self._secure_sessions.dm_scope_id(self._username or "", peer)
            scopes.append({
                "scope_type": "dm",
                "scope_id": scope_id,
                "after_message_id": self._offsets.get(self._offset_key("dm", scope_id), 0),
            })
        if scopes:
            await self._send(T.SYNC_MESSAGES, scopes=scopes, limit=200)

    # ── display ──────────────────────────────────────────────────────────────

    def _show_msg(self, sender: str, text: str, encrypted: bool, ts: float):
        time_str = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
        display  = text
        if encrypted and self._crypto_key:
            plain = decrypt(self._crypto_key, text)
            if plain is None:
                display = "[bold red][wrong password — cannot decrypt][/bold red]"
                log.warning("RECV decrypt_failed  sender=%s  room=%s", sender, self._room_id)
            else:
                display = plain
        elif encrypted and not self._crypto_key:
            display = "[dim][encrypted — join with the room password to read][/dim]"
            log.debug("RECV encrypted_msg_no_key  sender=%s", sender)

        is_me      = sender == self._username
        name_style = "bold cyan" if is_me else "bold green"
        label      = "You" if is_me else sender
        console.print(f"[dim]{time_str}[/dim]  [{name_style}]{label}[/{name_style}]: {display}")

    def _show_room_aead(self, sender: str, ciphertext: str, message_id: str, ts: float):
        try:
            text = decrypt_room_message(
                self._room_id or "", self._pending_pw, decode_room_envelope(ciphertext), message_id, self._room_salt
            )
        except Exception:
            _err("房间消息认证失败")
            return
        self._show_msg(sender, text, False, ts)

    def _show_help(self):
        t = Table(title="Commands", show_header=True, header_style="bold")
        t.add_column("Command", style="cyan", no_wrap=True)
        t.add_column("Description")
        rows = [
            ("/name <username>",             "Set your display name (required first)"),
            ("/create [room-name] [passwd]", "Create a room; passwd enables E2E encryption"),
            ("/join <ROOM-ID> [passwd]",     "Join a room by its 6-char ID"),
            ("/leave",                        "Leave the current room"),
            ("/rooms",                        "List all active rooms"),
            ("/ttl [@peer] [day|week|month|year|permanent]", "查询或设置消息过期时间"),
            ("/me",                           "Show your current status"),
            ("/log [N]",                      "Show last N lines of client.log (default 30)"),
            ("/help",                         "Show this help"),
            ("/quit",                         "Exit the client"),
            ("<message>",                     "Send a message to your room"),
        ]
        for cmd, desc in rows:
            t.add_row(cmd, desc)
        console.print(t)

    def _show_log(self, n: int = 30):
        """在终端显示 client.log 的最后 n 行。"""
        if not LOG_FILE.exists():
            _info("No log file yet.")
            return
        try:
            lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
            tail  = lines[-n:] if len(lines) >= n else lines
            console.print(Panel(
                "\n".join(tail) or "(empty)",
                title=f"[bold]client.log[/bold]  (last {len(tail)} lines)  {LOG_FILE}",
                border_style="yellow",
            ))
        except Exception as exc:
            _err(f"Cannot read log file: {exc}")
            log.error("show_log failed: %s", exc, exc_info=True)

    @staticmethod
    def _ttl_label(ttl_seconds: int) -> str:
        labels = {
            TTL_VALUES["day"]: "一天",
            TTL_VALUES["week"]: "一周",
            TTL_VALUES["month"]: "一个月",
            TTL_VALUES["year"]: "一年",
            TTL_VALUES["permanent"]: "永久",
        }
        return labels.get(int(ttl_seconds), f"{int(ttl_seconds)} 秒")

    async def _send_ttl_command(self, args: list[str]):
        if not self._username:
            _err("请先设置用户名：/name <用户名>")
            return
        if not self._ready:
            _err("连接尚未就绪，请稍后再试")
            return
        peer = ""
        value_arg = ""
        if args and args[0].startswith("@"):
            peer = args[0][1:]
            if not peer:
                _err("用法：/ttl [@对端] [day|week|month|year|permanent]")
                return
            value_arg = args[1] if len(args) > 1 else ""
        elif args and args[0] not in TTL_VALUES:
            _err("用法：/ttl [@对端] [day|week|month|year|permanent]")
            return
        elif args:
            value_arg = args[0]

        if peer:
            scope_type = "dm"
            scope_id = self._secure_sessions.dm_scope_id(self._username or "", peer)
            payload = {"scope_type": scope_type, "scope_id": scope_id, "to": peer}
        else:
            if not self._room_id:
                _err("当前没有房间；单聊请使用 /ttl @对端 [档位]")
                return
            payload = {"scope_type": "room", "scope_id": self._room_id}

        if value_arg:
            await self._send(T.SET_MESSAGE_TTL, **payload, ttl_seconds=TTL_VALUES[value_arg])
        else:
            await self._send(T.GET_MESSAGE_TTL, **payload)

    # ── send ─────────────────────────────────────────────────────────────────

    async def _send(self, msg_type: T, **payload):
        if not self._ws:
            return
        try:
            await self._ws.send(pack(msg_type, **payload))
            log.debug("SEND %-15s  user=%s  room=%s", msg_type.value,
                      self._username, self._room_id)
        except websockets.exceptions.ConnectionClosed as exc:
            log.warning("SEND failed (connection closed)  type=%s  %s",
                        msg_type.value, exc)
        except Exception as exc:
            log.error("SEND error  type=%s\n%s", msg_type.value,
                      traceback.format_exc())

    # ── incoming message dispatcher ──────────────────────────────────────────

    async def _dispatch(self, raw: str):
        try:
            msg = unpack(raw)
        except Exception:
            log.error("RECV unparse failed  raw=%r\n%s", raw[:120], traceback.format_exc())
            return

        mtype   = msg.get("type", "")
        payload = msg.get("payload", {})
        ts      = msg.get("ts", datetime.now().timestamp())
        log.debug("RECV %-15s  mid=%s", mtype, msg.get("mid", "?"))

        try:
            if mtype == T.WELCOME:
                _info(payload.get("message", ""))

            elif mtype == T.SERVER_HELLO:
                self._server_hello = True
                log.info(
                    "SERVER_HELLO  version=%s  protocol=%s  capabilities=%s",
                    payload.get("server_version", ""),
                    payload.get("protocol_version", ""),
                    payload.get("capabilities", []),
                )
                if self._username:
                    await self._send(T.SET_NAME, name=self._username)

            elif mtype == T.READY:
                self._ready = True
                _sys("连接已就绪")
                await self._sync_dm_messages()

            elif mtype == T.PEER_KEY_BUNDLE:
                peer = str(payload.get("name", ""))
                try:
                    self._secure_sessions.cache_peer_bundle(peer, payload.get("key_bundle", {}))
                    for text, client_msg_id in self._pending_dms.pop(peer, []):
                        self._dm_peers.add(peer)
                        self._save_offsets()
                        await self._send(T.SEND_ENCRYPTED_MSG, **self._secure_sessions.encrypt_dm(peer, text, client_msg_id))
                except SecureSessionError:
                    _err("加密私聊密钥不可用")

            elif mtype == T.SYSTEM:
                _sys(payload.get("message", ""))

            elif mtype == T.ERROR:
                server_msg = payload.get("message", "")
                _err(server_msg)
                log.warning("SERVER_ERROR  %s", server_msg)

            elif mtype == T.ROOM_CREATED:
                self._room_id   = payload["room_id"]
                self._room_name = payload["name"]
                metadata = self._pending_room_metadata
                self._room_salt = metadata["salt"] if metadata else ""
                self._room_access_token = metadata.access_token if metadata else ""
                if metadata:
                    self._known_room_metadata[self._room_id] = dict(metadata)
                self._pending_room_metadata = None
                lock_note = " [bold yellow](AEAD encrypted)[/bold yellow]"
                console.print(Panel(
                    f"Room ID: [bold yellow]{self._room_id}[/bold yellow]\n"
                    f"Name   : {self._room_name}{lock_note}\n\n"
                    f"Share the Room ID with others so they can /join",
                    title="[bold green]Room Created[/bold green]",
                    border_style="green",
                ))
                log.info("ROOM_CREATED  room=%s  name=%s  encrypted=%s",
                         self._room_id, self._room_name, True)
                await self._sync_room_messages()

            elif mtype == T.ROOM_JOINED:
                self._room_id   = payload["room_id"]
                self._room_name = payload["name"]
                members         = payload.get("members", [])
                lock_note = " [bold yellow](E2E encrypted)[/bold yellow]" if self._crypto_key else ""
                console.print(Panel(
                    f"Room ID: [bold yellow]{self._room_id}[/bold yellow]\n"
                    f"Name   : {self._room_name}{lock_note}\n"
                    f"Online : {', '.join(members)}",
                    title="[bold green]Joined Room[/bold green]",
                    border_style="green",
                ))
                log.info("ROOM_JOINED  room=%s  members=%s", self._room_id, members)
                await self._sync_room_messages()

            elif mtype == T.ROOM_LEFT:
                _sys("You left the room")
                log.info("ROOM_LEFT  room=%s", self._room_id)
                self._room_id    = None
                self._room_name  = None
                self._crypto_key = None
                self._room_salt = ""
                self._room_access_token = ""

            elif mtype == T.USER_JOINED:
                uname = payload["username"]
                _sys(f"{uname} joined the room")
                log.info("USER_JOINED  user=%s  room=%s", uname, self._room_id)

            elif mtype == T.USER_LEFT:
                uname = payload["username"]
                _sys(f"{uname} left the room")
                log.info("USER_LEFT  user=%s  room=%s", uname, self._room_id)

            elif mtype == T.NEW_MSG:
                sender    = payload.get("sender", "?")
                encrypted = payload.get("encrypted", False)
                seq       = payload.get("seq", 0)
                reply_to  = payload.get("reply_to")
                log.debug("MSG  from=%s  encrypted=%s  room=%s", sender, encrypted, self._room_id)
                if reply_to:
                    q_name = reply_to.get("sender", "")
                    q_text = reply_to.get("text", "")[:60]
                    console.print(f"[dim]  ↩ {q_name}: {q_text}[/dim]")
                self._show_msg(sender, payload.get("text", ""), encrypted, ts)
                self._update_offset("room", self._room_id or "", payload.get("message_id", 0))
                # Ack as read (terminal = message immediately visible)
                if seq:
                    import asyncio
                    asyncio.ensure_future(self._send(T.MSG_ACK, seq=seq, status="read"))

            elif mtype == T.NEW_ENCRYPTED_MSG:
                if payload.get("scope_type") == "room":
                    rid = payload.get("scope_id", "")
                    self._show_room_aead(
                        payload.get("sender_name", "?"),
                        payload.get("ciphertext", ""),
                        payload.get("client_msg_id", ""),
                        payload.get("created_at", ts),
                    )
                    self._update_offset("room", rid, payload.get("message_id", 0))
                elif payload.get("scope_type") == "dm":
                    sender = payload.get("sender_name", "")
                    recipient = payload.get("recipient_name", "")
                    peer = recipient if sender == self._username else sender
                    try:
                        self._show_msg(peer, self._secure_sessions.decrypt_dm(payload), False, payload.get("created_at", ts))
                        self._dm_peers.add(peer)
                        self._save_offsets()
                        self._update_offset("dm", payload.get("scope_id", ""), payload.get("message_id", 0))
                    except SecureSessionError:
                        _err("加密私聊密钥不可用")

            elif mtype == T.SYNC_MESSAGES_RESULT:
                for item in payload.get("messages", []):
                    if item.get("scope_type") == "room":
                        rid = item.get("scope_id", "")
                        self._show_room_aead(item.get("sender_name", "?"), item.get("ciphertext", ""), item.get("client_msg_id", ""), item.get("created_at", ts))
                        self._update_offset("room", rid, item.get("message_id", 0))
                    elif item.get("scope_type") == "dm":
                        sender = item.get("sender_name", "")
                        recipient = item.get("recipient_name", "")
                        peer = recipient if sender == self._username else sender
                        try:
                            self._show_msg(peer, self._secure_sessions.decrypt_dm(item), False, item.get("created_at", ts))
                            self._dm_peers.add(peer)
                            self._save_offsets()
                            self._update_offset("dm", item.get("scope_id", ""), item.get("message_id", 0))
                        except SecureSessionError:
                            _err("加密私聊密钥不可用")

            elif mtype == T.MESSAGE_TTL_UPDATED:
                ttl = int(payload.get("ttl_seconds", 0))
                scope_label = "房间" if payload.get("scope_type") == "room" else "私聊"
                _sys(f"{scope_label}消息过期时间：{self._ttl_label(ttl)}")

            elif mtype == T.USER_TYPING:
                uname  = payload.get("username", "")
                typing = payload.get("typing", False)
                if typing:
                    _sys(f"{uname} is typing…")
                log.debug("USER_TYPING  user=%s  typing=%s", uname, typing)

            elif mtype == T.SEND_ACK:
                log.debug("SEND_ACK  seq=%s  mid=%s",
                          payload.get("seq"), payload.get("client_mid"))
                self._update_offset(
                    payload.get("scope_type", "room"),
                    payload.get("scope_id", self._room_id or ""),
                    payload.get("message_id", 0),
                )

            elif mtype == T.MSG_STATUS:
                seq    = payload.get("seq")
                status = payload.get("status", "")
                user   = payload.get("from_user", "")
                log.debug("MSG_STATUS  seq=%s  status=%s  from=%s", seq, status, user)

            elif mtype == T.ROOM_LIST:
                rooms = payload.get("rooms", [])
                log.debug("ROOM_LIST  count=%d", len(rooms))
                if not rooms:
                    _info("No active rooms at the moment")
                    return
                t = Table(title="Active Rooms", show_header=True, header_style="bold")
                t.add_column("Room ID", style="yellow")
                t.add_column("Name")
                t.add_column("Members", justify="right")
                t.add_column("Creator")
                t.add_column("Lock")
                for r in rooms:
                    if r.get("salt") and r.get("encrypted_access_token"):
                        self._known_room_metadata[r["id"]] = {
                            "salt": r["salt"],
                            "encrypted_access_token": r["encrypted_access_token"],
                        }
                    t.add_row(r["id"], r["name"], str(r["members"]),
                              r["creator"], "[E2E]" if r["locked"] else "")
                console.print(t)

            else:
                log.warning("RECV unknown type=%s", mtype)

        except Exception:
            log.error("DISPATCH error  type=%s\n%s", mtype, traceback.format_exc())

    # ── command parser ───────────────────────────────────────────────────────

    async def _handle_line(self, line: str):
        line = line.strip()
        if not line:
            return

        if not line.startswith("/"):
            if not self._username:
                _err("Set your name first: /name <username>")
                return
            if not self._room_id:
                _err("Join a room first: /join <ROOM-ID>  or  /create")
                return
            if not self._ready:
                _err("连接尚未就绪，请稍后再试")
                return
            if not self._room_salt:
                _err("房间加密密钥不可用")
                return
            client_msg_id = f"room-{random.getrandbits(64):016x}"
            ciphertext = encode_room_envelope(encrypt_room_message(
                self._room_id, self._pending_pw, line, client_msg_id, self._room_salt
            ))
            await self._send(
                T.SEND_ENCRYPTED_MSG,
                scope_type="room",
                scope_id=self._room_id,
                ciphertext=ciphertext,
                crypto_meta={"alg": "ChaCha20-Poly1305", "version": 1},
                client_msg_id=client_msg_id,
            )
            self._show_msg(self._username, line, False, datetime.now().timestamp())
            return

        parts = line[1:].split(maxsplit=2)
        cmd   = parts[0].lower() if parts else ""
        args  = parts[1:] if len(parts) > 1 else []
        # log command but mask password argument
        log.debug("CMD  /%s  args=%s", cmd,
                  [a if i == 0 else "***" for i, a in enumerate(args)])

        try:
            if cmd in ("quit", "exit", "q"):
                log.info("USER_QUIT")
                self._running = False

            elif cmd == "help":
                self._show_help()

            elif cmd == "log":
                n = int(args[0]) if args and args[0].isdigit() else 30
                self._show_log(n)

            elif cmd == "ttl":
                await self._send_ttl_command(args)

            elif cmd == "name":
                if not args:
                    _err("Usage: /name <username>")
                    return
                self._username = args[0]
                self._secure_sessions = SecureSessionManager(
                    self._identity, TrustStore(Path.home() / ".beamchat" / "trust.json"), self._username
                )
                log.info("SET_NAME  name=%s", self._username)
                if self._server_hello:
                    await self._send(T.SET_NAME, name=self._username)
                else:
                    _info("正在等待服务器握手完成")

            elif cmd == "create":
                if not self._username:
                    _err("Set your name first: /name <username>")
                    return
                if not self._ready:
                    _err("连接尚未就绪，请稍后再试")
                    return
                room_name        = args[0] if args else f"{self._username}'s room"
                password         = args[1] if len(args) > 1 else ""
                room_id = "".join(random.choices("ABCDEFGHJKMNPQRSTUVWXYZ23456789", k=6))
                metadata = create_room_access_metadata(room_id, password)
                self._pending_pw = password
                self._pending_room_metadata = metadata
                log.info("CREATE_ROOM  name=%s", room_name)
                await self._send(T.CREATE_ROOM, room_id=room_id, name=room_name, **dict(metadata))

            elif cmd == "join":
                if not args:
                    _err("Usage: /join <ROOM-ID> [password]")
                    return
                if not self._username:
                    _err("Set your name first: /name <username>")
                    return
                if not self._ready:
                    _err("连接尚未就绪，请稍后再试")
                    return
                room_id  = args[0].upper()
                password = args[1] if len(args) > 1 else ""
                metadata = getattr(self, "_known_room_metadata", {}).get(room_id)
                if not metadata:
                    _err("缺少房间加密元数据，无法生成访问令牌")
                    return
                try:
                    access_token = decrypt_room_access_token(room_id, password, metadata)
                except Exception:
                    _err("房间访问令牌认证失败")
                    return
                self._pending_pw = password
                self._room_salt = metadata["salt"]
                self._room_access_token = access_token
                log.info("JOIN_ROOM  room=%s", room_id)
                await self._send(T.JOIN_ROOM, room_id=room_id, access_token=access_token)

            elif cmd == "leave":
                log.info("LEAVE_ROOM  room=%s", self._room_id)
                await self._send(T.LEAVE_ROOM)

            elif cmd == "dm":
                if len(args) < 2 or not self._ready:
                    _err("用法：/dm <对端> <消息>，且连接必须已就绪")
                    return
                peer, text = args[0], args[1]
                client_msg_id = f"dm-{datetime.now().timestamp():.6f}"
                self._dm_peers.add(peer)
                self._save_offsets()
                self._pending_dms.setdefault(peer, []).append((text, client_msg_id))
                await self._send(T.GET_PEER_KEY, name=peer)

            elif cmd in ("rooms", "list"):
                log.debug("LIST_ROOMS")
                await self._send(T.LIST_ROOMS)

            elif cmd in ("me", "status"):
                console.print(
                    f"Name  : [bold]{self._username or 'not set'}[/bold]\n"
                    f"Room  : [bold yellow]{self._room_id or '-'}[/bold yellow]"
                    + (f" ({self._room_name})" if self._room_name else "") +
                    f"\nE2E   : {'[green]on[/green]' if self._crypto_key else '[dim]off[/dim]'}\n"
                    f"Server: {self._url}\n"
                    f"Log   : {LOG_FILE}"
                )

            else:
                _err(f"Unknown command '/{cmd}'.  Type /help for help.")
                log.warning("UNKNOWN_CMD  /%s", cmd)

        except Exception:
            log.error("CMD error  /%s\n%s", cmd, traceback.format_exc())

    # ── main loops ───────────────────────────────────────────────────────────

    async def _recv_loop(self):
        try:
            async for raw in self._ws:
                await self._dispatch(raw)
        except websockets.exceptions.ConnectionClosedOK:
            log.info("Connection closed normally")
        except websockets.exceptions.ConnectionClosedError as exc:
            log.error("Connection closed with error  code=%s  reason=%s",
                      exc.code, exc.reason)
        except Exception:
            log.error("RECV_LOOP unexpected error\n%s", traceback.format_exc())
            raise

    async def _input_loop(self):
        loop = asyncio.get_event_loop()
        while self._running:
            try:
                line = await loop.run_in_executor(None, sys.stdin.readline)
                if not line:
                    self._running = False
                    break
                await self._handle_line(line)
            except Exception:
                log.error("INPUT_LOOP error\n%s", traceback.format_exc())
                break

    async def run(self):
        console.print(Panel(
            "[bold cyan]P2P Chat[/bold cyan] — lightweight, E2E-encrypted messaging\n\n"
            "No public IP required — works behind NAT / firewalls\n"
            "Type [bold]/help[/bold] to see available commands\n"
            f"[dim]Log: {LOG_FILE}[/dim]",
            title="Welcome",
            border_style="cyan",
        ))

        log.info("Connecting  url=%s", self._url)
        try:
            async with websockets.connect(
                self._url,
                ping_interval=20,
                ping_timeout=60,
                open_timeout=30,
            ) as ws:
                self._ws = ws
                self._server_hello = False
                self._ready = False
                self._ephemeral_private = X25519PrivateKey.generate()
                ephemeral_public = self._ephemeral_private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
                log.info("Connected  url=%s", self._url)
                await self._send(
                    T.CLIENT_HELLO,
                    client_version=CLIENT_VERSION,
                    protocol_version=PROTOCOL_VERSION,
                    capabilities=CLIENT_CAPABILITIES,
                    key_bundle=self._identity.public_bundle(
                        ephemeral_public,
                        sign_key_bundle(self._identity, ephemeral_public, PROTOCOL_VERSION),
                        PROTOCOL_VERSION,
                    ),
                )
                recv_task  = asyncio.create_task(self._recv_loop())
                input_task = asyncio.create_task(self._input_loop())
                done, pending = await asyncio.wait(
                    [recv_task, input_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                # surface any exception from the completed task
                for t in done:
                    exc = t.exception()
                    if exc:
                        log.error("Task error  %s\n%s",
                                  type(exc).__name__,
                                  "".join(traceback.format_exception(exc)))
                for t in pending:
                    t.cancel()
                    try:
                        await t
                    except asyncio.CancelledError:
                        pass

        except ConnectionRefusedError:
            msg = f"Cannot reach server at {self._url}"
            _err(msg)
            log.error(msg)
            console.print("  Make sure the server is running:  python server.py")
        except TimeoutError as exc:
            msg = f"Connection timed out: {exc}"
            _err(msg)
            log.error(msg, exc_info=True)
        except websockets.exceptions.InvalidURI:
            msg = f"Invalid server URL: {self._url}"
            _err(msg)
            log.error(msg)
        except OSError as exc:
            log.error("Network error\n%s", traceback.format_exc())
            _err(f"Network error: {exc}")
        except Exception:
            log.error("Unexpected error in run()\n%s", traceback.format_exc())
            raise
        finally:
            log.info("Session ended  user=%s  room=%s", self._username, self._room_id)
            console.print("\n[dim]Goodbye![/dim]")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="P2P Chat client")
    parser.add_argument(
        "--server",
        default="ws://localhost:8765",
        help="Server WebSocket URL  (default: ws://localhost:8765)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Also print DEBUG logs to terminal",
    )
    args = parser.parse_args()

    if args.debug:
        _console_handler = logging.StreamHandler(sys.stderr)
        _console_handler.setLevel(logging.DEBUG)
        _console_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(_console_handler)
        log.debug("Debug mode enabled")

    try:
        asyncio.run(ChatClient(args.server).run())
    except KeyboardInterrupt:
        log.info("KeyboardInterrupt")
