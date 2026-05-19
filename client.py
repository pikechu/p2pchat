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
  /help                         — show command list
  /quit                         — exit
  <anything else>               — send as message to current room
"""

import asyncio
import argparse
import sys
from datetime import datetime
from typing import Optional

import websockets
import websockets.exceptions
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from protocol import T, pack, unpack
from crypto import derive_key, encrypt, decrypt

console = Console(highlight=False)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _info(msg: str):
    console.print(f"[dim]{_ts()}  {msg}[/dim]")


def _sys(msg: str):
    console.print(f"[dim]{_ts()}  * {msg}[/dim]")


def _err(msg: str):
    console.print(f"[dim]{_ts()}[/dim]  [bold red]✗ {msg}[/bold red]")


# ── Client ────────────────────────────────────────────────────────────────────

class ChatClient:
    def __init__(self, server_url: str):
        self._url          = server_url
        self._ws           = None
        self._username: Optional[str] = None
        self._room_id: Optional[str]  = None
        self._room_name: Optional[str] = None
        self._crypto_key: Optional[bytes] = None  # set when room has a password
        self._pending_pw   = ""   # stashed until ROOM_CREATED returns the room_id
        self._running      = True

    # ── display ──────────────────────────────────────────────────────────────

    def _show_msg(self, sender: str, text: str, encrypted: bool, ts: float):
        time_str = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
        if encrypted and self._crypto_key:
            plain = decrypt(self._crypto_key, text)
            if plain is None:
                text = "[bold red][wrong password — cannot decrypt][/bold red]"
            else:
                text = plain
        elif encrypted and not self._crypto_key:
            text = "[dim][encrypted — join with the room password to read][/dim]"

        is_me = sender == self._username
        name_style = "bold cyan" if is_me else "bold green"
        label = "You" if is_me else sender
        console.print(f"[dim]{time_str}[/dim]  [{name_style}]{label}[/{name_style}]: {text}")

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
            ("/help",                         "Show this help"),
            ("/quit",                         "Exit the client"),
            ("<message>",                     "Send a message to your room"),
        ]
        for cmd, desc in rows:
            t.add_row(cmd, desc)
        console.print(t)

    # ── send helpers ─────────────────────────────────────────────────────────

    async def _send(self, msg_type: T, **payload):
        if self._ws:
            try:
                await self._ws.send(pack(msg_type, **payload))
            except websockets.exceptions.ConnectionClosed:
                pass

    # ── incoming message dispatcher ──────────────────────────────────────────

    async def _dispatch(self, raw: str):
        try:
            msg = unpack(raw)
        except Exception:
            return
        mtype   = msg.get("type", "")
        payload = msg.get("payload", {})
        ts      = msg.get("ts", datetime.now().timestamp())

        if mtype == T.WELCOME:
            _info(payload.get("message", ""))

        elif mtype == T.SYSTEM:
            _sys(payload.get("message", ""))

        elif mtype == T.ERROR:
            _err(payload.get("message", ""))

        elif mtype == T.ROOM_CREATED:
            self._room_id   = payload["room_id"]
            self._room_name = payload["name"]
            # derive crypto key now that we have the room_id
            if self._pending_pw:
                self._crypto_key = derive_key(self._room_id, self._pending_pw)
                self._pending_pw = ""
            else:
                self._crypto_key = None
            lock_note = " [bold yellow](🔒 E2E encrypted)[/bold yellow]" if self._crypto_key else ""
            console.print(Panel(
                f"Room ID: [bold yellow]{self._room_id}[/bold yellow]\n"
                f"Name   : {self._room_name}{lock_note}\n\n"
                f"Share the Room ID with others so they can /join",
                title="[bold green]Room Created[/bold green]",
                border_style="green",
            ))

        elif mtype == T.ROOM_JOINED:
            self._room_id   = payload["room_id"]
            self._room_name = payload["name"]
            members         = payload.get("members", [])
            lock_note = " [bold yellow](🔒 E2E encrypted)[/bold yellow]" if self._crypto_key else ""
            console.print(Panel(
                f"Room ID: [bold yellow]{self._room_id}[/bold yellow]\n"
                f"Name   : {self._room_name}{lock_note}\n"
                f"Online : {', '.join(members)}",
                title="[bold green]Joined Room[/bold green]",
                border_style="green",
            ))

        elif mtype == T.ROOM_LEFT:
            _sys("You left the room")
            self._room_id    = None
            self._room_name  = None
            self._crypto_key = None

        elif mtype == T.USER_JOINED:
            _sys(f"{payload['username']} joined the room")

        elif mtype == T.USER_LEFT:
            _sys(f"{payload['username']} left the room")

        elif mtype == T.NEW_MSG:
            self._show_msg(
                payload.get("sender", "?"),
                payload.get("text", ""),
                payload.get("encrypted", False),
                ts,
            )

        elif mtype == T.ROOM_LIST:
            rooms = payload.get("rooms", [])
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
                t.add_row(
                    r["id"], r["name"], str(r["members"]),
                    r["creator"], "🔒" if r["locked"] else "",
                )
            console.print(t)

    # ── command parser ───────────────────────────────────────────────────────

    async def _handle_line(self, line: str):
        line = line.strip()
        if not line:
            return

        if not line.startswith("/"):
            # plain message
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
            await self._send(T.SEND_MSG, text=text, encrypted=encrypted)
            # display own message locally (server echoes to others only)
            self._show_msg(self._username, line, False, datetime.now().timestamp())
            return

        # parse command
        parts = line[1:].split(maxsplit=2)
        cmd   = parts[0].lower() if parts else ""
        args  = parts[1:] if len(parts) > 1 else []

        if cmd in ("quit", "exit", "q"):
            self._running = False

        elif cmd == "help":
            self._show_help()

        elif cmd == "name":
            if not args:
                _err("Usage: /name <username>")
                return
            self._username = args[0]
            await self._send(T.SET_NAME, name=self._username)

        elif cmd == "create":
            if not self._username:
                _err("Set your name first: /name <username>")
                return
            room_name       = args[0] if args else f"{self._username}'s room"
            password        = args[1] if len(args) > 1 else ""
            self._pending_pw = password          # stash; key derived after we get room_id
            self._crypto_key = None
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
            await self._send(T.JOIN_ROOM, room_id=room_id, password=password)

        elif cmd == "leave":
            await self._send(T.LEAVE_ROOM)

        elif cmd in ("rooms", "list"):
            await self._send(T.LIST_ROOMS)

        elif cmd in ("me", "status"):
            console.print(
                f"Name  : [bold]{self._username or 'not set'}[/bold]\n"
                f"Room  : [bold yellow]{self._room_id or '—'}[/bold yellow]"
                + (f" ({self._room_name})" if self._room_name else "") +
                f"\nE2E   : {'[green]on[/green]' if self._crypto_key else '[dim]off[/dim]'}\n"
                f"Server: {self._url}"
            )

        else:
            _err(f"Unknown command '/{cmd}'.  Type /help for help.")

    # ── main loops ───────────────────────────────────────────────────────────

    async def _recv_loop(self):
        async for raw in self._ws:
            await self._dispatch(raw)

    async def _input_loop(self):
        loop = asyncio.get_event_loop()
        while self._running:
            try:
                line = await loop.run_in_executor(None, sys.stdin.readline)
                if not line:          # EOF
                    self._running = False
                    break
                await self._handle_line(line)
            except Exception:
                break

    async def run(self):
        console.print(Panel(
            "[bold cyan]P2P Chat[/bold cyan] — lightweight, E2E-encrypted messaging\n\n"
            "No public IP required — works behind NAT / firewalls\n"
            "Type [bold]/help[/bold] to see available commands",
            title="Welcome",
            border_style="cyan",
        ))

        try:
            async with websockets.connect(
                self._url,
                ping_interval=20,
                ping_timeout=60,
                open_timeout=10,
            ) as ws:
                self._ws = ws
                recv_task  = asyncio.create_task(self._recv_loop())
                input_task = asyncio.create_task(self._input_loop())
                done, pending = await asyncio.wait(
                    [recv_task, input_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                    try:
                        await t
                    except asyncio.CancelledError:
                        pass

        except ConnectionRefusedError:
            _err(f"Cannot reach server at {self._url}")
            console.print("  Make sure the server is running:  python server.py")
        except websockets.exceptions.InvalidURI:
            _err(f"Invalid server URL: {self._url}")
        except OSError as e:
            _err(f"Network error: {e}")
        finally:
            console.print("\n[dim]Goodbye![/dim]")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="P2P Chat client")
    parser.add_argument(
        "--server",
        default="ws://localhost:8765",
        help="Server WebSocket URL  (default: ws://localhost:8765)",
    )
    args = parser.parse_args()
    try:
        asyncio.run(ChatClient(args.server).run())
    except KeyboardInterrupt:
        pass
