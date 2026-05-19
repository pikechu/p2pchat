"""
End-to-end encryption for password-protected rooms.

Key derivation: PBKDF2-HMAC-SHA256(room_id + password, salt, 200_000 iters) → Fernet key
Encryption: AES-128-CBC + HMAC-SHA256 (Fernet)

Without a password the key is derived from room_id only — provides per-room isolation
but anyone who knows the room_id could derive the key. Recommend using passwords for
sensitive conversations.
"""

import base64
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

_SALT = b"p2pchat::v1::salt"


def derive_key(room_id: str, password: str = "") -> bytes:
    """Return a Fernet-ready 32-byte URL-safe base64 key."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_SALT + room_id.encode(),
        iterations=200_000,
    )
    raw = kdf.derive((room_id + password).encode())
    return base64.urlsafe_b64encode(raw)


def encrypt(key: bytes, plaintext: str) -> str:
    return Fernet(key).encrypt(plaintext.encode()).decode()


def decrypt(key: bytes, ciphertext: str) -> str | None:
    try:
        return Fernet(key).decrypt(ciphertext.encode()).decode()
    except (InvalidToken, Exception):
        return None
