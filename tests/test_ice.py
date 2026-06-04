import struct, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _make_stun_response(ip: str, port: int, transaction_id: bytes) -> bytes:
    """Build a minimal STUN Binding Success Response with XOR-MAPPED-ADDRESS."""
    import socket
    magic = 0x2112A442
    xport = port ^ (magic >> 16)
    xip   = struct.unpack(">I", socket.inet_aton(ip))[0] ^ magic

    attr_body = struct.pack(">BBHI", 0x00, 0x01, xport, xip)  # pad, family, port, ip
    attr = struct.pack(">HH", 0x0020, len(attr_body)) + attr_body

    header = struct.pack(">HHI12s", 0x0101, len(attr), magic, transaction_id)
    return header + attr


def test_parse_stun_response_returns_ip_port():
    from ice import _parse_stun_response
    tid = os.urandom(12)
    data = _make_stun_response("203.0.113.5", 54321, tid)
    result = _parse_stun_response(data, tid)
    assert result == ("203.0.113.5", 54321)


def test_parse_stun_response_wrong_tid_returns_none():
    from ice import _parse_stun_response
    tid = os.urandom(12)
    data = _make_stun_response("1.2.3.4", 1234, tid)
    assert _parse_stun_response(data, b"\x00" * 12) is None


def test_parse_stun_response_too_short_returns_none():
    from ice import _parse_stun_response
    assert _parse_stun_response(b"\x00" * 10, b"\x00" * 12) is None
