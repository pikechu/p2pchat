# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the application

```bash
# Install dependencies
pip install -r requirements.txt

# Run the relay server (default: ws://0.0.0.0:8765)
python server.py
python server.py --host 0.0.0.0 --port 9000

# Terminal client
python client.py
python client.py --server ws://HOST:8765
python client.py --debug   # also prints DEBUG logs to stderr

# GUI client (PyQt6)
python gui_client.py
python gui_client.py --server ws://HOST:8765 --name Alice --theme dark

# One-click server + SSH reverse tunnel (exposes local server to public internet)
python start.py --ssh user@vps-host
python start.py --ssh user@vps-host:2222 --port 9000
```

## Architecture

The app is a **NAT-transparent P2P chat** where clients connect outbound-only to a central relay server. No client needs a public IP. The relay never decrypts messages; all E2E encryption happens on clients.

### Core modules (shared by both clients)

| File | Role |
|------|------|
| `protocol.py` | Wire format: every frame is `{type, payload, ts, mid}` as JSON. `T` enum lists all message types. `pack()`/`unpack()` are the only serialisation functions. |
| `crypto.py` | E2E encryption: `derive_key(room_id, password)` → Fernet key via PBKDF2-HMAC-SHA256 (200k iterations). `encrypt()`/`decrypt()` wrap Fernet. A room without a password still derives a key from room_id alone (per-room isolation, not secret). |
| `server.py` | `ChatServer` manages room lifecycle (`Room` dataclass) and routes messages. State is three dicts: `_ws_to_name`, `_name_to_ws`, `_user_room`. Rooms auto-destroy when the last member leaves. |

### Terminal client (`client.py`)

`ChatClient.run()` opens a WebSocket and runs two concurrent asyncio tasks:
- `_recv_loop` — receives frames and dispatches to `_dispatch()`
- `_input_loop` — reads stdin in a thread executor, parses `/commands`

E2E key is held in `self._crypto_key`; set on `/create` or `/join` with a password.

### GUI client (`gui_client.py` + `gui/`)

| File | Role |
|------|------|
| `gui/bridge.py` | `WSBridge(QThread)` runs its own asyncio event loop in a background thread. GUI thread calls `send_frame()` which puts frames into an `asyncio.Queue` via `run_coroutine_threadsafe`. Incoming frames emit `received(str)` Qt signal back to the GUI thread. |
| `gui/window.py` | `MainWindow` owns a `WSBridge` and all panels. `_on_frame()` is the incoming-frame dispatcher (mirrors `_dispatch()` in the terminal client). Room state is `self._rooms: dict[str, dict]` keyed by room_id, holding `{name, members, locked, key}`. The crypto key for a room being created is stored under `"__pending__"` until the server returns the real room_id. |
| `gui/theme.py` | `make_qss(theme)` returns a QSS stylesheet string. `TOKENS` dict holds colour/sizing constants for light and dark themes. |
| `gui/widgets.py` | Custom Qt widgets: `Avatar`, `StatusDot`, `BubbleWidget`, `SysMsgWidget`, `DayMarkWidget`, `ConvRowWidget`. |

### Thread-safety in the GUI

The asyncio WebSocket loop runs in `WSBridge`'s QThread. The GUI thread never touches the WebSocket directly — it calls `WSBridge.send_frame()` (thread-safe via `run_coroutine_threadsafe`) and receives data via Qt signals (`received`, `connected`, `disconnected`).

### Protocol flow

```
client → server: SET_NAME → WELCOME/SYSTEM/ERROR
client → server: CREATE_ROOM | JOIN_ROOM → ROOM_CREATED | ROOM_JOINED + USER_JOINED broadcast
client → server: SEND_MSG → NEW_MSG broadcast (sender excluded)
client → server: LEAVE_ROOM → ROOM_LEFT + USER_LEFT broadcast
client → server: LIST_ROOMS → ROOM_LIST
```

Room IDs are 6-char alphanumeric (ambiguous chars 0/O/1/I/L excluded). Encrypted messages carry `encrypted: true` in payload; the ciphertext is the entire `text` field. The server relays the payload verbatim without inspecting it.
