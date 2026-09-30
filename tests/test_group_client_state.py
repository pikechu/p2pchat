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

from PyQt6.QtCore import QObject, QTimer, Qt, pyqtSignal
from PyQt6.QtWidgets import QApplication, QLabel, QMainWindow

from crypto import create_room_access_metadata, encode_room_envelope, encrypt_room_message
from encrypted_room_state import EncryptedRoomState, MAX_CACHED_ROOM_MESSAGES, retained_room_messages
from gui.widgets import MessageRow
from gui.window import ChatPanel, ConvPanel, MainWindow, MessagesArea
from identity import DeviceIdentity, TrustStore
from protocol import T, TTL_VALUES, pack
from secure_session import SecureSessionManager


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


def test_backfill_recomputes_sender_name_and_avatar_after_reordering(app):
    """先到的同发送者实时消息，插入另一发送者历史后必须重新显示姓名。"""
    messages = MessagesArea()
    messages.add_message("甲", "已缓存", 100, message_id=1)
    live = messages.add_message("甲", "实时消息", 300, message_id=3)
    messages.add_message("乙", "离线历史", 200, message_id=2)

    live_row = live.parentWidget()
    assert not live.findChild(QLabel, "BubbleSender").isHidden()
    assert not live_row.avatar.isHidden()


def test_messages_without_server_ids_preserve_same_second_arrival_order(app):
    """无持久化回复的整数秒时间，不能排到先发的本地问句之前。"""
    messages = MessagesArea(own_name="我")
    question = messages.add_message("我", "问句", 100.8, outgoing=True)
    answer = messages.add_message("甲", "稍后回复", 100.0)

    rows = [messages._lay.itemAt(index).widget() for index in range(messages._lay.count() - 1)
            if isinstance(messages._lay.itemAt(index).widget(), MessageRow)]
    assert [row.content for row in rows] == [question, answer]


def test_history_sort_preserves_pending_bubble_until_server_confirms_id(app):
    messages = MessagesArea(own_name="我")
    messages.add_message("甲", "实时消息", 300, message_id=30)
    pending = messages.add_message("我", "等待确认", 301, outgoing=True)
    messages.add_message("乙", "历史消息", 200, message_id=20)
    rows = [messages._lay.itemAt(index).widget() for index in range(messages._lay.count() - 1)
            if isinstance(messages._lay.itemAt(index).widget(), MessageRow)]
    assert [row.content._text for row in rows] == ["历史消息", "实时消息", "等待确认"]
    assert rows[-1].content is pending

    messages.update_message_id(pending, 31, 301)
    rows = [messages._lay.itemAt(index).widget() for index in range(messages._lay.count() - 1)
            if isinstance(messages._lay.itemAt(index).widget(), MessageRow)]
    assert [row.content._text for row in rows] == ["历史消息", "实时消息", "等待确认"]
    assert rows[-1].content is pending


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


def test_cache_restore_preserves_same_second_messages_without_server_ids(app, tmp_path):
    identity = _identity()
    original = _window()
    original._room_state = EncryptedRoomState(tmp_path, identity, "ws://localhost:8765")
    original._room_history["ROOM01"] = [
        {"sender": "我", "text": "问句", "ts": 100.8, "outgoing": True, "message_id": 0},
        {"sender": "甲", "text": "稍后回复", "ts": 100.0, "outgoing": False, "message_id": 0},
    ]
    original._save_room_state()

    restored = _window()
    restored._room_state = EncryptedRoomState(tmp_path, identity, "ws://localhost:8765")
    restored._restore_room_state()
    assert _texts(restored._chat, "ROOM01") == ["问句", "稍后回复"]
    assert [message["text"] for message in restored._room_history["ROOM01"]] == ["问句", "稍后回复"]


class _DelayedBridge(QObject):
    """只模拟正常连接回调；关闭确认可延迟，以覆盖已经入队的旧信号。"""

    received = pyqtSignal(str)
    connected = pyqtSignal()
    disconnected = pyqtSignal(str)
    reconnecting = pyqtSignal(int)
    finished = pyqtSignal()

    def __init__(self, url, username):
        super().__init__()
        self.url = url
        self._username = username
        self._running = False
        self.frames = []
        self.deleted = False

    def set_disconnect_cleanup(self, callback):
        self.cleanup = callback

    def send_frame(self, msg_type, **payload):
        self.frames.append((msg_type, payload))
        return True

    def start(self):
        self._running = True

    def close(self):
        pass

    def wait(self, _timeout):
        return not self._running

    def isRunning(self):
        return self._running

    def finish(self):
        self._running = False
        self.finished.emit()

    def deleteLater(self):
        self.deleted = True


def _server_window(tmp_path):
    window = _window()
    identity = _identity()
    window._server_url = "ws://localhost:8765"
    window._room_state = EncryptedRoomState(tmp_path, identity, window._server_url)
    window._secure_sessions = SecureSessionManager(identity, TrustStore(tmp_path / "trust.json"), "我")
    window._bridge = None
    window._voice_call = None
    window._pending_dms = {}
    window._pending_key_requests = set()
    window._ft_manager = MagicMock()
    window._ft_manager._dir = tmp_path
    window._ice_servers = []
    window._files_panel = MagicMock()
    window._webrtc_transfer = MagicMock()
    window._new_webrtc_transfer = MagicMock(side_effect=lambda *_args: MagicMock())
    window._close_all_file_transfers = MagicMock()
    window._on_connected = MagicMock()
    window._on_disconnected = MagicMock()
    window._on_reconnecting = MagicMock()
    for rid in window._rooms:
        window._rooms[rid]["access_token"] = f"服务器甲-{rid}"
    return window


def test_switch_server_saves_and_restores_group_cache_without_cross_server_joins(app, tmp_path, monkeypatch):
    """同一个群编号在两台正常服务器中具有独立凭证与历史。"""
    monkeypatch.setattr("gui.window.WSBridge", _DelayedBridge)
    window = _server_window(tmp_path)
    window._dispatch_frame(T.NEW_MSG, {"room_id": "ROOM01", "sender": "甲", "text": "服务器甲消息", "message_id": 17}, 100)
    window._conv.set_pinned("ROOM01", True)
    window._save_room_state()
    original_cache = window._room_state
    window._connect()
    old_transfer = window._webrtc_transfer
    old_bridge = window._bridge

    window._switch_server_state("ws://localhost:8766")
    window._connect()
    second_bridge = window._bridge
    second_bridge.received.emit(pack(T.READY, name="我"))
    assert not any(msg_type == T.JOIN_ROOM for msg_type, _payload in second_bridge.frames)
    assert window._rooms == {} and window._message_offsets == {}
    assert window._chat._msgs_by_room == {} and window._dm_peers == set()
    assert old_bridge.cleanup is old_transfer.close_all

    window._rooms["__pending__"] = {"password": "乙密码", "access_token": "服务器乙令牌"}
    window._dispatch_frame(T.ROOM_CREATED, {"room_id": "ROOM01", "name": "服务器乙群"}, 101)
    window._rooms["ROOM01"]["message_ttl_seconds"] = 0
    window._dispatch_frame(T.NEW_MSG, {"room_id": "ROOM01", "sender": "乙", "text": "服务器乙消息", "message_id": 29}, 102)
    second_cache = window._room_state
    assert second_cache.path != original_cache.path
    assert original_cache.load()["history"]["ROOM01"][0]["text"] == "服务器甲消息"

    window._switch_server_state("ws://localhost:8765")
    window._connect()
    window._bridge.received.emit(pack(T.READY, name="我"))
    assert window._room_state.path == original_cache.path
    assert _texts(window._chat, "ROOM01") == ["服务器甲消息"]
    assert window._message_offsets["room:ROOM01"] == 17
    assert window._conv._pinned == {"ROOM01"}
    joins = [payload for msg_type, payload in window._bridge.frames if msg_type == T.JOIN_ROOM]
    assert joins == [{"room_id": rid, "access_token": f"服务器甲-{rid}"} for rid in ("ROOM01", "ROOM02")]
    assert second_cache.load()["history"]["ROOM01"][0]["text"] == "服务器乙消息"
    assert old_bridge in window._retired_bridges
    old_bridge.finish()
    assert old_bridge not in window._retired_bridges and old_bridge.deleted


def test_delayed_old_bridge_callbacks_cannot_modify_new_server_state(app, tmp_path, monkeypatch):
    monkeypatch.setattr("gui.window.WSBridge", _DelayedBridge)
    window = _server_window(tmp_path)
    window._connect()
    old_bridge = window._bridge
    window._switch_server_state("ws://localhost:8766")
    window._connect()
    new_bridge = window._bridge
    new_bridge.received.emit(pack(T.READY, name="我"))

    def deliver_old_signals():
        old_bridge.received.emit(pack(T.READY, name="我"))
        old_bridge.received.emit(pack(T.ERROR, code="ROOM_NOT_FOUND", room_id="ROOM01"))
        old_bridge.connected.emit()
        old_bridge.disconnected.emit("closed")
        old_bridge.reconnecting.emit(1)

    QTimer.singleShot(0, deliver_old_signals)
    before_frames = list(new_bridge.frames)
    app.processEvents()
    assert window._identified
    assert new_bridge.frames == before_frames
    assert window._on_connected.call_count == window._on_disconnected.call_count == window._on_reconnecting.call_count == 0
    assert window._rooms == {} and window._joined_room_ids == set()


def test_old_server_webrtc_results_do_not_create_conversations_or_update_cards(app, tmp_path):
    window = _server_window(tmp_path)
    window._server_generation = 1
    window._ft_cards = {"existing": MagicMock()}
    window._webrtc_file_pending = {"existing": {}}
    stale = {"_server_generation": 0, "transfer_id": "existing", "session_id": "existing",
             "direction": "receive", "peer": "旧对端"}
    window._on_webrtc_file_received(tmp_path / "尚未保存.txt", stale)
    window._on_webrtc_file_sent(tmp_path / "原文件.txt", stale)
    window._on_webrtc_file_progress(stale)
    window._on_webrtc_channel_open(stale)
    window._on_webrtc_session_closed(stale)
    window._files_panel.add_file.assert_not_called()
    window._ft_cards["existing"].set_progress.assert_not_called()
    assert "existing" in window._ft_cards and "existing" in window._webrtc_file_pending
    assert window._dms == {}


def test_queued_old_webrtc_failure_cannot_fallback_through_new_server(app):
    """已排队的旧服失败闭包不能借用新服桥接执行中继回退。"""
    window = MainWindow.__new__(MainWindow)
    QMainWindow.__init__(window)
    window._server_generation = 0
    window._webrtc_task_failed.connect(window._on_webrtc_task_failed, Qt.ConnectionType.QueuedConnection)
    stale_fallback = MagicMock()
    current_fallback = MagicMock()
    error = OSError("连接失败")
    window._webrtc_task_failed.emit((0, stale_fallback), error)
    window._server_generation = 1
    window._webrtc_task_failed.emit((1, current_fallback), error)
    app.processEvents()

    stale_fallback.assert_not_called()
    current_fallback.assert_called_once_with(error)
    window.deleteLater()
