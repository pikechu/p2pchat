"""语音帧 AEAD 加密和重放保护。"""

from __future__ import annotations

import base64
import binascii
import json
import struct
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


VOICE_PACKET_VERSION = 1
VOICE_PACKET_ALG = "VOICE-AEAD-v1"
VOICE_REPLAY_WINDOW = 128
_KEY_LENGTH = 32
_NONCE_LENGTH = 12
_HKDF_SALT = b"BeamChat/voice/hkdf/v1"


class VoiceCryptoError(Exception):
    """表示语音密钥、包格式、认证或重放检查失败。"""


@dataclass
class _ReplayWindow:
    size: int = VOICE_REPLAY_WINDOW
    max_seen: int = -1
    seen: set[int] = field(default_factory=set)

    def check_and_mark(self, seq: int) -> None:
        if seq < 0:
            raise VoiceCryptoError("语音包序列号无效")
        if seq in self.seen:
            raise VoiceCryptoError("语音包重复")
        if self.max_seen >= 0 and seq < self.max_seen - self.size + 1:
            raise VoiceCryptoError("语音包已超出重放窗口")
        self.seen.add(seq)
        if seq > self.max_seen:
            self.max_seen = seq
            floor = self.max_seen - self.size + 1
            self.seen = {value for value in self.seen if value >= floor}


class VoiceCipher:
    """为单一发送方向加密或解密语音 PCM 帧。"""

    def __init__(self, key: bytes, *, call_id: str, sender: str, recipient: str, direction: str):
        self._call_id = _require_text(call_id, "call_id")
        self._sender = _require_text(sender, "sender")
        self._recipient = _require_text(recipient, "recipient")
        self._direction = _require_text(direction, "direction")
        self._key = _derive_direction_key(_require_key(key), self._call_id, self._direction)
        self._seq = 0
        self._replay = _ReplayWindow()

    def encrypt(self, pcm: bytes) -> dict:
        """加密一帧 PCM，返回可 JSON 序列化的 voice payload。"""
        if not isinstance(pcm, bytes):
            raise VoiceCryptoError("PCM 必须是字节串")
        seq = self._seq
        self._seq += 1
        nonce = _nonce_for_seq(seq)
        aad = _aad(self._call_id, self._sender, self._recipient, self._direction, seq)
        ciphertext = ChaCha20Poly1305(self._key).encrypt(nonce, pcm, aad)
        return {
            "version": VOICE_PACKET_VERSION,
            "alg": VOICE_PACKET_ALG,
            "call_id": self._call_id,
            "sender": self._sender,
            "recipient": self._recipient,
            "direction": self._direction,
            "seq": seq,
            "nonce": _b64(nonce),
            "ciphertext": _b64(ciphertext),
        }

    def decrypt(self, packet: dict | str | bytes) -> bytes:
        """验证方向、AAD 和重放窗口后解密一帧 PCM。"""
        packet = decode_voice_packet(packet)
        try:
            if packet.get("version") != VOICE_PACKET_VERSION or packet.get("alg") != VOICE_PACKET_ALG:
                raise ValueError
            call_id = str(packet["call_id"])
            sender = str(packet["sender"])
            recipient = str(packet["recipient"])
            direction = str(packet["direction"])
            seq = int(packet["seq"])
            nonce = _unb64(packet["nonce"])
            ciphertext = _unb64(packet["ciphertext"])
        except (KeyError, TypeError, ValueError, UnicodeError, binascii.Error) as exc:
            raise VoiceCryptoError("语音包格式无效") from exc
        if (
            call_id != self._call_id
            or sender != self._sender
            or recipient != self._recipient
            or direction != self._direction
            or nonce != _nonce_for_seq(seq)
        ):
            raise VoiceCryptoError("语音包上下文不匹配")
        try:
            plaintext = ChaCha20Poly1305(self._key).decrypt(
                nonce, ciphertext, _aad(call_id, sender, recipient, direction, seq)
            )
        except InvalidTag as exc:
            raise VoiceCryptoError("语音包认证失败") from exc
        self._replay.check_and_mark(seq)
        return plaintext


def encode_voice_packet(packet: dict) -> bytes:
    """将 voice payload 编码为 UDP 可发送字节。"""
    return json.dumps(packet, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def decode_voice_packet(packet: dict | str | bytes) -> dict:
    """解析 relay dict、JSON str 或 UDP bytes 形式的 voice payload。"""
    if isinstance(packet, dict):
        return packet
    try:
        if isinstance(packet, bytes):
            packet = packet.decode("utf-8")
        decoded = json.loads(packet)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise VoiceCryptoError("语音包格式无效") from exc
    if not isinstance(decoded, dict):
        raise VoiceCryptoError("语音包格式无效")
    return decoded


def is_encrypted_voice_payload(packet) -> bool:
    """服务端用于拒绝旧明文语音帧的轻量结构检查。"""
    if not isinstance(packet, dict):
        return False
    required = {"version", "alg", "call_id", "sender", "recipient", "direction", "seq", "nonce", "ciphertext"}
    if set(packet) != required:
        return False
    if packet.get("version") != VOICE_PACKET_VERSION or packet.get("alg") != VOICE_PACKET_ALG:
        return False
    try:
        seq = int(packet["seq"])
        nonce = base64.b64decode(packet["nonce"], validate=True)
        base64.b64decode(packet["ciphertext"], validate=True)
    except (TypeError, ValueError, binascii.Error):
        return False
    return seq >= 0 and nonce == _nonce_for_seq(seq)


def _derive_direction_key(key: bytes, call_id: str, direction: str) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=_KEY_LENGTH,
        salt=_HKDF_SALT,
        info=b"call\x00" + call_id.encode("utf-8") + b"\x00direction\x00" + direction.encode("utf-8"),
    ).derive(key)


def _aad(call_id: str, sender: str, recipient: str, direction: str, seq: int) -> bytes:
    return json.dumps(
        {
            "call_id": call_id,
            "sender": sender,
            "recipient": recipient,
            "direction": direction,
            "seq": seq,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _nonce_for_seq(seq: int) -> bytes:
    if not isinstance(seq, int) or seq < 0 or seq > 0xFFFFFFFFFFFFFFFF:
        raise VoiceCryptoError("语音包序列号无效")
    return b"\x00\x00\x00\x00" + struct.pack(">Q", seq)


def _require_key(key: bytes) -> bytes:
    if not isinstance(key, bytes) or len(key) != _KEY_LENGTH:
        raise VoiceCryptoError("语音密钥长度无效")
    return key


def _require_text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise VoiceCryptoError(f"{label} 无效")
    return value


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.b64decode(value, validate=True)
