"""房间访问令牌与 AEAD 消息的端到端回归测试。"""

import base64
import pytest

from crypto import (
    create_room_access_metadata,
    decrypt_room_access_token,
    decrypt_room_message,
    encrypt_room_message,
)
from e2e_crypto import CryptoError
from protocol import T, pack, unpack


def test_room_metadata_never_contains_password_and_wrong_password_cannot_open_token():
    room_id = "ABC123"
    password = "correct horse battery staple"
    metadata = create_room_access_metadata(room_id, password)

    assert set(metadata) == {"salt", "encrypted_access_token", "access_token_hash"}
    assert password not in str(metadata)
    create_frame = pack(T.CREATE_ROOM, room_id=room_id, name="机密房间", **dict(metadata))
    join_frame = pack(T.JOIN_ROOM, room_id=room_id, access_token=metadata.access_token)
    assert "password" not in create_frame
    assert "password" not in join_frame
    assert "access_token_hash" in unpack(create_frame)["payload"]
    assert "access_token" in unpack(join_frame)["payload"]
    assert decrypt_room_access_token(room_id, password, metadata) == metadata.access_token
    with pytest.raises(CryptoError):
        decrypt_room_access_token(room_id, "错误密码", metadata)


def test_room_aead_message_rejects_tampering_with_room_specific_error():
    room_id = "ABC123"
    metadata = create_room_access_metadata(room_id, "密码")
    envelope = encrypt_room_message(room_id, "密码", "仅应在客户端解密", "msg-1", metadata["salt"])
    tampered = dict(envelope)
    raw = bytearray(base64.b64decode(tampered["ciphertext"].encode("ascii")))
    raw[-1] ^= 1
    tampered["ciphertext"] = base64.b64encode(bytes(raw)).decode("ascii")

    with pytest.raises(CryptoError, match="房间消息认证失败"):
        decrypt_room_message(room_id, "密码", tampered, "msg-1", metadata["salt"])
