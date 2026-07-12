from crypto import decrypt, derive_key, encrypt


def test_legacy_fernet_ciphertexts_remain_readable():
    key = derive_key("legacy-room", "password")

    ciphertext = encrypt(key, "historical message")

    assert decrypt(key, ciphertext) == "historical message"
    assert decrypt(key, "not a fernet token") is None


def test_crypto_exposes_the_unified_aead_envelope_interface():
    from crypto import decrypt_envelope, encrypt_envelope

    context = {"scope_type": "room", "scope_id": "new-room", "message_id": "m-1"}
    envelope = encrypt_envelope(b"k" * 32, b"new message", context)

    assert decrypt_envelope(b"k" * 32, envelope, context) == b"new message"
