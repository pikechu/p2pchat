import base64

import pytest


def _cipher_pair():
    from voice_crypto import VoiceCipher

    key = b"v" * 32
    sender = "alice"
    recipient = "bob"
    call_id = "call-1"
    direction = "alice->bob"
    encryptor = VoiceCipher(key, call_id=call_id, sender=sender, recipient=recipient, direction=direction)
    decryptor = VoiceCipher(key, call_id=call_id, sender=sender, recipient=recipient, direction=direction)
    return encryptor, decryptor


def test_voice_cipher_round_trip_and_packet_does_not_contain_pcm():
    encryptor, decryptor = _cipher_pair()
    pcm = (b"\x01\x02" * 320)

    packet = encryptor.encrypt(pcm)

    assert isinstance(packet, dict)
    assert packet["version"] == 1
    assert packet["alg"] == "VOICE-AEAD-v1"
    assert packet["seq"] == 0
    assert base64.b64encode(pcm).decode("ascii") not in str(packet)
    assert pcm.hex() not in str(packet)
    assert decryptor.decrypt(packet) == pcm


def test_voice_cipher_rejects_tampering_and_replay():
    from voice_crypto import VoiceCryptoError

    encryptor, decryptor = _cipher_pair()
    packet = encryptor.encrypt(b"pcm-frame")
    tampered = dict(packet)
    raw = bytearray(base64.b64decode(tampered["ciphertext"], validate=True))
    raw[0] ^= 1
    tampered["ciphertext"] = base64.b64encode(bytes(raw)).decode("ascii")

    with pytest.raises(VoiceCryptoError):
        decryptor.decrypt(tampered)

    assert decryptor.decrypt(packet) == b"pcm-frame"
    with pytest.raises(VoiceCryptoError):
        decryptor.decrypt(packet)


def test_voice_cipher_rejects_old_sequence_outside_128_packet_window():
    from voice_crypto import VoiceCryptoError

    encryptor, decryptor = _cipher_pair()
    packets = [encryptor.encrypt(bytes([i % 256])) for i in range(130)]

    assert decryptor.decrypt(packets[129]) == bytes([129 % 256])
    assert decryptor.decrypt(packets[2]) == bytes([2])
    with pytest.raises(VoiceCryptoError):
        decryptor.decrypt(packets[1])


def test_voice_cipher_rejects_wrong_direction_or_participants():
    from voice_crypto import VoiceCipher, VoiceCryptoError

    encryptor, _ = _cipher_pair()
    packet = encryptor.encrypt(b"pcm-frame")

    wrong_direction = VoiceCipher(
        b"v" * 32, call_id="call-1", sender="alice", recipient="bob", direction="bob->alice"
    )
    wrong_participant = VoiceCipher(
        b"v" * 32, call_id="call-1", sender="alice", recipient="carol", direction="alice->carol"
    )

    with pytest.raises(VoiceCryptoError):
        wrong_direction.decrypt(packet)
    with pytest.raises(VoiceCryptoError):
        wrong_participant.decrypt(packet)


def test_voice_cipher_binds_direction_key_to_call_id():
    from voice_crypto import VoiceCipher, VoiceCryptoError

    key = b"v" * 32
    first = VoiceCipher(key, call_id="call-1", sender="alice", recipient="bob", direction="alice->bob")
    second = VoiceCipher(key, call_id="call-2", sender="alice", recipient="bob", direction="alice->bob")
    packet_1 = first.encrypt(b"same-pcm")
    packet_2 = second.encrypt(b"same-pcm")

    assert packet_1["seq"] == packet_2["seq"] == 0
    assert packet_1["nonce"] == packet_2["nonce"]
    assert packet_1["ciphertext"] != packet_2["ciphertext"]

    decryptor = VoiceCipher(key, call_id="call-2", sender="alice", recipient="bob", direction="alice->bob")
    with pytest.raises(VoiceCryptoError):
        decryptor.decrypt(packet_1)


def test_room_voice_packet_from_previous_call_is_rejected_by_new_call_id():
    from voice_crypto import VoiceCipher, VoiceCryptoError

    room_voice_key = b"r" * 32
    old_call = VoiceCipher(room_voice_key, call_id="old-call", sender="alice", recipient="bob", direction="alice->bob")
    new_call_rx = VoiceCipher(room_voice_key, call_id="new-call", sender="alice", recipient="bob", direction="alice->bob")

    with pytest.raises(VoiceCryptoError):
        new_call_rx.decrypt(old_call.encrypt(b"room-pcm"))
