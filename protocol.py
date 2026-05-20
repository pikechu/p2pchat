"""
Wire protocol: every frame is a JSON object.

  { "type": str, "payload": {...}, "ts": float, "mid": str }

Client → Server types: SET_NAME, CREATE_ROOM, JOIN_ROOM, LEAVE_ROOM, SEND_MSG, LIST_ROOMS
Server → Client types: WELCOME, SYSTEM, ERROR, ROOM_CREATED, ROOM_JOINED, ROOM_LEFT,
                        NEW_MSG, USER_JOINED, USER_LEFT, ROOM_LIST
"""

from enum import Enum
import json
import time
import uuid


class T(str, Enum):
    # client → server
    SET_NAME    = "SET_NAME"
    CREATE_ROOM = "CREATE_ROOM"
    JOIN_ROOM   = "JOIN_ROOM"
    LEAVE_ROOM  = "LEAVE_ROOM"
    SEND_MSG    = "SEND_MSG"
    LIST_ROOMS  = "LIST_ROOMS"
    TYPING      = "TYPING"       # {typing: bool}
    MSG_ACK     = "MSG_ACK"      # {seq: int, status: "delivered"|"read"}
    LIST_USERS  = "LIST_USERS"   # {} — request list of online usernames
    SEND_DM     = "SEND_DM"      # {to, text, client_mid}
    DELETE_ROOM = "DELETE_ROOM"  # {room_id} — creator only

    # file transfer (client→server, routed user-to-user)
    FILE_OFFER  = "FILE_OFFER"   # {to, transfer_id, filename, size, mime}
    FILE_ACCEPT = "FILE_ACCEPT"  # {to, transfer_id}
    FILE_REJECT = "FILE_REJECT"  # {to, transfer_id, reason}
    FILE_CHUNK  = "FILE_CHUNK"   # {to, transfer_id, index, total, data (base64)}
    FILE_DONE   = "FILE_DONE"    # {to, transfer_id, sha256}
    FILE_ERROR  = "FILE_ERROR"   # {to, transfer_id, message}

    # room-based file sharing (client→server, broadcast to all room members)
    FILE_ROOM_SHARE     = "FILE_ROOM_SHARE"      # {room_id, transfer_id, filename, size, mime}
    FILE_ROOM_CHUNK     = "FILE_ROOM_CHUNK"      # {transfer_id, index, total, data (base64)}
    FILE_ROOM_DONE      = "FILE_ROOM_DONE"       # {transfer_id, sha256}
    # server → all room members
    FILE_ROOM_AVAILABLE = "FILE_ROOM_AVAILABLE"  # {transfer_id, filename, size, mime, from_user, room_id, sha256, chunks}
    FILE_ROOM_ERROR     = "FILE_ROOM_ERROR"      # {transfer_id, message}

    # server → client
    WELCOME      = "WELCOME"
    SYSTEM       = "SYSTEM"
    ERROR        = "ERROR"
    ROOM_CREATED = "ROOM_CREATED"
    ROOM_JOINED  = "ROOM_JOINED"
    ROOM_LEFT    = "ROOM_LEFT"
    NEW_MSG      = "NEW_MSG"
    USER_JOINED  = "USER_JOINED"
    USER_LEFT    = "USER_LEFT"
    ROOM_LIST    = "ROOM_LIST"
    USER_TYPING  = "USER_TYPING"  # {username, room_id, typing: bool}
    MSG_STATUS   = "MSG_STATUS"   # {seq, status, from_user, room_id}
    SEND_ACK     = "SEND_ACK"     # {seq, client_mid} — echoed to original sender
    USER_LIST    = "USER_LIST"    # {users: [str]} — response to LIST_USERS
    RECV_DM      = "RECV_DM"      # {from, text, client_mid} — routed by server
    DM_ACK       = "DM_ACK"       # {client_mid, to} — echo back to DM sender
    ROOM_DELETED = "ROOM_DELETED" # {room_id} — broadcast when creator deletes room


def pack(msg_type: T, **payload) -> str:
    return json.dumps({
        "type": msg_type.value,
        "payload": payload,
        "ts": time.time(),
        "mid": uuid.uuid4().hex[:8],
    })


def unpack(raw: str) -> dict:
    return json.loads(raw)
