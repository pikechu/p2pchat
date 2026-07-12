import base64
import importlib
import json
import os
import stat

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


def identity_module():
    return importlib.import_module("identity")


def raw_x25519_public() -> bytes:
    return X25519PrivateKey.generate().public_key().public_bytes(
        Encoding.Raw,
        PublicFormat.Raw,
    )


def make_bundle(tmp_path, protocol_version=7):
    module = identity_module()
    path = tmp_path / "identity.json"
    identity = module.IdentityStore(path).load_or_create()
    ephemeral_public = raw_x25519_public()
    signature = module.sign_key_bundle(identity, ephemeral_public, protocol_version)
    return module, identity.public_bundle(ephemeral_public, signature, protocol_version)


def filesystem_preserves_private_mode(path):
    probe = path / "mode-probe"
    probe.write_text("x", encoding="utf-8")
    os.chmod(probe, 0o600)
    return stat.S_IMODE(probe.stat().st_mode) == 0o600


def test_identity_store_persists_identity_and_prekey(tmp_path):
    module = identity_module()
    path = tmp_path / "identity.json"

    created = module.IdentityStore(path).load_or_create()
    reloaded = module.IdentityStore(path).load_or_create()

    assert created.identity_public == reloaded.identity_public
    assert created.prekey_public == reloaded.prekey_public
    assert path.exists()


@pytest.mark.skipif(os.name != "posix", reason="仅 POSIX 平台要求 0600 权限")
def test_identity_store_writes_owner_only_permissions(tmp_path):
    module = identity_module()
    path = tmp_path / "identity.json"

    if not filesystem_preserves_private_mode(tmp_path):
        pytest.skip("当前文件系统不支持 POSIX 权限位")

    module.IdentityStore(path).load_or_create()

    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_fingerprint_is_full_sha256_in_uppercase_groups(tmp_path):
    module = identity_module()
    public_key = module.IdentityStore(tmp_path / "identity.json").load_or_create().identity_public

    value = module.fingerprint(public_key)

    groups = value.split(" ")
    assert len(groups) == 16
    assert all(len(group) == 4 and group == group.upper() for group in groups)
    assert "".join(groups) == __import__("hashlib").sha256(public_key).hexdigest().upper()


def test_valid_key_bundle_contains_base64_public_material_and_verifies(tmp_path):
    module, bundle = make_bundle(tmp_path)

    assert set(bundle) == {
        "identity_public",
        "prekey_public",
        "prekey_signature",
        "ephemeral_public",
        "ephemeral_signature",
    }
    for value in bundle.values():
        assert isinstance(value, str)
        assert base64.b64decode(value, validate=True)
    assert "private" not in json.dumps(bundle).lower()
    assert module.verify_key_bundle(bundle, 7) is True


def test_key_bundle_can_be_created_after_reload_without_hidden_protocol_state(tmp_path):
    module = identity_module()
    path = tmp_path / "identity.json"
    created = module.IdentityStore(path).load_or_create()
    ephemeral_public = raw_x25519_public()
    signature = module.sign_key_bundle(created, ephemeral_public, 7)
    reloaded = module.IdentityStore(path).load_or_create()

    bundle = reloaded.public_bundle(ephemeral_public, signature, 7)

    assert module.verify_key_bundle(bundle, 7) is True


@pytest.mark.parametrize(
    "field",
    [
        "identity_public",
        "prekey_public",
        "prekey_signature",
        "ephemeral_public",
        "ephemeral_signature",
    ],
)
def test_key_bundle_rejects_each_tampered_field(tmp_path, field):
    module, bundle = make_bundle(tmp_path)
    raw = bytearray(base64.b64decode(bundle[field], validate=True))
    raw[0] ^= 1
    bundle[field] = base64.b64encode(bytes(raw)).decode("ascii")

    assert module.verify_key_bundle(bundle, 7) is False


def test_key_bundle_rejects_wrong_protocol_version_and_invalid_base64(tmp_path):
    module, bundle = make_bundle(tmp_path)

    assert module.verify_key_bundle(bundle, 8) is False
    bundle["ephemeral_public"] = "not base64!"
    assert module.verify_key_bundle(bundle, 7) is False


def test_trust_store_observation_and_explicit_acceptance(tmp_path):
    module = identity_module()
    path = tmp_path / "trust.json"
    store = module.TrustStore(path)
    first_key = b"a" * 32
    changed_key = b"b" * 32

    assert store.observe("alice", first_key) is module.TrustDecision.NEW
    assert store.observe("alice", first_key) is module.TrustDecision.TRUSTED
    assert store.observe("alice", changed_key) is module.TrustDecision.CHANGED
    assert store.observe("alice", first_key) is module.TrustDecision.TRUSTED

    store.accept("alice", changed_key)

    assert store.observe("alice", changed_key) is module.TrustDecision.TRUSTED
    assert module.TrustStore(path).observe("alice", changed_key) is module.TrustDecision.TRUSTED


def test_trust_store_uses_json_and_rejects_corruption(tmp_path):
    module = identity_module()
    path = tmp_path / "trust.json"
    store = module.TrustStore(path)

    assert store.observe("alice", b"a" * 32) is module.TrustDecision.NEW
    assert json.loads(path.read_text(encoding="utf-8"))["alice"]

    path.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(ValueError, match="信任库"):
        store.observe("alice", b"a" * 32)


def test_trust_store_rejects_invalid_record_before_any_update(tmp_path):
    module = identity_module()
    path = tmp_path / "trust.json"
    path.write_text(json.dumps({"alice": "not base64!"}), encoding="utf-8")

    with pytest.raises(ValueError, match="信任库"):
        module.TrustStore(path).observe("bob", b"b" * 32)
