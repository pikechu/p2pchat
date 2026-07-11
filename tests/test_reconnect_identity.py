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

from PyQt6.QtCore import QCoreApplication
from PyQt6.QtWidgets import QMessageBox

from gui.window import MainWindow
from protocol import T


@pytest.fixture(scope="module")
def app():
    return QCoreApplication.instance() or QCoreApplication(sys.argv)


def _make_window_stub():
    window = MainWindow.__new__(MainWindow)
    window._voice_call = MagicMock()
    window._voice_call.state.name = "IDLE"
    window._bridge = MagicMock()
    window._bridge.send_frame = MagicMock(return_value=True)
    window._rooms = {}
    window._reconnect_room_id = ""
    window._server_room_id = ""
    window._identified = True
    window._conv = MagicMock()
    window._chat = MagicMock()
    window._files_panel = MagicMock()
    window._send_avatar = MagicMock()
    window._webrtc_file_pending = {}
    window._webrtc_transfer = MagicMock()
    window._webrtc_transfer.handle_offer = AsyncMock()
    window._webrtc_transfer.handle_answer = AsyncMock()
    window._webrtc_transfer.handle_ice = AsyncMock()
    window._webrtc_transfer.close = AsyncMock()
    return window


def test_on_connected_only_sends_set_name(app):
    window = _make_window_stub()
    window._username = "pp"

    with patch.object(MainWindow, "setWindowTitle"):
        MainWindow._on_connected(window)

    assert window._identified is False
    window._bridge.send_frame.assert_called_once_with(T.SET_NAME, name="pp")


def test_name_set_system_unblocks_followup_requests(app):
    window = _make_window_stub()
    window._username = "pp"
    window._identified = False
    window._reconnect_room_id = "ROOM01"
    window._rooms = {"ROOM01": {"_password": "pw"}}

    with patch("gui.window.derive_key", return_value="derived-key"):
        MainWindow._dispatch_frame(window, T.SYSTEM, {"message": "Name set to 'pp'"}, 0.0)

    assert window._identified is True
    window._send_avatar.assert_called_once()
    assert window._bridge.send_frame.call_args_list[0].args == (T.LIST_ROOMS,)
    assert window._bridge.send_frame.call_args_list[1].args == (T.JOIN_ROOM,)
    assert window._bridge.send_frame.call_args_list[1].kwargs == {"room_id": "ROOM01", "password": "pw"}


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
