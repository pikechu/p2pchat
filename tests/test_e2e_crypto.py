import base64

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


def public_bytes(private_key):
    return private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def test_x25519_root_and_scope_keys_are_deterministic_and_separated():
    from e2e_crypto import CryptoError, derive_scope_keys, derive_x25519_root

    alice = X25519PrivateKey.generate()
    bob = X25519PrivateKey.generate()
    alice_root = derive_x25519_root(alice, public_bytes(bob), b"dm/root")
    bob_root = derive_x25519_root(bob, public_bytes(alice), b"dm/root")

    assert alice_root == bob_root
    first = derive_scope_keys(alice_root, "dm", "alice:bob")
    second = derive_scope_keys(alice_root, "dm", "alice:bob")
    other_scope = derive_scope_keys(alice_root, "room", "general")
    assert first == second
    assert len({first.message_key, first.file_key, first.voice_key}) == 3
    assert first.message_key != other_scope.message_key
    with pytest.raises(CryptoError):
        derive_scope_keys(alice_root, "", "alice:bob")


def test_aead_envelope_round_trip_and_rejects_tampering():
    from e2e_crypto import CryptoError, decrypt_envelope, encrypt_envelope

    key = b"k" * 32
    context = {"scope_type": "room", "scope_id": "general", "message_id": "m-1"}
    envelope = encrypt_envelope(key, b"secret message", context)
    second_envelope = encrypt_envelope(key, b"secret message", context)

    assert envelope["version"] == 1
    assert envelope["nonce"] != second_envelope["nonce"]
    assert envelope["ciphertext"] != second_envelope["ciphertext"]
    assert decrypt_envelope(key, envelope, context) == b"secret message"

    for field, value in (("ciphertext", None), ("nonce", None), ("version", 2), ("ciphertext", "not base64!")):
        tampered = dict(envelope)
        if value is None:
            raw = bytearray(base64.b64decode(tampered[field], validate=True))
            raw[0] ^= 1
            tampered[field] = base64.b64encode(bytes(raw)).decode("ascii")
        else:
            tampered[field] = value
        with pytest.raises(CryptoError):
            decrypt_envelope(key, tampered, context)

    with pytest.raises(CryptoError):
        decrypt_envelope(key, envelope, {**context, "message_id": "m-2"})


def test_dm_envelope_allows_both_participants_and_rejects_third_party():
    from e2e_crypto import CryptoError, decrypt_dm_envelope, encrypt_dm_for_participants

    alice_prekey = X25519PrivateKey.generate()
    bob_prekey = X25519PrivateKey.generate()
    mallory_prekey = X25519PrivateKey.generate()
    alice_identity = b"a" * 32
    bob_identity = b"b" * 32
    context = {"scope_type": "dm", "scope_id": "alice:bob", "message_id": "dm-1"}
    envelope = encrypt_dm_for_participants(
        b"private message",
        sender_identity_public=alice_identity,
        recipient_identity_public=bob_identity,
        sender_prekey_private=alice_prekey,
        sender_prekey_public=public_bytes(alice_prekey),
        recipient_prekey_public=public_bytes(bob_prekey),
        **context,
    )

    assert decrypt_dm_envelope(
        alice_prekey, alice_identity, bob_identity, envelope, **context
    ) == b"private message"
    assert decrypt_dm_envelope(
        bob_prekey, bob_identity, alice_identity, envelope, **context
    ) == b"private message"
    with pytest.raises(CryptoError):
        decrypt_dm_envelope(
            mallory_prekey, b"m" * 32, alice_identity, envelope, **context
        )
    with pytest.raises(CryptoError):
        decrypt_dm_envelope(
            bob_prekey, bob_identity, alice_identity, envelope, message_id="dm-2", scope_type="dm", scope_id="alice:bob"
        )
    with pytest.raises(CryptoError):
        decrypt_dm_envelope(
            bob_prekey, bob_identity, alice_identity, envelope, message_id="dm-1", scope_type="dm", scope_id="alice:eve"
        )


def test_dm_envelope_rejects_mismatched_sender_prekey_and_tampered_wrap_fields():
    from e2e_crypto import CryptoError, decrypt_dm_envelope, encrypt_dm_for_participants

    alice_prekey = X25519PrivateKey.generate()
    bob_prekey = X25519PrivateKey.generate()
    wrong_prekey = X25519PrivateKey.generate()
    alice_identity = b"a" * 32
    bob_identity = b"b" * 32
    context = {"scope_type": "dm", "scope_id": "alice:bob", "message_id": "dm-1"}

    with pytest.raises(CryptoError):
        encrypt_dm_for_participants(
            b"private message",
            sender_identity_public=alice_identity,
            recipient_identity_public=bob_identity,
            sender_prekey_private=alice_prekey,
            sender_prekey_public=public_bytes(wrong_prekey),
            recipient_prekey_public=public_bytes(bob_prekey),
            **context,
        )

    envelope = encrypt_dm_for_participants(
        b"private message",
        sender_identity_public=alice_identity,
        recipient_identity_public=bob_identity,
        sender_prekey_private=alice_prekey,
        sender_prekey_public=public_bytes(alice_prekey),
        recipient_prekey_public=public_bytes(bob_prekey),
        **context,
    )
    tampered = {
        **envelope,
        "key_wraps": [dict(item) for item in envelope["key_wraps"]],
    }
    tampered["key_wraps"][1]["sender_prekey_public"] = base64.b64encode(public_bytes(wrong_prekey)).decode("ascii")
    with pytest.raises(CryptoError):
        decrypt_dm_envelope(bob_prekey, bob_identity, alice_identity, tampered, **context)

    invalid_base64 = {
        **envelope,
        "key_wraps": [dict(item) for item in envelope["key_wraps"]],
    }
    invalid_base64["key_wraps"][1]["recipient_prekey_public"] = "not base64!"
    with pytest.raises(CryptoError):
        decrypt_dm_envelope(bob_prekey, bob_identity, alice_identity, invalid_base64, **context)


def test_room_root_is_reproducible_and_wrong_password_cannot_decrypt_token():
    from e2e_crypto import CryptoError, decrypt_envelope, derive_room_root, encrypt_envelope

    salt = b"s" * 16
    root = derive_room_root("general", "correct password", salt)
    assert root == derive_room_root("general", "correct password", salt)
    context = {"scope_type": "room-token", "scope_id": "general", "message_id": "access"}
    token = encrypt_envelope(root, b"access-token", context)

    with pytest.raises(CryptoError):
        decrypt_envelope(derive_room_root("general", "wrong password", salt), token, context)
