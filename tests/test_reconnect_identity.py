import os
import pathlib
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

pytest.importorskip("PyQt6")

sounddevice_stub = types.ModuleType("sounddevice")
sounddevice_stub.InputStream = object
sounddevice_stub.OutputStream = object
sys.modules.setdefault("sounddevice", sounddevice_stub)

from PyQt6.QtCore import QPoint
from PyQt6.QtWidgets import QApplication, QMessageBox, QMenu

from crypto import create_room_access_metadata
from gui.popups import popup_above_global_pos
from gui.window import ConvPanel, MainWindow, RoomSearchDialog, TTLMenuButton
from protocol import T, TTL_VALUES
from secure_session import SecureSessionError, SessionState


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication(sys.argv)


def _make_window_stub():
    window = MainWindow.__new__(MainWindow)
    window._voice_call = MagicMock()
    window._voice_call.state.name = "IDLE"
    window._bridge = MagicMock()
    window._bridge.send_frame = MagicMock(return_value=True)
    window._username = "me"
    window._dm_peers = set()
    window._displayed_message_ids = set()
    window._rooms = {}
    window._reconnect_room_id = ""
    window._server_room_id = ""
    window._implicit_leave = False
    window._identified = True
    window._conv = MagicMock()
    window._chat = MagicMock()
    window._files_panel = MagicMock()
    window._send_avatar = MagicMock()
    window._save_message_offsets = MagicMock()
    window._update_message_offset = MagicMock()
    window._pending_dms = {}
    window._pending_key_requests = set()
    window._pending_bubbles = {}
    window.isActiveWindow = MagicMock(return_value=True)
    window.isVisible = MagicMock(return_value=True)
    window._webrtc_file_pending = {}
    window._webrtc_transfer = MagicMock()
    window._webrtc_transfer.handle_offer = AsyncMock()
    window._webrtc_transfer.handle_answer = AsyncMock()
    window._webrtc_transfer.handle_ice = AsyncMock()
    window._webrtc_transfer.close = AsyncMock()
    return window


def test_on_connected_does_not_send_followup_requests(app):
    window = _make_window_stub()
    window._username = "pp"
    window._identified = False

    with patch.object(MainWindow, "setWindowTitle"):
        MainWindow._on_connected(window)

    assert window._identified is False
    window._bridge.send_frame.assert_not_called()


def test_on_connected_does_not_clear_ready_state(app):
    window = _make_window_stub()
    window._username = "pp"
    window._identified = True

    with patch.object(MainWindow, "setWindowTitle"):
        MainWindow._on_connected(window)

    assert window._identified is True


def test_ready_unblocks_followup_requests(app):
    window = _make_window_stub()
    window._username = "pp"
    window._identified = False
    window._reconnect_room_id = "ROOM01"
    window._rooms = {"ROOM01": {"access_token": "token-123"}}

    MainWindow._dispatch_frame(window, T.READY, {"name": "pp"}, 0.0)

    assert window._identified is True
    window._send_avatar.assert_called_once()
    assert window._bridge.send_frame.call_args_list[0].args == (T.LIST_ROOMS,)
    assert window._bridge.send_frame.call_args_list[1].args == (T.JOIN_ROOM,)
    assert window._bridge.send_frame.call_args_list[1].kwargs == {"room_id": "ROOM01", "access_token": "token-123"}


def test_transient_identity_errors_do_not_popup(app):
    window = _make_window_stub()
    window._identified = False
    window._rooms = {}

    with patch.object(MainWindow, "setWindowTitle") as set_title, \
         patch.object(QMessageBox, "warning") as warning:
        MainWindow._dispatch_frame(window, T.ERROR, {"message": "SET_NAME first"}, 0.0)
        MainWindow._dispatch_frame(window, T.ERROR, {"message": "'pp' is already taken"}, 0.0)

    warning.assert_not_called()
    set_title.assert_called_once()


def test_encrypted_dm_decrypt_failure_shows_key_unavailable(app):
    window = _make_window_stub()
    window._secure_sessions = MagicMock()
    window._secure_sessions.decrypt_dm.side_effect = SecureSessionError(
        SessionState.UNAVAILABLE, "加密私聊密钥不可用"
    )

    with patch.object(QMessageBox, "warning") as warning:
        MainWindow._show_decrypted_dm(window, {"sender_name": "alice"}, 0.0)

    warning.assert_called_once()
    assert "无法解密这条私聊消息" in warning.call_args.args[2]
    assert "加密私聊密钥不可用" not in warning.call_args.args[2]


def test_peer_key_unavailable_clears_pending_dm_and_shows_actionable_prompt(app):
    window = _make_window_stub()
    bubble = MagicMock()
    window._pending_dms = {"alice": [("你好", 7)]}
    window._pending_key_requests = {"alice"}
    window._pending_bubbles = {7: bubble}

    with patch.object(QMessageBox, "warning") as warning:
        MainWindow._dispatch_frame(
            window,
            T.ERROR,
            {
                "code": "PEER_KEY_UNAVAILABLE",
                "name": "alice",
                "message": "对端不在线或无公开密钥包",
            },
            0.0,
        )

    assert "alice" not in window._pending_dms
    assert "alice" not in window._pending_key_requests
    assert 7 not in window._pending_bubbles
    bubble.set_status.assert_called_once_with("failed")
    warning.assert_called_once()
    assert "对方不在线" in warning.call_args.args[2]
    assert "加密私聊密钥不可用" not in warning.call_args.args[2]


def test_encrypted_dm_uses_dm_decrypt_path_without_room_fallback(app):
    window = _make_window_stub()
    window._username = "bob"
    window._dms = {"@alice": "alice"}
    window._dm_peers = set()
    window._chat.current_room_id = "@alice"
    window._secure_sessions = MagicMock()
    window._secure_sessions.decrypt_dm.return_value = "私聊明文"

    MainWindow._show_decrypted_dm(
        window,
        {
            "scope_type": "dm",
            "scope_id": "dm-scope",
            "sender_name": "alice",
            "recipient_name": "bob",
            "message_id": 9,
            "created_at": 1234,
        },
        1234,
    )

    window._secure_sessions.decrypt_dm.assert_called_once()
    window._chat.add_message.assert_called_once_with("alice", "私聊明文", 1234.0, outgoing=False)
    assert "alice" in window._dm_peers


def test_inactive_dm_message_increments_unread_and_updates_preview(app):
    window = _make_window_stub()
    window._username = "bob"
    window._dms = {"@alice": "alice"}
    window._dm_peers = set()
    window._chat.current_room_id = "ROOM01"
    window._secure_sessions = MagicMock()
    window._secure_sessions.decrypt_dm.return_value = "私聊明文"

    MainWindow._show_decrypted_dm(
        window,
        {
            "scope_type": "dm",
            "scope_id": "dm-scope",
            "sender_name": "alice",
            "recipient_name": "bob",
            "message_id": 9,
            "created_at": 1234,
        },
        1234,
    )

    window._chat.add_message.assert_not_called()
    window._conv.set_preview.assert_called_once_with("@alice", "alice: 私聊明文", 1234.0)
    window._conv.increment_unread.assert_called_once_with("@alice")


def test_gui_syncs_known_dm_peers(app):
    window = _make_window_stub()
    window._dm_peers = {"bob"}
    window._username = "alice"
    window._message_offsets = {"dm:scope-alice-bob": 9}
    window._secure_sessions = MagicMock()
    window._secure_sessions.dm_scope_id.return_value = "scope-alice-bob"

    MainWindow._sync_dm_messages(window)

    window._bridge.send_frame.assert_called_once_with(
        T.SYNC_MESSAGES,
        scopes=[{"scope_type": "dm", "scope_id": "scope-alice-bob", "after_message_id": 9}],
        limit=200,
    )


def test_gui_room_sync_requests_history_from_start(app):
    window = _make_window_stub()
    window._message_offsets = {"room:ROOM01": 42}

    MainWindow._sync_room_messages(window, "ROOM01")

    window._bridge.send_frame.assert_called_once_with(
        T.SYNC_MESSAGES,
        scopes=[{"scope_type": "room", "scope_id": "ROOM01", "after_message_id": 0}],
        limit=200,
    )


def test_gui_skips_duplicate_room_history_messages(app):
    window = _make_window_stub()
    window._rooms = {
        "ROOM01": {
            "password": "",
            "salt": "",
        }
    }
    window._chat.current_room_id = "ROOM01"
    window._displayed_message_ids = {"room:ROOM01:9"}

    MainWindow._dispatch_frame(
        window,
        T.NEW_MSG,
        {"sender": "alice", "text": "重复消息", "room_id": "ROOM01", "message_id": 9},
        1234,
    )

    window._chat.add_message.assert_not_called()
    window._update_message_offset.assert_called_once_with("room", "ROOM01", 9)


def test_inactive_room_message_increments_unread(app):
    window = _make_window_stub()
    window._chat.current_room_id = "ROOM01"

    MainWindow._dispatch_frame(
        window,
        T.NEW_MSG,
        {"sender": "alice", "text": "新消息", "room_id": "ROOM02", "message_id": 10},
        1234,
    )

    window._chat.add_message.assert_not_called()
    window._conv.set_preview.assert_called_once_with("ROOM02", "alice: 新消息", 1234)
    window._conv.increment_unread.assert_called_once_with("ROOM02")


def test_active_room_message_updates_preview_without_unread(app):
    window = _make_window_stub()
    window._chat.current_room_id = "ROOM01"

    MainWindow._dispatch_frame(
        window,
        T.NEW_MSG,
        {"sender": "alice", "text": "当前消息", "room_id": "ROOM01", "message_id": 10},
        1234,
    )

    window._chat.add_message.assert_called_once()
    window._conv.set_preview.assert_called_once_with("ROOM01", "alice: 当前消息", 1234)
    window._conv.increment_unread.assert_not_called()


def test_gui_marks_own_ack_as_displayed_to_skip_history_echo(app):
    window = _make_window_stub()
    bubble = MagicMock()
    window._pending_bubbles = {77: bubble}
    window._seq_bubbles = {}
    window._server_room_id = "ROOM01"

    MainWindow._dispatch_frame(
        window,
        T.SEND_ACK,
        {"client_mid": 77, "seq": 3, "scope_type": "room", "scope_id": "ROOM01", "message_id": 12},
        0,
    )

    assert "room:ROOM01:12" in window._displayed_message_ids
    MainWindow._dispatch_frame(
        window,
        T.NEW_MSG,
        {"sender": "me", "text": "自己发过的消息", "room_id": "ROOM01", "message_id": 12},
        1234,
    )
    window._chat.add_message.assert_not_called()


def test_message_ttl_button_sends_room_setting(app):
    window = _make_window_stub()
    window._chat.current_room_id = "ROOM01"

    MainWindow._on_message_ttl_requested(window, 365 * 24 * 60 * 60)

    window._bridge.send_frame.assert_called_once_with(
        T.SET_MESSAGE_TTL,
        scope_type="room",
        scope_id="ROOM01",
        ttl_seconds=365 * 24 * 60 * 60,
    )


def test_message_ttl_button_sends_dm_setting(app):
    window = _make_window_stub()
    window._chat.current_room_id = "@bob"
    window._dms = {"@bob": "bob"}
    window._secure_sessions = MagicMock()
    window._secure_sessions.dm_scope_id.return_value = "dm-scope"

    MainWindow._on_message_ttl_requested(window, 0)

    window._bridge.send_frame.assert_called_once_with(
        T.SET_MESSAGE_TTL,
        scope_type="dm",
        scope_id="dm-scope",
        to="bob",
        ttl_seconds=0,
    )


def test_ttl_menu_button_values_and_current_item(app, monkeypatch):
    button = TTLMenuButton()
    button.set_policy("room", "ROOM01", TTL_VALUES["month"], True)
    captured = {}

    def fake_exec(menu, *_args, **_kwargs):
        captured["actions"] = menu.actions()
        return None

    monkeypatch.setattr(QMenu, "exec", fake_exec)
    button._open_menu()

    actions = captured["actions"]
    assert [action.text() for action in actions] == ["一天", "一周", "一个月", "一年", "永久"]
    assert [action.isChecked() for action in actions] == [False, False, True, False, False]
    assert "一个月" in button.toolTip()


def test_conversation_unread_badge_and_activity_moves_row_to_top(app):
    panel = ConvPanel()
    panel.upsert_room("ROOM01", "一号房", "alice", 1, False)
    panel.upsert_room("ROOM02", "二号房", "bob", 1, False)

    panel.increment_unread("ROOM01")

    assert panel._unread["ROOM01"] == 1
    assert panel._rows["ROOM01"]._unread_badge.text() == "1"
    assert panel._list_lay.itemAt(0).widget() is panel._rows["ROOM01"]

    panel.set_active("ROOM01")

    assert panel._unread["ROOM01"] == 0
    assert panel._rows["ROOM01"]._unread_badge.isVisible() is False


def test_ttl_menu_button_opens_above_button(app, monkeypatch):
    button = TTLMenuButton()
    captured = {}

    def fake_exec(menu, pos, *_args, **_kwargs):
        captured["pos"] = pos
        captured["menu_height"] = menu.sizeHint().height()
        return None

    monkeypatch.setattr(QMenu, "exec", fake_exec)
    button._open_menu()

    button_top = button.mapToGlobal(button.rect().topLeft()).y()
    assert captured["pos"].y() <= button_top - captured["menu_height"]


def test_popup_above_global_pos_offsets_menu_by_its_height(app, monkeypatch):
    menu = QMenu()
    menu.addAction("复制")
    anchor = QPoint(120, 240)
    captured = {}

    def fake_exec(self, pos, *_args, **_kwargs):
        captured["pos"] = pos
        captured["height"] = self.sizeHint().height()
        return None

    monkeypatch.setattr(QMenu, "exec", fake_exec)
    popup_above_global_pos(menu, anchor)

    assert captured["pos"].x() == anchor.x()
    assert captured["pos"].y() == anchor.y() - captured["height"]


def test_room_click_reprompts_when_cached_password_is_wrong(app):
    window = _make_window_stub()
    metadata = create_room_access_metadata("ROOM01", "正确密码")
    window._rooms = {
        "ROOM01": {
            "name": "旧房间",
            "locked": True,
            "metadata": dict(metadata),
            "password": "旧密码",
        }
    }
    window._chat.current_room_id = ""

    with patch.object(MainWindow, "_prompt_room_password", return_value="正确密码") as prompt, \
         patch.object(QMessageBox, "warning") as warning:
        MainWindow._on_room_selected(window, "ROOM01")

    prompt.assert_called_once()
    warning.assert_not_called()
    window._bridge.send_frame.assert_called_once_with(
        T.JOIN_ROOM,
        room_id="ROOM01",
        access_token=metadata.access_token,
    )
    assert window._rooms["ROOM01"]["password"] == "正确密码"


def test_room_click_uses_empty_password_before_prompting(app):
    window = _make_window_stub()
    metadata = create_room_access_metadata("ROOM01", "")
    window._rooms = {
        "ROOM01": {
            "name": "公开房间",
            "locked": True,
            "metadata": dict(metadata),
            "password": "",
        }
    }
    window._chat.current_room_id = ""

    with patch.object(MainWindow, "_prompt_room_password") as prompt, \
         patch.object(QMessageBox, "warning") as warning:
        MainWindow._on_room_selected(window, "ROOM01")

    prompt.assert_not_called()
    warning.assert_not_called()
    window._bridge.send_frame.assert_called_once_with(
        T.JOIN_ROOM,
        room_id="ROOM01",
        access_token=metadata.access_token,
    )


def test_room_click_does_not_leave_current_room_before_token_is_valid(app):
    window = _make_window_stub()
    metadata = create_room_access_metadata("ROOM01", "正确密码")
    window._rooms = {
        "ROOM01": {
            "name": "旧房间",
            "locked": True,
            "metadata": dict(metadata),
            "password": "错误密码",
        }
    }
    window._chat.current_room_id = "CURRENT"
    window._server_room_id = "CURRENT"

    with patch.object(MainWindow, "_prompt_room_password", return_value=None), \
         patch.object(QMessageBox, "warning"):
        MainWindow._on_room_selected(window, "ROOM01")

    window._bridge.send_frame.assert_not_called()
    assert window._implicit_leave is False


def test_room_search_joins_empty_password_room_without_prompt(app):
    metadata = create_room_access_metadata("ROOM01", "")
    dialog = RoomSearchDialog(
        {
            "ROOM01": {
                "name": "公开房间",
                "locked": True,
                "metadata": dict(metadata),
                "members": [],
                "creator": "alice",
            }
        },
        "bob",
    )
    emitted = []
    dialog.join_requested.connect(lambda room_id, password: emitted.append((room_id, password)))

    with patch("PyQt6.QtWidgets.QInputDialog.getText") as get_text:
        dialog._on_join("ROOM01", True)

    get_text.assert_not_called()
    assert emitted == [("ROOM01", "")]


def test_start_file_send_rejects_files_larger_than_50mb(app, tmp_path):
    window = _make_window_stub()
    window._chat.current_room_id = "ROOM01"
    window._bridge.is_connected = MagicMock(return_value=True)
    window._room_file_senders = {}
    window._ft_cards = {}
    big_file = tmp_path / "big.bin"
    big_file.touch()
    os.truncate(big_file, 51 * 1024 * 1024)

    with patch.object(QMessageBox, "warning") as warning:
        MainWindow._start_file_send(window, str(pathlib.Path(big_file)))

    warning.assert_called_once_with(window, "文件过大", "文件大小不能超过 50 MB。")
    window._bridge.send_frame.assert_not_called()


def test_webrtc_offer_dispatches_to_transfer(app):
    window = _make_window_stub()
    payload = {"from": "alice", "session_id": "rtc-1", "sdp": {"type": "offer", "sdp": "v=0"}}

    MainWindow._dispatch_frame(window, T.WEBRTC_OFFER, payload, 0.0)

    window._webrtc_transfer.handle_offer.assert_awaited_once_with(payload)


def test_webrtc_answer_ice_and_close_dispatch_to_transfer(app):
    window = _make_window_stub()
    answer = {"from": "alice", "session_id": "rtc-2", "sdp": {"type": "answer", "sdp": "v=0"}}
    ice = {"from": "alice", "session_id": "rtc-2", "candidate": {"candidate": "candidate:1"}}
    close = {"from": "alice", "session_id": "rtc-2"}

    MainWindow._dispatch_frame(window, T.WEBRTC_ANSWER, answer, 0.0)
    MainWindow._dispatch_frame(window, T.WEBRTC_ICE, ice, 0.0)
    MainWindow._dispatch_frame(window, T.WEBRTC_CLOSE, close, 0.0)

    window._webrtc_transfer.handle_answer.assert_awaited_once_with(answer)
    window._webrtc_transfer.handle_ice.assert_awaited_once_with(ice)
    window._webrtc_transfer.close.assert_awaited_once_with("rtc-2")


def test_webrtc_close_marks_transfer_card_closed(app):
    window = _make_window_stub()
    card = MagicMock()
    window._ft_cards = {"rtc-2": card}
    window._webrtc_file_pending = {"rtc-2": {"peer": "alice"}}

    MainWindow._dispatch_frame(window, T.WEBRTC_CLOSE, {"from": "alice", "session_id": "rtc-2"}, 0.0)

    card.set_error.assert_called_once_with("对端已关闭传输")
    assert "rtc-2" not in window._ft_cards
    assert "rtc-2" not in window._webrtc_file_pending


def test_webrtc_file_received_adds_file_to_panel(app, tmp_path):
    window = _make_window_stub()
    window._dms = {}
    save_path = tmp_path / "received.bin"
    save_path.write_bytes(b"data")
    meta = {
        "from_user": "alice",
        "filename": "received.bin",
        "size": 4,
    }

    with patch("gui.window.FileCard") as file_card:
        file_card.return_value = MagicMock()
        MainWindow._on_webrtc_file_received(window, save_path, meta)

    window._files_panel.add_file.assert_called_once_with(
        "received.bin", "alice", "WebRTC", 4, str(save_path)
    )


def test_webrtc_file_sent_marks_card_done_and_adds_file(app, tmp_path):
    window = _make_window_stub()
    window._username = "me"
    source_path = tmp_path / "sent.bin"
    source_path.write_bytes(b"data")
    card = MagicMock()
    window._ft_cards = {"tid1": card}
    meta = {
        "to_user": "bob",
        "transfer_id": "tid1",
        "filename": "sent.bin",
        "size": 4,
    }

    MainWindow._on_webrtc_file_sent(window, source_path, meta)

    card.set_done.assert_called_once_with()
    window._files_panel.add_file.assert_called_once_with(
        "sent.bin", "me", "@ bob", 4, str(source_path)
    )
    assert "tid1" not in window._ft_cards
