"""群后台收信、界面切换、同步排序以及加密缓存的回归测试。"""

import json
import sys
import time
import types
from unittest.mock import MagicMock, patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

pytest.importorskip("PyQt6")
sys.modules.setdefault("sounddevice", types.SimpleNamespace(InputStream=object, OutputStream=object))

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

from crypto import create_room_access_metadata, encode_room_envelope, encrypt_room_message
from encrypted_room_state import EncryptedRoomState, MAX_CACHED_ROOM_MESSAGES, retained_room_messages
from gui.widgets import MessageRow
from gui.window import ChatPanel, ConvPanel, MainWindow
from identity import DeviceIdentity
from protocol import T, TTL_VALUES


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _identity():
    return DeviceIdentity(Ed25519PrivateKey.generate(), X25519PrivateKey.generate())


def _window():
    window = MainWindow.__new__(MainWindow)
    window._username = "我"
    window._identified = True
    window._bridge = MagicMock()
    window._bridge.send_frame.return_value = True
    window._rooms = {
        "ROOM01": {"name": "群一", "members": ["我", "甲"], "message_ttl_seconds": 0},
        "ROOM02": {"name": "群二", "members": ["我", "乙"], "message_ttl_seconds": 0},
    }
    window._joined_room_ids = {"ROOM01", "ROOM02"}
    window._pending_room_focus = ""
    window._server_room_id = "ROOM01"
    window._reconnect_room_id = ""
    window._room_sync_offsets = {}
    window._room_history = {}
    window._message_offsets = {}
    window._dm_peers = set()
    window._dms = {}
    window._displayed_message_ids = set()
    window._pending_bubbles = {}
    window._seq_bubbles = {}
    window._is_typing = False
    window._typing_timer = QTimer()
    window._send_avatar = MagicMock()
    window._save_message_offsets = MagicMock()
    window._update_private_voice_button = MagicMock()
    window.isActiveWindow = MagicMock(return_value=True)
    window.isVisible = MagicMock(return_value=True)
    window._chat = ChatPanel()
    window._conv = ConvPanel()
    for rid, room in window._rooms.items():
        window._conv.upsert_room(rid, room["name"], "", 2)
    window._chat.open_room("ROOM01", "群一", ["我", "甲"], False)
    return window


def _texts(panel, rid):
    messages = panel._msgs_by_room[rid]
    return [messages._lay.itemAt(index).widget().content._text
            for index in range(messages._lay.count() - 1)
            if isinstance(messages._lay.itemAt(index).widget(), MessageRow)]


def test_background_group_messages_survive_close_reopen_and_deduplicate(app):
    window = _window()
    MainWindow._close_room_view(window, "ROOM01")
    assert window._chat.current_room_id is None
    assert window._joined_room_ids == {"ROOM01", "ROOM02"}
    assert not any(call.args[0] == T.LEAVE_ROOM for call in window._bridge.send_frame.call_args_list)

    payload = {"room_id": "ROOM01", "sender": "甲", "text": "后台保留", "message_id": 8}
    MainWindow._dispatch_frame(window, T.NEW_MSG, payload, 100)
    MainWindow._dispatch_frame(window, T.NEW_MSG, payload, 100)
    assert window._conv._unread["ROOM01"] == 1
    assert _texts(window._chat, "ROOM01") == ["后台保留"]
    assert window._room_history["ROOM01"][0]["message_id"] == 8

    window._bridge.reset_mock()
    MainWindow._on_room_selected(window, "ROOM01")
    assert _texts(window._chat, "ROOM01") == ["后台保留"]
    assert window._conv._unread["ROOM01"] == 0
    assert not any(call.args[0] == T.LEAVE_ROOM for call in window._bridge.send_frame.call_args_list)


def test_switch_to_new_group_never_leaves_other_memberships(app):
    window = _window()
    metadata = create_room_access_metadata("ROOM03", "")
    window._rooms["ROOM03"] = {"name": "新群", "metadata": dict(metadata), "password": ""}
    MainWindow._on_room_selected(window, "ROOM03")
    window._bridge.send_frame.assert_called_once_with(T.JOIN_ROOM, room_id="ROOM03", access_token=metadata.access_token)
    assert window._joined_room_ids == {"ROOM01", "ROOM02"}


def test_explicit_leave_removes_only_target_group(app):
    window = _window()
    MainWindow._dispatch_frame(window, T.NEW_MSG, {"room_id": "ROOM02", "sender": "乙", "text": "保留", "message_id": 9}, 100)
    MainWindow._dispatch_frame(window, T.ROOM_LEFT, {"room_id": "ROOM02"}, 101)
    assert window._joined_room_ids == {"ROOM01"}
    assert window._chat.current_room_id == "ROOM01"
    assert "ROOM02" not in window._room_history
    assert "ROOM02" not in window._chat._msgs_by_room


def test_ready_rejoins_all_groups_without_stealing_dm_focus(app):
    window = _window()
    window._chat.open_room("@甲", "甲", ["我", "甲"], False)
    window._rooms["ROOM01"]["access_token"] = "token-1"
    window._rooms["ROOM02"]["access_token"] = "token-2"
    window._message_offsets = {"room:ROOM01": 5, "room:ROOM02": 7}
    MainWindow._dispatch_frame(window, T.READY, {"name": "我"}, 100)
    joins = [call.kwargs for call in window._bridge.send_frame.call_args_list if call.args[0] == T.JOIN_ROOM]
    assert joins == [{"room_id": "ROOM01", "access_token": "token-1"}, {"room_id": "ROOM02", "access_token": "token-2"}]
    for rid in ("ROOM01", "ROOM02"):
        MainWindow._dispatch_frame(window, T.ROOM_JOINED, {"room_id": rid, "name": rid, "members": ["我", "甲"]}, 101)
    assert window._chat.current_room_id == "@甲"
    assert window._conv._active is None


def test_reconnect_uses_snapshot_cursor_even_if_live_message_arrives_first(app):
    window = _window()
    window._room_history["ROOM01"] = [{"message_id": 5, "ts": 100}]
    window._message_offsets = {"room:ROOM01": 99}
    window._room_sync_offsets = {"ROOM01": 5}
    MainWindow._sync_room_messages(window, "ROOM01")
    window._bridge.send_frame.assert_called_once_with(T.SYNC_MESSAGES, scopes=[{"scope_type": "room", "scope_id": "ROOM01", "after_message_id": 5}], limit=200)


def test_missing_local_history_resyncs_instead_of_skipping_saved_offsets(app):
    window = _window()
    window._message_offsets = {"room:ROOM01": 99}
    MainWindow._sync_room_messages(window, "ROOM01")
    assert window._bridge.send_frame.call_args.kwargs["scopes"] == [{"scope_type": "room", "scope_id": "ROOM01", "after_message_id": 0, "history_mode": "all"}]


def test_history_is_sorted_before_live_messages_and_pagination_continues(app):
    window = _window()
    metadata = create_room_access_metadata("ROOM01", "")
    window._rooms["ROOM01"].update(password="", salt=metadata["salt"])
    MainWindow._dispatch_frame(window, T.NEW_MSG, {"room_id": "ROOM01", "sender": "甲", "text": "实时消息", "message_id": 30}, 300)
    encrypted = []
    for mid in (20, 10):
        ciphertext = encode_room_envelope(encrypt_room_message("ROOM01", "", f"历史{mid}", str(mid), metadata["salt"]))
        encrypted.append({"scope_type": "room", "scope_id": "ROOM01", "sender_name": "甲", "ciphertext": ciphertext, "client_msg_id": str(mid), "message_id": mid, "created_at": mid * 10})
    next_scopes = [{"scope_type": "room", "scope_id": "ROOM01", "after_message_id": 20}]
    MainWindow._dispatch_frame(window, T.SYNC_MESSAGES_RESULT, {"messages": encrypted, "has_more": True, "next_scopes": next_scopes}, 400)
    assert _texts(window._chat, "ROOM01") == ["历史10", "历史20", "实时消息"]
    window._bridge.send_frame.assert_called_with(T.SYNC_MESSAGES, scopes=next_scopes, limit=200)


def test_pinned_chats_stay_before_new_activity_and_unread_survives_metadata_update(app):
    panel = ConvPanel()
    panel.upsert_room("ROOM01", "群一", "", 1)
    panel.upsert_room("ROOM02", "群二", "", 1)
    panel.set_pinned("ROOM01", True)
    panel.increment_unread("ROOM02")
    panel.set_preview("ROOM02", "最新消息", 10)
    panel.upsert_room("ROOM02", "群二新名", "", 2)
    assert panel._list_lay.itemAt(0).widget() is panel._rows["ROOM01"]
    assert panel._unread["ROOM02"] == 1


def test_room_cache_encrypts_credentials_and_history_and_rejects_wrong_identity(tmp_path):
    identity = _identity()
    cache = EncryptedRoomState(tmp_path, identity, "ws://server-a:8765")
    state = {"rooms": {"ROOM01": {"password": "密码秘密", "access_token": "令牌秘密"}}, "history": {"ROOM01": [{"text": "消息秘密"}]}, "offsets": {"ROOM01": 9}}
    cache.save(state)
    raw = cache.path.read_text(encoding="utf-8")
    assert "密码秘密" not in raw and "令牌秘密" not in raw and "消息秘密" not in raw
    assert cache.load() == state
    assert EncryptedRoomState(tmp_path, _identity(), "ws://server-a:8765").load() == {}
    assert EncryptedRoomState(tmp_path, identity, "ws://server-b:8765").load() == {}
    envelope = json.loads(raw)
    envelope["ciphertext"] = ("B" if envelope["ciphertext"].startswith("A") else "A") + envelope["ciphertext"][1:]
    cache.path.write_text(json.dumps(envelope), encoding="utf-8")
    assert cache.load() == {}


def test_restart_restores_history_offsets_unread_and_pins_together(app, tmp_path):
    identity = _identity()
    original = _window()
    original._room_state = EncryptedRoomState(tmp_path, identity, "ws://server:8765")
    MainWindow._dispatch_frame(original, T.NEW_MSG, {"room_id": "ROOM02", "sender": "乙", "text": "重启后可见", "message_id": 17}, 100)
    original._conv.set_pinned("ROOM02", True)
    original._save_room_state()

    restored = _window()
    restored._rooms = {}
    restored._joined_room_ids = set()
    restored._chat = ChatPanel()
    restored._conv = ConvPanel()
    restored._room_state = EncryptedRoomState(tmp_path, identity, "ws://server:8765")
    restored._restore_room_state()
    assert _texts(restored._chat, "ROOM02") == ["重启后可见"]
    assert restored._message_offsets["room:ROOM02"] == 17
    assert restored._conv._unread["ROOM02"] == 1
    assert restored._conv._pinned == {"ROOM02"}
    MainWindow._sync_room_messages(restored, "ROOM02")
    assert restored._bridge.send_frame.call_args.kwargs["scopes"][0]["after_message_id"] == 17


def test_interrupted_backfill_keeps_safe_cursor_across_restart(app, tmp_path):
    identity = _identity()
    original = _window()
    original._room_state = EncryptedRoomState(tmp_path, identity, "ws://server:8765")
    original._message_offsets["room:ROOM01"] = 5
    original._room_history["ROOM01"] = [{"message_id": 5, "sender": "甲", "text": "已收", "ts": 50}]
    original._room_sync_offsets["ROOM01"] = 5
    original._sync_room_messages("ROOM01")
    MainWindow._dispatch_frame(original, T.NEW_MSG, {"room_id": "ROOM01", "sender": "甲", "text": "晚到实时消息", "message_id": 100}, 1000)
    assert original._message_offsets["room:ROOM01"] == 5
    assert original._room_state.load()["offsets"]["ROOM01"] == 5

    restored = _window()
    restored._room_state = EncryptedRoomState(tmp_path, identity, "ws://server:8765")
    restored._restore_room_state()
    restored._sync_room_messages("ROOM01")
    assert restored._bridge.send_frame.call_args.kwargs["scopes"][0]["after_message_id"] == 5
    assert restored._room_sync_offsets["ROOM01"] == 5


def test_empty_sync_result_completes_only_its_queued_group(app):
    window = _window()
    window._room_history = {
        "ROOM01": [{"message_id": 5, "ts": 50}],
        "ROOM02": [{"message_id": 7, "ts": 70}],
    }
    window._message_offsets = {"room:ROOM01": 5, "room:ROOM02": 7}
    window._sync_room_messages("ROOM01")
    window._sync_room_messages("ROOM02")
    MainWindow._dispatch_frame(window, T.SYNC_MESSAGES_RESULT, {"messages": [], "has_more": False}, 100)
    assert "ROOM01" not in window._room_sync_offsets
    assert window._room_sync_offsets == {"ROOM02": 7}
    MainWindow._dispatch_frame(window, T.SYNC_MESSAGES_RESULT, {"messages": [], "has_more": False}, 100)
    assert window._room_sync_offsets == {}


def test_live_message_during_initial_history_sync_cannot_persist_stale_offset(app, tmp_path):
    window = _window()
    window._room_state = EncryptedRoomState(tmp_path, _identity(), "ws://server:8765")
    window._message_offsets["room:ROOM01"] = 99
    window._sync_room_messages("ROOM01")
    MainWindow._dispatch_frame(window, T.NEW_MSG, {"room_id": "ROOM01", "sender": "甲", "text": "先到实时消息", "message_id": 100}, 1000)
    assert window._room_state.load()["offsets"]["ROOM01"] == 0


@pytest.mark.parametrize("view_open", [True, False])
def test_restart_before_send_ack_reconciles_existing_bubble_in_open_or_closed_view(app, tmp_path, view_open):
    identity = _identity()
    original = _window()
    original._room_state = EncryptedRoomState(tmp_path, identity, "ws://server:8765")
    original._msg_counter = 0
    metadata = create_room_access_metadata("ROOM01", "")
    original._rooms["ROOM01"].update(password="", salt=metadata["salt"])
    original._on_send_message("已发未确认")
    client_msg_id = original._room_history["ROOM01"][0]["client_msg_id"]

    restored = _window()
    restored._room_state = EncryptedRoomState(tmp_path, identity, "ws://server:8765")
    restored._restore_room_state()
    if not view_open:
        restored._close_room_view("ROOM01")
    messages = restored._chat._msgs_by_room["ROOM01"]
    original_row = next(messages._lay.itemAt(index).widget() for index in range(messages._lay.count() - 1)
                        if isinstance(messages._lay.itemAt(index).widget(), MessageRow))
    bubble = original_row.content
    restored._dispatch_frame(T.NEW_MSG, {"room_id": "ROOM01", "sender": "甲", "text": "后来的消息", "message_id": 9}, time.time() + 1)
    with patch.object(bubble, "set_status", wraps=bubble.set_status) as set_status:
        restored._dispatch_frame(T.NEW_MSG, {"room_id": "ROOM01", "sender": "我", "text": "已发未确认", "message_id": 8, "client_msg_id": client_msg_id}, time.time())
    set_status.assert_called_once_with("sent")
    assert _texts(restored._chat, "ROOM01") == ["已发未确认", "后来的消息"]
    assert original_row._message_id == 8
    assert len(restored._room_history["ROOM01"]) == 2
    assert restored._room_history["ROOM01"][0]["message_id"] == 8
    assert "room:ROOM01:8" in restored._displayed_message_ids


def test_room_cache_respects_ttl_and_message_limit():
    messages = [{"message_id": mid, "ts": mid} for mid in range(MAX_CACHED_ROOM_MESSAGES + 10)]
    assert len(retained_room_messages(messages, TTL_VALUES["permanent"], now=2000)) == MAX_CACHED_ROOM_MESSAGES
    assert [message["message_id"] for message in retained_room_messages(messages, 10, now=15)] == list(range(6, MAX_CACHED_ROOM_MESSAGES + 10))[-MAX_CACHED_ROOM_MESSAGES:]
