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
import logging
import logging.handlers
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

from protocol import T, pack, unpack
from crypto import derive_key, encrypt, decrypt

# ── Logging setup ─────────────────────────────────────────────────────────────

LOG_FILE = Path(__file__).parent / "client.log"

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
        self._running         = True
        log.info("Client initialised  server=%s", server_url)

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
        """Print last n lines of client.log to the terminal."""
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

            elif mtype == T.SYSTEM:
                _sys(payload.get("message", ""))

            elif mtype == T.ERROR:
                server_msg = payload.get("message", "")
                _err(server_msg)
                log.warning("SERVER_ERROR  %s", server_msg)

            elif mtype == T.ROOM_CREATED:
                self._room_id   = payload["room_id"]
                self._room_name = payload["name"]
                if self._pending_pw:
                    self._crypto_key = derive_key(self._room_id, self._pending_pw)
                    self._pending_pw = ""
                else:
                    self._crypto_key = None
                lock_note = " [bold yellow](E2E encrypted)[/bold yellow]" if self._crypto_key else ""
                console.print(Panel(
                    f"Room ID: [bold yellow]{self._room_id}[/bold yellow]\n"
                    f"Name   : {self._room_name}{lock_note}\n\n"
                    f"Share the Room ID with others so they can /join",
                    title="[bold green]Room Created[/bold green]",
                    border_style="green",
                ))
                log.info("ROOM_CREATED  room=%s  name=%s  encrypted=%s",
                         self._room_id, self._room_name, bool(self._crypto_key))

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

            elif mtype == T.ROOM_LEFT:
                _sys("You left the room")
                log.info("ROOM_LEFT  room=%s", self._room_id)
                self._room_id    = None
                self._room_name  = None
                self._crypto_key = None

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
                log.debug("MSG  from=%s  encrypted=%s  room=%s", sender, encrypted, self._room_id)
                self._show_msg(sender, payload.get("text", ""), encrypted, ts)

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
            text, encrypted = line, False
            if self._crypto_key:
                text      = encrypt(self._crypto_key, line)
                encrypted = True
            log.debug("SEND_MSG  room=%s  encrypted=%s  len=%d",
                      self._room_id, encrypted, len(text))
            await self._send(T.SEND_MSG, text=text, encrypted=encrypted)
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

            elif cmd == "name":
                if not args:
                    _err("Usage: /name <username>")
                    return
                self._username = args[0]
                log.info("SET_NAME  name=%s", self._username)
                await self._send(T.SET_NAME, name=self._username)

            elif cmd == "create":
                if not self._username:
                    _err("Set your name first: /name <username>")
                    return
                room_name        = args[0] if args else f"{self._username}'s room"
                password         = args[1] if len(args) > 1 else ""
                self._pending_pw = password
                self._crypto_key = None
                log.info("CREATE_ROOM  name=%s  encrypted=%s", room_name, bool(password))
                await self._send(T.CREATE_ROOM, name=room_name, password=password)

            elif cmd == "join":
                if not args:
                    _err("Usage: /join <ROOM-ID> [password]")
                    return
                if not self._username:
                    _err("Set your name first: /name <username>")
                    return
                room_id  = args[0].upper()
                password = args[1] if len(args) > 1 else ""
                self._crypto_key = derive_key(room_id, password) if password else None
                log.info("JOIN_ROOM  room=%s  encrypted=%s", room_id, bool(password))
                await self._send(T.JOIN_ROOM, room_id=room_id, password=password)

            elif cmd == "leave":
                log.info("LEAVE_ROOM  room=%s", self._room_id)
                await self._send(T.LEAVE_ROOM)

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
                log.info("Connected  url=%s", self._url)
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
