import os
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

pytest.importorskip("PyQt6")

voice_call_stub = types.ModuleType("voice_call")
voice_call_stub.VoiceCall = object
voice_call_stub.CallState = object
sys.modules.setdefault("voice_call", voice_call_stub)

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
    window._send_avatar = MagicMock()
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
