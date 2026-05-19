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


def pack(msg_type: T, **payload) -> str:
    return json.dumps({
        "type": msg_type.value,
        "payload": payload,
        "ts": time.time(),
        "mid": uuid.uuid4().hex[:8],
    })


def unpack(raw: str) -> dict:
    return json.loads(raw)
