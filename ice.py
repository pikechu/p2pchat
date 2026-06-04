"""STUN query and UDP hole-punching for 1v1 NAT traversal."""
import os
import socket
import struct
import time
from typing import Optional, Tuple

STUN_HOST    = "stun.l.google.com"
STUN_PORT    = 19302
STUN_TIMEOUT = 3.0
PUNCH_PROBES = 5       # UDP packets to send per direction during hole-punch
PUNCH_DELAY  = 0.05    # seconds between probes


def get_external_address(local_sock: socket.socket) -> Optional[Tuple[str, int]]:
    """
    Query the STUN server using an existing bound UDP socket.
    Returns (external_ip, external_port) or None on failure.
    The socket is NOT closed — caller keeps using it for data.
    """
    try:
        tid = os.urandom(12)
        request = struct.pack(">HHI12s", 0x0001, 0, 0x2112A442, tid)
        stun_ip = socket.gethostbyname(STUN_HOST)
        old_timeout = local_sock.gettimeout()
        local_sock.settimeout(STUN_TIMEOUT)
        local_sock.sendto(request, (stun_ip, STUN_PORT))
        data, _ = local_sock.recvfrom(512)
        local_sock.settimeout(old_timeout)
        return _parse_stun_response(data, tid)
    except Exception:
        return None


def send_hole_punch_probes(sock: socket.socket, peer_addr: Tuple[str, int]) -> None:
    """Send PUNCH_PROBES UDP pings to peer_addr to open NAT pinholes."""
    for _ in range(PUNCH_PROBES):
        try:
            sock.sendto(b"PING", peer_addr)
        except Exception:
            break
        time.sleep(PUNCH_DELAY)


def _parse_stun_response(data: bytes, transaction_id: bytes) -> Optional[Tuple[str, int]]:
    if len(data) < 20:
        return None
    msg_type, _msg_len, magic, tid = struct.unpack(">HHI12s", data[:20])
    if msg_type != 0x0101 or tid != transaction_id:
        return None
    magic_int = 0x2112A442
    pos = 20
    while pos + 4 <= len(data):
        attr_type, attr_len = struct.unpack(">HH", data[pos:pos + 4])
        pos += 4
        if attr_type == 0x0020 and attr_len >= 8:   # XOR-MAPPED-ADDRESS
            family = data[pos + 1]
            if family == 0x01:   # IPv4
                xport, xip_int = struct.unpack(">HI", data[pos + 2:pos + 8])
                port = xport ^ (magic_int >> 16)
                ip   = socket.inet_ntoa(struct.pack(">I", xip_int ^ magic_int))
                return (ip, port)
        aligned = attr_len + (4 - attr_len % 4) % 4
        pos += aligned
    return None
