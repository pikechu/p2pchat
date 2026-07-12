import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json
from protocol import (
    CLIENT_CAPABILITIES,
    CLIENT_VERSION,
    PROTOCOL_VERSION,
    SERVER_CAPABILITIES,
    T,
    pack,
    unpack,
)


def test_file_offer_type_exists():
    assert T.FILE_OFFER == "FILE_OFFER"


def test_file_accept_type_exists():
    assert T.FILE_ACCEPT == "FILE_ACCEPT"


def test_file_reject_type_exists():
    assert T.FILE_REJECT == "FILE_REJECT"


def test_file_chunk_type_exists():
    assert T.FILE_CHUNK == "FILE_CHUNK"


def test_file_done_type_exists():
    assert T.FILE_DONE == "FILE_DONE"


def test_file_error_type_exists():
    assert T.FILE_ERROR == "FILE_ERROR"


def test_room_file_chunk_ack_type_exists():
    assert T.FILE_ROOM_CHUNK_ACK == "FILE_ROOM_CHUNK_ACK"


def test_room_file_done_ack_type_exists():
    assert T.FILE_ROOM_DONE_ACK == "FILE_ROOM_DONE_ACK"


def test_room_file_received_type_exists():
    assert T.FILE_ROOM_RECEIVED == "FILE_ROOM_RECEIVED"


def test_webrtc_signal_types_exist():
    assert T.WEBRTC_OFFER == "WEBRTC_OFFER"
    assert T.WEBRTC_ANSWER == "WEBRTC_ANSWER"
    assert T.WEBRTC_ICE == "WEBRTC_ICE"
    assert T.WEBRTC_CLOSE == "WEBRTC_CLOSE"
    assert T.WEBRTC_ERROR == "WEBRTC_ERROR"


def test_offline_sync_protocol_types_exist():
    assert T.CLIENT_HELLO == "CLIENT_HELLO"
    assert T.SERVER_HELLO == "SERVER_HELLO"
    assert T.READY == "READY"
    assert T.GET_PEER_KEY == "GET_PEER_KEY"
    assert T.PEER_KEY_BUNDLE == "PEER_KEY_BUNDLE"
    assert T.SEND_ENCRYPTED_MSG == "SEND_ENCRYPTED_MSG"
    assert T.NEW_ENCRYPTED_MSG == "NEW_ENCRYPTED_MSG"
    assert T.SYNC_MESSAGES == "SYNC_MESSAGES"
    assert T.SYNC_MESSAGES_RESULT == "SYNC_MESSAGES_RESULT"


def test_protocol_versioning_constants_exist():
    assert PROTOCOL_VERSION >= 3
    assert CLIENT_VERSION
    assert "offline_message_sync" in CLIENT_CAPABILITIES
    assert "offline_message_sync" in SERVER_CAPABILITIES
    for capability in (
        "authenticated_key_exchange", "encrypted_files", "encrypted_voice", "ttl_policy",
    ):
        assert capability in CLIENT_CAPABILITIES
        assert capability in SERVER_CAPABILITIES


def test_file_offer_pack_roundtrip():
    raw = pack(T.FILE_OFFER,
               to="bob", transfer_id="abc123",
               filename="cat.png", size=204800, mime="image/png")
    msg = unpack(raw)
    assert msg["type"] == "FILE_OFFER"
    p = msg["payload"]
    assert p["to"] == "bob"
    assert p["transfer_id"] == "abc123"
    assert p["filename"] == "cat.png"
    assert p["size"] == 204800
    assert p["mime"] == "image/png"


def test_file_chunk_pack_roundtrip():
    raw = pack(T.FILE_CHUNK,
               to="bob", transfer_id="abc123",
               index=0, total=3, data="AAAA")
    msg = unpack(raw)
    assert msg["type"] == "FILE_CHUNK"
    p = msg["payload"]
    assert p["index"] == 0
    assert p["total"] == 3
    assert p["data"] == "AAAA"


def test_webrtc_offer_pack_roundtrip():
    raw = pack(T.WEBRTC_OFFER,
               to="bob", session_id="s1",
               sdp={"type": "offer", "sdp": "v=0"})
    msg = unpack(raw)
    assert msg["type"] == "WEBRTC_OFFER"
    p = msg["payload"]
    assert p["to"] == "bob"
    assert p["session_id"] == "s1"
    assert p["sdp"]["type"] == "offer"
