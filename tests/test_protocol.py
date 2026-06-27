import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json
from protocol import T, pack, unpack


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
