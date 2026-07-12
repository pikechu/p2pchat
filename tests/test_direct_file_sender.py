import hashlib
import logging
import os
import pathlib
import sys
import tempfile
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

pytest.importorskip("PyQt6")

sounddevice_stub = types.ModuleType("sounddevice")
sounddevice_stub.InputStream = object
sounddevice_stub.OutputStream = object
sys.modules.setdefault("sounddevice", sounddevice_stub)

from gui.window import MainWindow
from file_transfer import CHUNK_SIZE, DirectFileSender, EncryptedFileSender, FileTransferManager
from protocol import T, unpack


def _make_window_stub(tmp_path: pathlib.Path):
    window = MainWindow.__new__(MainWindow)
    window._bridge = MagicMock()
    window._bridge.is_connected = MagicMock(return_value=True)
    window._bridge.send_frame = MagicMock(return_value=True)
    window._bridge.send_raw_frame = MagicMock(return_value=True)
    window._bridge._queue = None
    window._ft_manager = FileTransferManager(downloads_dir=tmp_path)
    window._ft_cards = {}
    window._direct_file_senders = {}
    window._room_file_senders = {}
    window._dms = {}
    window._theme = "light"
    window._conv = MagicMock()
    window._chat = MagicMock()
    window._files_panel = MagicMock()
    window._webrtc_transfer = MagicMock()
    window._webrtc_transfer.start_offer = AsyncMock()
    window._webrtc_transfer.close = AsyncMock()
    window._webrtc_file_pending = {}
    window._secure_sessions = MagicMock()
    window._secure_sessions.file_key = MagicMock(return_value=(b"D" * 32, "dm-scope"))
    window._encrypted_file_receivers = {}
    window._username = "me"
    return window


def _encrypted_offer(tmp_path: pathlib.Path, transfer_id: str, filename: str, data: bytes):
    path = tmp_path / filename
    path.write_bytes(data)
    sender = EncryptedFileSender(
        path, b"D" * 32, transfer_id=transfer_id, scope_type="dm",
        scope_id="dm-scope", sender="bob", recipient="me",
    )
    return sender.offer_payload()


def test_direct_file_accept_streams_file_without_chunk_buffer(tmp_path):
    window = _make_window_stub(tmp_path)
    path = tmp_path / "send.bin"
    data = b"A" * (CHUNK_SIZE + 123)
    path.write_bytes(data)

    tid = "direct1"
    window._ft_manager.outgoing[tid] = {
        "to": "bob",
        "path": path,
        "sender": DirectFileSender(path),
    }
    card = MagicMock()
    window._ft_cards[tid] = card

    with patch("gui.window.QTimer.singleShot", side_effect=lambda _ms, fn: fn()):
        MainWindow._on_file_accept(window, {"transfer_id": tid})

    assert window._bridge.send_raw_frame.call_count == 2
    first_payload = unpack(window._bridge.send_raw_frame.call_args_list[0].args[0])
    second_payload = unpack(window._bridge.send_raw_frame.call_args_list[1].args[0])
    assert first_payload["type"] == T.FILE_CHUNK
    assert second_payload["type"] == T.FILE_CHUNK
    assert first_payload["payload"]["index"] == 0
    assert second_payload["payload"]["index"] == 1
    window._bridge.send_frame.assert_called_once_with(
        T.FILE_DONE,
        to="bob",
        transfer_id=tid,
        sha256=hashlib.sha256(data).hexdigest(),
    )
    assert card.set_progress.call_count == 2
    card.set_done.assert_called_once_with()
    assert tid not in window._ft_manager.outgoing
    assert tid not in window._direct_file_senders


def test_direct_file_sender_marks_error_when_socket_write_fails(tmp_path):
    window = _make_window_stub(tmp_path)
    path = tmp_path / "send.bin"
    path.write_bytes(b"A" * 32)

    tid = "direct2"
    window._ft_manager.outgoing[tid] = {
        "to": "bob",
        "path": path,
        "sender": DirectFileSender(path),
    }
    card = MagicMock()
    window._ft_cards[tid] = card
    window._bridge.send_raw_frame.return_value = False

    with patch("gui.window.QTimer.singleShot", side_effect=lambda _ms, fn: fn()):
        MainWindow._on_file_accept(window, {"transfer_id": tid})

    card.set_error.assert_called_once_with("传输中断")
    assert tid not in window._ft_manager.outgoing
    assert tid not in window._direct_file_senders


def test_start_file_send_in_dm_prefers_webrtc(tmp_path):
    window = _make_window_stub(tmp_path)
    window._chat.current_room_id = "@bob"
    window._dms = {"@bob": "bob"}
    path = tmp_path / "send.bin"
    path.write_bytes(b"abc")

    with patch("gui.window.FileCard") as file_card, \
         patch("gui.window.QTimer.singleShot") as single_shot, \
         patch.object(MainWindow, "_run_webrtc_task",
                      side_effect=lambda coro, **_kwargs: __import__("asyncio").run(coro)):
        file_card.return_value = MagicMock()
        MainWindow._start_file_send(window, str(path))

    single_shot.assert_called_once()
    window._webrtc_transfer.start_offer.assert_awaited_once()
    args = window._webrtc_transfer.start_offer.await_args.args
    assert args[0] == "bob"
    assert args[1] == path
    window._bridge.send_frame.assert_not_called()


def test_start_file_send_in_dm_falls_back_to_relay_when_webrtc_fails(tmp_path):
    window = _make_window_stub(tmp_path)
    window._chat.current_room_id = "@bob"
    window._dms = {"@bob": "bob"}
    window._webrtc_transfer.start_offer = AsyncMock(side_effect=RuntimeError("no aiortc"))
    path = tmp_path / "send.bin"
    path.write_bytes(b"abc")

    with patch("gui.window.FileCard") as file_card, \
         patch.object(MainWindow, "_run_webrtc_task",
                      side_effect=lambda coro, **_kwargs: __import__("asyncio").run(coro)):
        card = MagicMock()
        file_card.return_value = card
        MainWindow._start_file_send(window, str(path))

    file_card.assert_called_once()
    window._chat.add_file_card.assert_called_once_with(card)
    window._bridge.send_frame.assert_called_once()
    assert window._bridge.send_frame.call_args.args[0] == T.FILE_OFFER
    assert window._bridge.send_frame.call_args.kwargs["to"] == "bob"
    tid = window._bridge.send_frame.call_args.kwargs["transfer_id"]
    assert window._ft_manager.outgoing[tid]["to"] == "bob"
    assert "sender" in window._ft_manager.outgoing[tid]


def test_pending_webrtc_file_falls_back_to_relay(tmp_path):
    window = _make_window_stub(tmp_path)
    path = tmp_path / "send.bin"
    path.write_bytes(b"abc")
    card = MagicMock()
    window._ft_cards["tid1"] = card
    window._webrtc_file_pending["tid1"] = {"peer": "bob", "path": path}

    with patch("gui.window.FileCard") as file_card, \
         patch.object(MainWindow, "_run_webrtc_task",
                      side_effect=lambda coro, **_kwargs: __import__("asyncio").run(coro)):
        file_card.return_value = MagicMock()
        MainWindow._fallback_webrtc_file_if_pending(window, "tid1")

    window._webrtc_transfer.close.assert_awaited_once_with("tid1")
    window._bridge.send_frame.assert_called_once()
    assert window._bridge.send_frame.call_args.args[0] == T.FILE_OFFER
    assert window._bridge.send_frame.call_args.kwargs["to"] == "bob"
    file_card.assert_not_called()
    card.set_error.assert_not_called()
    assert window._ft_cards["tid1"] is card
    assert "tid1" not in window._webrtc_file_pending


def test_opened_webrtc_file_does_not_fallback(tmp_path):
    window = _make_window_stub(tmp_path)
    path = tmp_path / "send.bin"
    path.write_bytes(b"abc")
    window._webrtc_file_pending["tid1"] = {"peer": "bob", "path": path}

    MainWindow._on_webrtc_channel_open(window, {"session_id": "tid1"})
    MainWindow._fallback_webrtc_file_if_pending(window, "tid1")

    window._bridge.send_frame.assert_not_called()


def test_cancel_pending_webrtc_file_notifies_peer_and_marks_error(tmp_path):
    window = _make_window_stub(tmp_path)
    path = tmp_path / "send.bin"
    path.write_bytes(b"abc")
    card = MagicMock()
    window._ft_cards["tid1"] = card
    window._webrtc_file_pending["tid1"] = {"peer": "bob", "path": path}

    with patch.object(MainWindow, "_run_webrtc_task",
                      side_effect=lambda coro, **_kwargs: __import__("asyncio").run(coro)):
        MainWindow._cancel_transfer(window, "tid1")

    window._bridge.send_frame.assert_called_once_with(
        T.WEBRTC_CLOSE, to="bob", session_id="tid1"
    )
    window._webrtc_transfer.close.assert_awaited_once_with("tid1")
    card.set_error.assert_called_once_with("已取消")
    assert "tid1" not in window._webrtc_file_pending


def test_webrtc_progress_updates_file_card(tmp_path):
    window = _make_window_stub(tmp_path)
    card = MagicMock()
    window._ft_cards["tid1"] = card

    MainWindow._on_webrtc_file_progress(window, {
        "transfer_id": "tid1",
        "progress": 42,
    })

    card.set_progress.assert_called_once_with(42)


def test_dm_webrtc_flow_logs_fallback_and_completion(tmp_path, caplog):
    window = _make_window_stub(tmp_path)
    window._username = "me"
    path = tmp_path / "send.bin"
    path.write_bytes(b"abc")
    card = MagicMock()
    window._ft_cards["tid1"] = card
    window._webrtc_file_pending["tid1"] = {"peer": "bob", "path": path}

    with patch("gui.window.FileCard") as file_card, \
         patch.object(MainWindow, "_run_webrtc_task",
                      side_effect=lambda coro, **_kwargs: __import__("asyncio").run(coro)), \
         caplog.at_level(logging.INFO, logger="gui"):
        file_card.return_value = MagicMock()
        MainWindow._fallback_webrtc_file_if_pending(window, "tid1")
        MainWindow._on_webrtc_file_sent(window, path, {
            "to_user": "bob",
            "transfer_id": "tid2",
            "filename": "send.bin",
            "size": 3,
        })

    messages = [record.getMessage() for record in caplog.records]
    assert any("WEBRTC file fallback session=tid1 peer=bob filename=send.bin reason=timeout" in msg
               for msg in messages)
    assert any("WEBRTC file sent session=tid2 peer=bob filename=send.bin size=3" in msg
               for msg in messages)


def test_incoming_direct_file_offer_targets_sender_dm(tmp_path):
    window = _make_window_stub(tmp_path)
    window._username = "me"
    window._chat.current_room_id = "ROOM01"
    card = MagicMock()

    with patch("gui.window.FileCard", return_value=card):
        offer = _encrypted_offer(tmp_path, "relay1", "doc.txt", b"abc")
        MainWindow._on_file_offer(window, {
            "from": "bob",
            "transfer_id": "relay1",
            **offer,
        })

    assert window._dms["@bob"] == "bob"
    window.__dict__["_conv"].upsert_room.assert_called_once_with("@bob", "@ bob", "bob", 0, False)
    window.__dict__["_chat"].add_file_card_to_room.assert_called_once_with("@bob", card)
    window.__dict__["_chat"].add_file_card.assert_not_called()
    window.__dict__["_conv"].increment_unread.assert_called_once_with("@bob")
    window._bridge.send_frame.assert_called_once_with(T.FILE_ACCEPT, to="bob", transfer_id="relay1")


def test_webrtc_file_received_targets_sender_dm(tmp_path):
    window = _make_window_stub(tmp_path)
    window._username = "me"
    save_path = tmp_path / "received.bin"
    save_path.write_bytes(b"data")
    card = MagicMock()

    with patch("gui.window.FileCard", return_value=card):
        MainWindow._on_webrtc_file_received(window, save_path, {
            "from_user": "alice",
            "transfer_id": "rtc1",
            "filename": "received.bin",
            "size": 4,
        })

    assert window._dms["@alice"] == "alice"
    window.__dict__["_chat"].add_file_card_to_room.assert_called_once_with("@alice", card)
    window.__dict__["_chat"].add_file_card.assert_not_called()
    window.__dict__["_conv"].increment_unread.assert_called_once_with("@alice")
    card.set_done.assert_called_once_with(save_path=str(save_path))


def test_webrtc_file_received_in_active_dm_does_not_increment_unread(tmp_path):
    window = _make_window_stub(tmp_path)
    window._username = "me"
    window._chat.current_room_id = "@alice"
    save_path = tmp_path / "received.bin"
    save_path.write_bytes(b"data")
    card = MagicMock()

    with patch("gui.window.FileCard", return_value=card):
        MainWindow._on_webrtc_file_received(window, save_path, {
            "from_user": "alice",
            "transfer_id": "rtc1",
            "filename": "received.bin",
            "size": 4,
        })

    window.__dict__["_conv"].increment_unread.assert_not_called()


def test_unknown_webrtc_offer_error_falls_back_and_disables_webrtc(tmp_path):
    window = _make_window_stub(tmp_path)
    path = tmp_path / "send.bin"
    path.write_bytes(b"abc")
    card = MagicMock()
    window._rooms = {}
    window._identified = True
    window._webrtc_supported = True
    window._webrtc_file_pending["tid1"] = {"peer": "bob", "path": path}
    window._ft_cards["tid1"] = card

    with patch("gui.window.QMessageBox.warning") as warning, \
         patch.object(MainWindow, "_run_webrtc_task",
                      side_effect=lambda coro, **_kwargs: __import__("asyncio").run(coro)):
        MainWindow._dispatch_frame(window, T.ERROR, {"message": "Unknown type 'WEBRTC_OFFER'"}, 0.0)

    warning.assert_not_called()
    assert window._webrtc_supported is False
    window._bridge.send_frame.assert_called_once()
    assert window._bridge.send_frame.call_args.args[0] == T.FILE_OFFER
    assert window._bridge.send_frame.call_args.kwargs["to"] == "bob"
    assert window._ft_cards["tid1"] is card
    assert "tid1" not in window._webrtc_file_pending


def test_webrtc_disabled_dm_send_goes_directly_to_relay(tmp_path):
    window = _make_window_stub(tmp_path)
    window._chat.current_room_id = "@bob"
    window._dms = {"@bob": "bob"}
    window._webrtc_supported = False
    path = tmp_path / "send.bin"
    path.write_bytes(b"abc")

    with patch("gui.window.FileCard") as file_card:
        card = MagicMock()
        file_card.return_value = card
        MainWindow._start_file_send(window, str(path))

    window._webrtc_transfer.start_offer.assert_not_called()
    window._bridge.send_frame.assert_called_once()
    assert window._bridge.send_frame.call_args.args[0] == T.FILE_OFFER
    window._chat.add_file_card.assert_called_once_with(card)
