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

PROTOCOL_VERSION = 3
CLIENT_VERSION = "1.1.4"
BASE_CAPABILITIES = [
    "authenticated_key_exchange",
    "encrypted_files",
    "encrypted_voice",
    "ttl_policy",
    "encrypted_message_persistence",
    "room_message_ttl",
    "dm_message_ttl",
    "offline_message_sync",
    "streaming_file_transfer",
    "file_room_chunk_ack",
]
REQUIRED_CAPABILITIES = [
    "authenticated_key_exchange",
    "encrypted_files",
    "encrypted_voice",
    "ttl_policy",
]
CLIENT_CAPABILITIES = list(BASE_CAPABILITIES)
SERVER_CAPABILITIES = [
    "authenticated_key_exchange",
    "encrypted_files",
    "encrypted_voice",
    "ttl_policy",
    "encrypted_message_persistence",
    "room_message_ttl",
    "dm_message_ttl",
    "offline_message_sync",
]


class T(str, Enum):
    # client → server
    CLIENT_HELLO = "CLIENT_HELLO"
    GET_PEER_KEY = "GET_PEER_KEY"
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
    SEND_ENCRYPTED_MSG = "SEND_ENCRYPTED_MSG"  # {scope_type, scope_id, ciphertext, crypto_meta, ...}
    SYNC_MESSAGES = "SYNC_MESSAGES"  # {scopes: [{scope_type, scope_id, after_message_id}], limit}
    SET_MESSAGE_TTL = "SET_MESSAGE_TTL"  # {scope_type, scope_id, ttl_seconds, to?}; ttl_seconds=0 表示永久
    DELETE_ROOM    = "DELETE_ROOM"    # {room_id} — creator only
    SET_ROOM_NAME  = "SET_ROOM_NAME"  # {room_id, name} — creator only
    SET_ROOM_ICON  = "SET_ROOM_ICON"  # {room_id, icon} — creator only
    SET_AVATAR     = "SET_AVATAR"     # {data: base64 PNG} — client → server

    # voice call (client↔server, user-to-user relay like FILE_*)
    CALL_OFFER   = "CALL_OFFER"   # {to, room_id?}
    CALL_ANSWER  = "CALL_ANSWER"  # {to}
    CALL_REJECT  = "CALL_REJECT"  # {to, reason?}
    CALL_HANGUP  = "CALL_HANGUP"  # {to}
    CALL_ICE     = "CALL_ICE"     # {to, candidate: {ip, port}}
    VOICE_CHUNK  = "VOICE_CHUNK"  # {to, data: base64 PCM int16}

    # WebRTC signaling (client↔server, user-to-user relay only)
    WEBRTC_OFFER = "WEBRTC_OFFER"  # {to, session_id, sdp}
    WEBRTC_ANSWER = "WEBRTC_ANSWER"  # {to, session_id, sdp}
    WEBRTC_ICE = "WEBRTC_ICE"  # {to, session_id, candidate}
    WEBRTC_CLOSE = "WEBRTC_CLOSE"  # {to, session_id}
    WEBRTC_ERROR = "WEBRTC_ERROR"  # {to, session_id, message}

    # file transfer (client→server, routed user-to-user)
    FILE_OFFER  = "FILE_OFFER"   # {to, transfer_id, encrypted_metadata, size, total, ciphertext_size}
    FILE_ACCEPT = "FILE_ACCEPT"  # {to, transfer_id}
    FILE_REJECT = "FILE_REJECT"  # {to, transfer_id, reason}
    FILE_CHUNK  = "FILE_CHUNK"   # {to, transfer_id, index, total, encrypted_chunk}
    FILE_DONE   = "FILE_DONE"    # {to, transfer_id, encrypted_done}
    FILE_ERROR  = "FILE_ERROR"   # {to, transfer_id, message}

    # room-based file sharing (client→server, broadcast to all room members)
    FILE_ROOM_SHARE     = "FILE_ROOM_SHARE"      # {room_id, transfer_id, encrypted_metadata, size, total, ciphertext_size}
    FILE_ROOM_CHUNK     = "FILE_ROOM_CHUNK"      # {transfer_id, index, total, encrypted_chunk}
    FILE_ROOM_DONE      = "FILE_ROOM_DONE"       # {transfer_id, encrypted_done}
    FILE_ROOM_RECEIVED  = "FILE_ROOM_RECEIVED"   # {transfer_id}
    # server → all room members
    FILE_ROOM_CHUNK_ACK = "FILE_ROOM_CHUNK_ACK"  # {transfer_id, index}
    FILE_ROOM_DONE_ACK  = "FILE_ROOM_DONE_ACK"   # {transfer_id}
    FILE_ROOM_AVAILABLE = "FILE_ROOM_AVAILABLE"  # legacy, unused for encrypted streaming
    FILE_ROOM_ERROR     = "FILE_ROOM_ERROR"      # {transfer_id, message}

    # server → client
    SERVER_HELLO = "SERVER_HELLO"
    READY        = "READY"
    PEER_KEY_BUNDLE = "PEER_KEY_BUNDLE"
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
    NEW_ENCRYPTED_MSG = "NEW_ENCRYPTED_MSG"
    SYNC_MESSAGES_RESULT = "SYNC_MESSAGES_RESULT"
    MESSAGE_TTL_UPDATED = "MESSAGE_TTL_UPDATED"
    ROOM_DELETED      = "ROOM_DELETED"      # {room_id} — broadcast when creator deletes room
    ROOM_NAME_UPDATED = "ROOM_NAME_UPDATED" # {room_id, name} — broadcast on rename
    ROOM_ICON_UPDATED = "ROOM_ICON_UPDATED" # {room_id, icon} — broadcast on icon change
    USER_AVATAR    = "USER_AVATAR"    # {name, data: base64 PNG} — server → client


def pack(msg_type: T, **payload) -> str:
    return json.dumps({
        "type": msg_type.value,
        "payload": payload,
        "ts": time.time(),
        "mid": uuid.uuid4().hex[:8],
    })


def unpack(raw: str) -> dict:
    return json.loads(raw)
