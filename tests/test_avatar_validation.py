import base64
import struct

from server import ChatServer, MAX_AVATAR_BYTES


def _png_header(width: int, height: int, padding: int = 0) -> str:
    raw = (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x06\x00\x00\x00"
        + b"\x00\x00\x00\x00"
        + b"x" * padding
    )
    return base64.b64encode(raw).decode("ascii")


def test_avatar_accepts_bounded_png():
    assert ChatServer._valid_avatar_data(_png_header(128, 128))


def test_avatar_rejects_oversized_dimensions_and_payload():
    assert not ChatServer._valid_avatar_data(_png_header(513, 128))
    assert not ChatServer._valid_avatar_data(_png_header(128, 128, MAX_AVATAR_BYTES))
    assert not ChatServer._valid_avatar_data(base64.b64encode(b"not an image").decode("ascii"))
