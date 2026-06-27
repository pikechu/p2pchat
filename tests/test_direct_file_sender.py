import hashlib
import os
import pathlib
import sys
import tempfile
import types
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

pytest.importorskip("PyQt6")

voice_call_stub = types.ModuleType("voice_call")
voice_call_stub.VoiceCall = object
voice_call_stub.CallState = object
sys.modules.setdefault("voice_call", voice_call_stub)

from gui.window import MainWindow
from file_transfer import CHUNK_SIZE, DirectFileSender, FileTransferManager
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
    return window


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
    card.set_done.assert_called_once_with(save_path=str(path))
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
