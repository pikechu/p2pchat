from identity import DeviceIdentity
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from identity import sign_key_bundle
from protocol import PROTOCOL_VERSION
from server import ChatServer, Room


def _bundle():
    identity = DeviceIdentity(Ed25519PrivateKey.generate(), X25519PrivateKey.generate())
    ephemeral = X25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return identity.public_bundle(
        ephemeral,
        sign_key_bundle(identity, ephemeral, PROTOCOL_VERSION),
        PROTOCOL_VERSION,
    )


def test_room_creator_permission_is_bound_to_device_identity(monkeypatch):
    owner_bundle = _bundle()
    attacker_bundle = _bundle()
    room = Room(
        id="ABCDEF",
        name="secure",
        creator="alice",
        creator_identity=owner_bundle["identity_public"],
    )
    server = ChatServer(enable_message_persistence=False)
    monkeypatch.setattr(server, "_save_rooms", lambda: None)

    assert server._is_room_creator(room, "alice", owner_bundle)
    assert not server._is_room_creator(room, "alice", attacker_bundle)
    assert not server._is_room_creator(room, "mallory", owner_bundle)
