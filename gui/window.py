"""Main application window — Beam P2P Chat desktop client."""

import asyncio
import json
import logging
import logging.handlers
import os
import pathlib
import sys
import time
import uuid
import base64
import hashlib
from datetime import datetime

_log = logging.getLogger("gui")
if not _log.handlers:
    _log_dir = pathlib.Path.home() / ".beamchat"
    _log_dir.mkdir(parents=True, exist_ok=True)
    _fh = logging.handlers.RotatingFileHandler(
        _log_dir / "gui_client.log", maxBytes=5 * 1024 * 1024, backupCount=2, encoding="utf-8"
    )
    _fh.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s",
                                       datefmt="%Y-%m-%d %H:%M:%S"))
    _log.addHandler(_fh)
    _log.setLevel(logging.DEBUG)

from file_transfer import CHUNK_SIZE, DirectFileSender, FileTransferManager, RoomFileSender, guess_mime
from ice_config import load_ice_servers
from webrtc_transfer import WebRTCTransfer

from PyQt6.QtCore import Qt, QSize, QTimer, QEvent, QUrl, pyqtSlot, pyqtSignal
from PyQt6.QtGui import (
    QColor, QIcon, QPainter, QPainterPath, QLinearGradient, QBrush,
    QAction, QPixmap,
)
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QGridLayout, QLabel,
    QPushButton, QScrollArea, QLineEdit, QTextEdit,
    QDialog, QDialogButtonBox, QFormLayout, QSizePolicy,
    QFrame, QMessageBox, QMenu, QToolButton, QApplication,
    QCheckBox, QComboBox, QStackedWidget, QListWidget, QListWidgetItem,
)

from protocol import T, pack, unpack
from crypto import derive_key, encrypt, decrypt
from .bridge import WSBridge
from .theme import make_qss, TOKENS
from .widgets import (
    Avatar, StatusDot, BubbleWidget, SysMsgWidget,
    DayMarkWidget, ConvRowWidget, TypingWidget, EmojiPanel,
    FileCard, ImageCard, VideoCard,
)
from voice_call import VoiceCall, CallState
from .call_widget import CallWidget, IncomingCallDialog

WEBRTC_FILE_FALLBACK_MS = 5000


# ── Helpers ───────────────────────────────────────────────────────────────────

def _lbl(text: str, obj: str, parent=None) -> QLabel:
    w = QLabel(text, parent)
    w.setObjectName(obj)
    return w


def _btn(text: str, obj: str, parent=None) -> QPushButton:
    w = QPushButton(text, parent)
    w.setObjectName(obj)
    w.setCursor(Qt.CursorShape.PointingHandCursor)
    return w


# ── Join / Create room dialog ─────────────────────────────────────────────────

class RoomDialog(QDialog):
    def __init__(self, mode: str = "join", parent=None):
        super().__init__(parent)
        self._mode = mode
        self.setObjectName("Dialog")
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setModal(True)
        self.setMinimumWidth(380)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(16)

        title = "Join Room" if mode == "join" else "Create Room"
        lay.addWidget(_lbl(title, "DialogTitle"))

        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)

        if mode == "create":
            self._name = QLineEdit()
            self._name.setObjectName("FormInput")
            self._name.setPlaceholderText("e.g.  core-devs")
            form.addRow(_lbl("Room name", "FormLabel"), self._name)
        else:
            self._room_id = QLineEdit()
            self._room_id.setObjectName("FormInput")
            self._room_id.setPlaceholderText("6-char room ID")
            self._room_id.setMaxLength(6)
            form.addRow(_lbl("Room ID", "FormLabel"), self._room_id)

        self._password = QLineEdit()
        self._password.setObjectName("FormInput")
        self._password.setEchoMode(QLineEdit.EchoMode.Password)
        self._password.setPlaceholderText("optional — enables E2E encryption")
        form.addRow(_lbl("Password", "FormLabel"), self._password)
        lay.addLayout(form)

        btns = QHBoxLayout()
        btns.setSpacing(8)
        cancel = _btn("Cancel", "BtnGhost")
        cancel.clicked.connect(self.reject)
        ok = _btn(title, "BtnPrimary")
        ok.clicked.connect(self.accept)
        btns.addWidget(cancel)
        btns.addStretch()
        btns.addWidget(ok)
        lay.addLayout(btns)

    def values(self) -> dict:
        if self._mode == "create":
            return {"name": self._name.text().strip(),
                    "password": self._password.text()}
        return {"room_id": self._room_id.text().strip().upper(),
                "password": self._password.text()}


# ── Settings dialog ───────────────────────────────────────────────────────────

class SettingsDialog(QDialog):
    def __init__(self, server_url: str, username: str, theme: str,
                 download_dir: str = "", close_pref: str | None = None,
                 ice_servers: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("Dialog")
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setModal(True)
        self.setMinimumWidth(420)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(16)

        lay.addWidget(_lbl("Settings", "DialogTitle"))

        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)

        self._url = QLineEdit(server_url)
        self._url.setObjectName("FormInput")
        form.addRow(_lbl("Server URL", "FormLabel"), self._url)

        self._user = QLineEdit(username)
        self._user.setObjectName("FormInput")
        self._user.setPlaceholderText("Your display name")
        form.addRow(_lbl("Username", "FormLabel"), self._user)

        self._theme = QComboBox()
        self._theme.setObjectName("FormInput")
        self._theme.addItems(["light", "dark"])
        self._theme.setCurrentText(theme)
        form.addRow(_lbl("Theme", "FormLabel"), self._theme)

        self._close_combo = QComboBox()
        self._close_combo.setObjectName("FormInput")
        self._close_combo.addItems(["每次询问", "最小化到托盘", "退出程序"])
        _close_map = {None: "每次询问", "tray": "最小化到托盘", "quit": "退出程序"}
        self._close_combo.setCurrentText(_close_map.get(close_pref, "每次询问"))
        form.addRow(_lbl("X 关闭按钮", "FormLabel"), self._close_combo)

        self._ice_servers = QTextEdit()
        self._ice_servers.setObjectName("FormInput")
        self._ice_servers.setPlainText(ice_servers)
        self._ice_servers.setPlaceholderText(
            'stun:stun.l.google.com:19302 或 JSON 列表，例如 [{"urls":["turn:host:3478"],"username":"u","credential":"p"}]'
        )
        self._ice_servers.setFixedHeight(72)
        form.addRow(_lbl("ICE servers", "FormLabel"), self._ice_servers)

        lay.addLayout(form)

        # Download directory row
        dl_row = QHBoxLayout()
        dl_row.setSpacing(6)
        dl_row.addWidget(_lbl("下载路径", "FormLabel"))
        self._dl_lbl = QLabel(download_dir or "—")
        self._dl_lbl.setObjectName("SettingsVersion")
        self._dl_lbl.setWordWrap(False)
        self._dl_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        dl_row.addWidget(self._dl_lbl, 1)
        dl_change = _btn("更改", "BtnGhost")
        dl_change.setFixedHeight(28)
        dl_change.setMinimumWidth(52)
        dl_change.clicked.connect(self._on_change_dl)
        dl_row.addWidget(dl_change)
        lay.addLayout(dl_row)

        from version import __version__
        ver_row = QHBoxLayout()
        ver_lbl = QLabel(f"版本  v{__version__}")
        ver_lbl.setObjectName("SettingsVersion")
        ver_row.addWidget(ver_lbl)
        ver_row.addStretch()
        self._check_btn = _btn("检查更新", "BtnGhost")
        self._check_btn.setFixedHeight(28)
        self._check_btn.setMinimumWidth(96)
        self._check_btn.clicked.connect(self._on_check_update)
        self._check_status = QLabel("")
        self._check_status.setObjectName("SettingsVersion")
        ver_row.addWidget(self._check_status)
        ver_row.addWidget(self._check_btn)
        lay.addLayout(ver_row)

        btns = QHBoxLayout()
        btns.setSpacing(8)
        cancel = _btn("Cancel", "BtnGhost")
        cancel.clicked.connect(self.reject)
        ok = _btn("Save", "BtnPrimary")
        ok.clicked.connect(self.accept)
        btns.addWidget(cancel)
        btns.addStretch()
        btns.addWidget(ok)
        lay.addLayout(btns)

    def _on_check_update(self):
        self._check_btn.setEnabled(False)
        self._check_status.setText("检查中…")
        self._check_status.setStyleSheet("")
        import threading
        def _worker():
            from updater import check_update
            result = check_update(timeout=8)
            self._check_result = result
            QTimer.singleShot(0, self._on_check_done)
        threading.Thread(target=_worker, daemon=True).start()

    def _on_check_done(self):
        self._check_btn.setEnabled(True)
        ver, url, err = getattr(self, "_check_result", (None, None, None))
        if ver:
            self._check_status.setText(f"新版本 v{ver} !")
            self._check_status.setStyleSheet("color: #22c55e; font-weight: 600;")
            parent = self.parent()
            if parent and hasattr(parent, "_show_update_bar"):
                parent._update_available_ver = ver
                parent._update_available_url = url
                parent._show_update_bar(ver)
        elif err:
            self._check_status.setText(err)
            self._check_status.setStyleSheet("color: #f59e0b;")
        else:
            self._check_status.setText("已是最新")
            self._check_status.setStyleSheet("")

    def _on_change_dl(self):
        from PyQt6.QtWidgets import QFileDialog
        path = QFileDialog.getExistingDirectory(
            self, "选择下载目录", self._dl_lbl.text()
        )
        if path:
            self._dl_lbl.setText(path)

    def values(self) -> dict:
        _pref_map = {"每次询问": None, "最小化到托盘": "tray", "退出程序": "quit"}
        return {
            "server_url":   self._url.text().strip(),
            "username":     self._user.text().strip(),
            "theme":        self._theme.currentText(),
            "download_dir": self._dl_lbl.text(),
            "close_pref":   _pref_map.get(self._close_combo.currentText()),
            "ice_servers":  self._ice_servers.toPlainText().strip(),
        }


# ── Rail (left icon bar) ──────────────────────────────────────────────────────

class Rail(QWidget):
    avatar_change = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Rail")
        self.setFixedWidth(56)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 12, 8, 12)
        lay.setSpacing(4)
        lay.setAlignment(Qt.AlignmentFlag.AlignTop)

        self._btns: dict[str, QPushButton] = {}
        icons = [("💬", "chats"), ("👥", "peers"), ("📁", "files")]
        for icon, key in icons:
            btn = QPushButton(icon)
            btn.setObjectName("RailBtn")
            btn.setFixedSize(40, 40)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self._btns[key] = btn
            lay.addWidget(btn, alignment=Qt.AlignmentFlag.AlignHCenter)

        lay.addStretch()

        self._settings_btn = QPushButton("⚙")
        self._settings_btn.setObjectName("RailBtn")
        self._settings_btn.setFixedSize(40, 40)
        self._settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        lay.addWidget(self._settings_btn, alignment=Qt.AlignmentFlag.AlignHCenter)

        self._avatar = Avatar("?", 36)
        self._avatar.setCursor(Qt.CursorShape.PointingHandCursor)
        self._avatar.setToolTip("更换头像")
        self._avatar.clicked.connect(self.avatar_change)
        lay.addWidget(self._avatar, alignment=Qt.AlignmentFlag.AlignHCenter)

    def set_active(self, key: str):
        for k, btn in self._btns.items():
            btn.setProperty("active", k == key)
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def set_username(self, name: str):
        self._avatar.set_name(name)

    def set_avatar_pixmap(self, pixmap):
        self._avatar.set_pixmap(pixmap)


# ── Conversation list panel ───────────────────────────────────────────────────

class ConvPanel(QWidget):
    room_selected      = pyqtSignal(str)   # room_id
    room_right_clicked = pyqtSignal(str)   # room_id
    create_room        = pyqtSignal()
    search_rooms       = pyqtSignal()

    def __init__(self, theme: str = "light", parent=None):
        super().__init__(parent)
        self._theme = theme
        self.setObjectName("ConvPanel")
        self.setFixedWidth(300)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        header = QWidget()
        header.setObjectName("ConvHeader")
        hlay = QVBoxLayout(header)
        hlay.setContentsMargins(16, 14, 16, 10)
        hlay.setSpacing(8)

        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        title_lbl = _lbl("Chats", "ConvTitle")
        title_row.addWidget(title_lbl)
        title_row.addStretch()

        search_btn = _btn("🔍", "NewRoomBtn")
        search_btn.setFixedSize(28, 28)
        search_btn.setToolTip("搜索聊天室")
        search_btn.clicked.connect(self.search_rooms)
        title_row.addWidget(search_btn)

        new_btn = _btn("+", "NewRoomBtn")
        new_btn.setFixedSize(28, 28)
        new_btn.setToolTip("Create room")
        new_btn.clicked.connect(self.create_room)
        title_row.addWidget(new_btn)
        hlay.addLayout(title_row)

        self._search = QLineEdit()
        self._search.setObjectName("SearchBox")
        self._search.setPlaceholderText("Search…")
        self._search.textChanged.connect(self._filter)
        hlay.addWidget(self._search)

        lay.addWidget(header)

        scroll = QScrollArea()
        scroll.setObjectName("ConvScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._list_widget = QWidget()
        self._list_widget.setObjectName("ConvList")
        self._list_lay = QVBoxLayout(self._list_widget)
        self._list_lay.setContentsMargins(0, 0, 0, 0)
        self._list_lay.setSpacing(0)
        self._list_lay.addStretch()

        scroll.setWidget(self._list_widget)
        lay.addWidget(scroll)

        self._rows: dict[str, ConvRowWidget] = {}
        self._active: str | None = None
        self._unread: dict[str, int] = {}

    def upsert_room(self, room_id: str, name: str, creator: str,
                    members: int = 0, locked: bool = False,
                    unread: int = 0):
        if room_id in self._rows:
            self._rows[room_id].set_members(members)
            return
        row = ConvRowWidget(room_id, name, creator, members, locked, unread,
                            self._theme, conn_state="ok")
        row.clicked.connect(self._on_row_clicked)
        row.right_clicked.connect(self.room_right_clicked)
        self._list_lay.insertWidget(self._list_lay.count() - 1, row)
        self._rows[room_id] = row

    def update_members(self, room_id: str, count: int):
        if row := self._rows.get(room_id):
            row.set_members(count)

    def update_room_name(self, room_id: str, name: str):
        if row := self._rows.get(room_id):
            row.set_room_name(name)

    def remove_room(self, room_id: str):
        row = self._rows.pop(room_id, None)
        if row:
            self._list_lay.removeWidget(row)
            row.deleteLater()
            if self._active == room_id:
                self._active = None

    def set_active(self, room_id: str | None):
        for rid, row in self._rows.items():
            row.set_active(rid == room_id)
        if room_id:
            self._unread[room_id] = 0
            if row := self._rows.get(room_id):
                row.set_unread(0)
        self._active = room_id

    def set_preview(self, room_id: str, text: str, ts: float = 0):
        if row := self._rows.get(room_id):
            row.set_preview(text, ts)

    def increment_unread(self, room_id: str, amount: int = 1):
        count = self._unread.get(room_id, 0) + amount
        self._unread[room_id] = count
        if row := self._rows.get(room_id):
            row.set_unread(count)

    def set_conn_state(self, room_id: str, state: str):
        if row := self._rows.get(room_id):
            row.set_conn_state(state)

    def _on_row_clicked(self, room_id: str):
        self.set_active(room_id)
        self.room_selected.emit(room_id)

    def _filter(self, query: str):
        q = query.lower()
        for row in self._rows.values():
            name = row._name_lbl.text().lower()
            row.setVisible(not q or q in name)


# ── Reply bar (shown above composer input when replying) ──────────────────────

class ReplyBar(QWidget):
    cancelled = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ReplyBar")
        self.setFixedHeight(46)
        self._sender = ""
        self._text   = ""
        self._seq    = 0

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 6, 8, 6)
        lay.setSpacing(8)

        icon = QLabel("↩")
        icon.setObjectName("ReplyIcon")
        lay.addWidget(icon)

        mid = QVBoxLayout()
        mid.setSpacing(0)
        self._name_lbl = QLabel()
        self._name_lbl.setObjectName("ReplyName")
        self._text_lbl = QLabel()
        self._text_lbl.setObjectName("ReplyPreview")
        mid.addWidget(self._name_lbl)
        mid.addWidget(self._text_lbl)
        lay.addLayout(mid, 1)

        cancel_btn = QPushButton("×")
        cancel_btn.setObjectName("ReplyCancel")
        cancel_btn.setFixedSize(22, 22)
        cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel_btn.clicked.connect(self.cancelled)
        lay.addWidget(cancel_btn)

    def set_reply(self, sender: str, text: str, seq: int):
        self._sender = sender
        self._text   = text
        self._seq    = seq
        self._name_lbl.setText(sender)
        self._text_lbl.setText((text[:60] + "…") if len(text) > 60 else text)

    def data(self) -> dict:
        return {"sender": self._sender, "text": self._text, "seq": self._seq}


# ── Chat header ───────────────────────────────────────────────────────────────

class ChatHeader(QWidget):
    info_toggled = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ChatHeader")
        self.setFixedHeight(52)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 0, 16, 0)
        lay.setSpacing(10)

        self._avatar = Avatar("?", 36)
        lay.addWidget(self._avatar)

        info = QVBoxLayout()
        info.setSpacing(1)
        self._name_lbl = _lbl("", "ChatName")
        self._sub_lbl  = _lbl("", "ChatSub")
        info.addWidget(self._name_lbl)
        info.addWidget(self._sub_lbl)
        lay.addLayout(info)
        lay.addStretch()

        self._status_dot = StatusDot("offline")
        lay.addWidget(self._status_dot)

        info_btn = QPushButton("⋯")
        info_btn.setObjectName("HeaderBtn")
        info_btn.setFixedSize(32, 32)
        info_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        info_btn.clicked.connect(self.info_toggled)
        lay.addWidget(info_btn)

    def update_room(self, name: str, members: list[str], locked: bool,
                    conn_state: str = "ok", icon: str = ""):
        self._avatar.set_name(name)
        self._name_lbl.setText(("🔒 " if locked else "") + name)
        count = len(members)
        self._sub_lbl.setText(f"{count} member{'s' if count != 1 else ''}")
        self._status_dot.set_state(conn_state)

    def update_member_count(self, count: int):
        self._sub_lbl.setText(f"{count} member{'s' if count != 1 else ''}")

    def update_room_name(self, name: str, locked: bool = False):
        self._avatar.set_name(name)
        self._name_lbl.setText(("🔒 " if locked else "") + name)

    def set_conn_state(self, state: str):
        self._status_dot.set_state(state)


# ── Messages area ─────────────────────────────────────────────────────────────

class MessagesArea(QScrollArea):
    reply_requested = pyqtSignal(str, str, int)   # sender, text, seq

    def __init__(self, theme: str = "light", own_name: str = "", parent=None):
        super().__init__(parent)
        self._theme = theme
        self._own_name   = own_name
        self._own_pixmap: QPixmap | None = None
        self.setObjectName("MsgsScroll")
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._container = QWidget()
        self._container.setObjectName("MsgsContainer")
        self._lay = QVBoxLayout(self._container)
        self._lay.setContentsMargins(20, 14, 20, 8)
        self._lay.setSpacing(2)
        self._lay.addStretch()

        self.setWidget(self._container)
        self._last_sender: str | None = None
        self._last_day:    str | None = None
        self._peer_pixmaps: dict[str, QPixmap] = {}
        self._peer_avatar_widgets: dict[str, list] = {}   # sender → [Avatar, ...]

    def set_own_avatar(self, pixmap: QPixmap | None):
        self._own_pixmap = pixmap

    def set_peer_avatar(self, name: str, pixmap: QPixmap) -> None:
        self._peer_pixmaps[name] = pixmap
        for av in self._peer_avatar_widgets.get(name, []):
            av.set_pixmap(pixmap)

    def add_message(self, sender: str, text: str, ts: float,
                    outgoing: bool = False,
                    seq: int = 0,
                    quote: dict | None = None) -> BubbleWidget:
        # Day separator
        day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        if day != self._last_day:
            label = datetime.fromtimestamp(ts).strftime("%B %d, %Y")
            self._lay.insertWidget(self._lay.count() - 1,
                                   DayMarkWidget(label),
                                   alignment=Qt.AlignmentFlag.AlignHCenter)
            self._last_day = day
            self._last_sender = None

        show_av     = sender != self._last_sender
        show_sender = show_av and not outgoing
        bubble = BubbleWidget(sender, text, ts, outgoing, show_sender,
                              self._theme, seq=seq, quote=quote)
        bubble.reply_requested.connect(self.reply_requested)
        # Wrap in a row widget so heightForWidth() propagates through the layout.
        # Passing alignment= directly to insertWidget() wraps the bubble in a
        # fixed-size container using sizeHint() height, which truncates wrapped text.
        row = QWidget()
        row_lay = QHBoxLayout(row)
        row_lay.setContentsMargins(0, 0, 0, 0)
        row_lay.setSpacing(6)
        if outgoing:
            row_lay.addStretch()
            row_lay.addWidget(bubble)
            if show_av:
                own_av = Avatar(self._own_name or sender, 32)
                if self._own_pixmap:
                    own_av.set_pixmap(self._own_pixmap)
                row_lay.addWidget(own_av, 0, Qt.AlignmentFlag.AlignTop)
            else:
                ph = QWidget()
                ph.setFixedWidth(32)
                row_lay.addWidget(ph)
        else:
            if show_sender:
                av = Avatar(sender, 32)
                if sender in self._peer_pixmaps:
                    av.set_pixmap(self._peer_pixmaps[sender])
                self._peer_avatar_widgets.setdefault(sender, []).append(av)
                row_lay.addWidget(av, 0, Qt.AlignmentFlag.AlignTop)
            else:
                placeholder = QWidget()
                placeholder.setFixedWidth(32)
                row_lay.addWidget(placeholder)
            row_lay.addWidget(bubble)
            row_lay.addStretch()
        self._lay.insertWidget(self._lay.count() - 1, row)
        self._last_sender = sender
        QTimer.singleShot(50, lambda: self.verticalScrollBar().setValue(
            self.verticalScrollBar().maximum()))
        return bubble

    def add_sys_msg(self, text: str):
        self._lay.insertWidget(self._lay.count() - 1,
                               SysMsgWidget(text),
                               alignment=Qt.AlignmentFlag.AlignHCenter)
        self._last_sender = None

    def add_file_card(self, card):
        wrapper = QWidget()
        lay = QHBoxLayout(wrapper)
        lay.setContentsMargins(16, 2, 16, 2)
        if card._outgoing:
            lay.addStretch()
        lay.addWidget(card)
        if not card._outgoing:
            lay.addStretch()
        self._lay.insertWidget(self._lay.count() - 1, wrapper)
        QTimer.singleShot(50, lambda: self.verticalScrollBar().setValue(
            self.verticalScrollBar().maximum()))

    def clear(self):
        while self._lay.count() > 1:
            item = self._lay.takeAt(0)
            if w := item.widget():
                w.deleteLater()
        self._last_sender = None
        self._last_day    = None


# ── Composer ──────────────────────────────────────────────────────────────────

class Composer(QWidget):
    send_message   = pyqtSignal(str)
    typing_started = pyqtSignal()
    typing_stopped = pyqtSignal()
    emoji_toggled  = pyqtSignal()
    file_selected  = pyqtSignal(str)   # file path

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ComposerBar")
        self._reply_data: dict | None = None
        self._was_typing = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Reply bar (hidden until user triggers reply)
        self._reply_bar = ReplyBar()
        self._reply_bar.hide()
        self._reply_bar.cancelled.connect(self.clear_reply)
        outer.addWidget(self._reply_bar)

        # Inner composer row
        inner_wrap = QWidget()
        inner_wrap.setObjectName("ComposerBarInner")
        wrap_lay = QHBoxLayout(inner_wrap)
        wrap_lay.setContentsMargins(16, 10, 16, 12)
        wrap_lay.setSpacing(0)

        inner_frame = QFrame()
        inner_frame.setObjectName("ComposerInner")
        inner_lay = QHBoxLayout(inner_frame)
        inner_lay.setContentsMargins(8, 4, 4, 4)
        inner_lay.setSpacing(4)

        attach = QPushButton("📎")
        attach.setObjectName("ComposerIconBtn")
        attach.setToolTip("发送文件")
        attach.clicked.connect(self._pick_file)
        inner_lay.addWidget(attach)

        img_btn = QPushButton("🖼")
        img_btn.setObjectName("ComposerIconBtn")
        img_btn.setToolTip("发送图片")
        img_btn.clicked.connect(self._pick_image)
        inner_lay.addWidget(img_btn)

        vid_btn = QPushButton("🎬")
        vid_btn.setObjectName("ComposerIconBtn")
        vid_btn.setToolTip("发送视频")
        vid_btn.clicked.connect(self._pick_video)
        inner_lay.addWidget(vid_btn)

        self._input = QLineEdit()
        self._input.setObjectName("ComposerInput")
        self._input.setPlaceholderText("Message…")
        self._input.returnPressed.connect(self._on_send)
        self._input.textChanged.connect(self._on_text_changed)
        self._input.installEventFilter(self)
        inner_lay.addWidget(self._input)

        self._emoji_btn = QPushButton("😊")
        self._emoji_btn.setObjectName("ComposerIconBtn")
        self._emoji_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._emoji_btn.clicked.connect(self.emoji_toggled)
        inner_lay.addWidget(self._emoji_btn)

        send = QPushButton("↑")
        send.setObjectName("SendBtn")
        send.clicked.connect(self._on_send)
        inner_lay.addWidget(send)

        wrap_lay.addWidget(inner_frame)
        outer.addWidget(inner_wrap)

    def _on_text_changed(self, text: str):
        if text and not self._was_typing:
            self._was_typing = True
            self.typing_started.emit()
        elif not text and self._was_typing:
            self._was_typing = False
            self.typing_stopped.emit()

    def _on_send(self):
        text = self._input.text().strip()
        if text:
            self.send_message.emit(text)
            self._input.clear()
            self._was_typing = False

    def set_enabled(self, enabled: bool):
        self._input.setEnabled(enabled)
        self._input.setPlaceholderText(
            "Message…" if enabled else "Join a room to start chatting"
        )

    def set_reply(self, sender: str, text: str, seq: int):
        self._reply_bar.set_reply(sender, text, seq)
        self._reply_data = self._reply_bar.data()
        self._reply_bar.show()
        self._input.setFocus()

    def clear_reply(self):
        self._reply_data = None
        self._reply_bar.hide()

    @property
    def pending_reply(self) -> dict | None:
        return self._reply_data

    def insert_emoji(self, emoji: str):
        pos = self._input.cursorPosition()
        cur = self._input.text()
        self._input.setText(cur[:pos] + emoji + cur[pos:])
        self._input.setCursorPosition(pos + len(emoji))
        self._input.setFocus()

    def eventFilter(self, obj, event):
        if obj is self._input and event.type() == QEvent.Type.KeyPress:
            if (event.key() == Qt.Key.Key_V and
                    event.modifiers() & Qt.KeyboardModifier.ControlModifier):
                cb = QApplication.clipboard()
                img = cb.image()
                if not img.isNull():
                    import tempfile
                    tmp = pathlib.Path(tempfile.mktemp(suffix=".png"))
                    img.save(str(tmp))
                    self.file_selected.emit(str(tmp))
                    return True
                md = cb.mimeData()
                if md and md.hasUrls():
                    for url in md.urls():
                        if url.isLocalFile():
                            self.file_selected.emit(url.toLocalFile())
                            return True
        return super().eventFilter(obj, event)

    def _pick_file(self):
        from PyQt6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            self, "Send File", str(pathlib.Path.home()),
            "All Files (*)"
        )
        if path:
            self.file_selected.emit(path)

    def _pick_image(self):
        from PyQt6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            self, "发送图片", str(pathlib.Path.home()),
            "Images (*.png *.jpg *.jpeg *.gif *.webp)"
        )
        if path:
            self.file_selected.emit(path)

    def _pick_video(self):
        from PyQt6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            self, "发送视频", str(pathlib.Path.home()),
            "Videos (*.mp4 *.webm *.mov *.avi)"
        )
        if path:
            self.file_selected.emit(path)


# ── Empty / placeholder panel ─────────────────────────────────────────────────

class EmptyPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("EmptyPanel")
        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.setSpacing(8)
        icon = QLabel("💬")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setStyleSheet("font-size: 48px;")
        lay.addWidget(icon)
        lay.addWidget(_lbl("Select or create a room", "EmptyTitle"))
        sub = _lbl("使用 + 创建聊天室，或 🔍 搜索并加入", "EmptyDesc")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub.setWordWrap(True)
        lay.addWidget(sub)


# ── Room info panel (right sidebar) ──────────────────────────────────────────

class RoomInfoPanel(QWidget):
    rename_requested     = pyqtSignal(str, str)  # room_id, new_name
    icon_change_requested = pyqtSignal(str, str) # room_id, new_icon

    _ICON_CHOICES = ["💬", "🎮", "🎵", "📚", "🏠", "🌟", "🔥", "💡",
                     "🎯", "🚀", "🌈", "🎲", "🍀", "⚡", "🎸", "🏆"]

    def __init__(self, theme: str = "light", parent=None):
        super().__init__(parent)
        self.setObjectName("RoomInfoPanel")
        self.setFixedWidth(240)
        self._room_id  = ""
        self._is_creator = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(12)

        # Header row
        hdr = QHBoxLayout()
        title = _lbl("聊天室信息", "InfoPanelTitle")
        hdr.addWidget(title)
        hdr.addStretch()
        close_btn = _btn("×", "InfoCloseBtn")
        close_btn.setFixedSize(24, 24)
        close_btn.clicked.connect(self.hide)
        hdr.addWidget(close_btn)
        lay.addLayout(hdr)

        # Room avatar (large) + icon selector row
        av_wrap = QWidget()
        av_lay = QVBoxLayout(av_wrap)
        av_lay.setContentsMargins(0, 0, 0, 0)
        av_lay.setSpacing(4)
        av_lay.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._big_avatar = Avatar("?", 64)
        self._icon_lbl = QLabel()
        self._icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._icon_lbl.setStyleSheet("font-size: 36px;")
        self._icon_lbl.hide()
        av_lay.addWidget(self._big_avatar, alignment=Qt.AlignmentFlag.AlignHCenter)
        av_lay.addWidget(self._icon_lbl, alignment=Qt.AlignmentFlag.AlignHCenter)

        # Change icon button (creator only)
        self._change_icon_btn = _btn("更换图标", "InfoEditBtn")
        self._change_icon_btn.setFixedHeight(26)
        self._change_icon_btn.hide()
        self._change_icon_btn.clicked.connect(self._on_change_icon)
        av_lay.addWidget(self._change_icon_btn, alignment=Qt.AlignmentFlag.AlignHCenter)
        lay.addWidget(av_wrap)

        # Icon picker row (hidden until change icon clicked)
        self._icon_picker = QWidget()
        self._icon_picker.hide()
        ip_lay = QGridLayout(self._icon_picker)
        ip_lay.setContentsMargins(0, 0, 0, 0)
        ip_lay.setSpacing(4)
        for i, emoji in enumerate(self._ICON_CHOICES):
            btn = QPushButton(emoji)
            btn.setFixedSize(28, 28)
            btn.setObjectName("EmojiBtn")
            btn.clicked.connect(lambda checked, e=emoji: self._apply_icon(e))
            ip_lay.addWidget(btn, i // 4, i % 4)
        clear_btn = _btn("✕", "EmojiBtn")
        clear_btn.setFixedSize(28, 28)
        clear_btn.setToolTip("清除图标")
        clear_btn.clicked.connect(lambda: self._apply_icon(""))
        ip_lay.addWidget(clear_btn, len(self._ICON_CHOICES) // 4,
                         len(self._ICON_CHOICES) % 4)
        lay.addWidget(self._icon_picker)

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setObjectName("InfoSep")
        lay.addWidget(sep)

        # Name row
        name_row = QHBoxLayout()
        self._name_display = _lbl("", "InfoRoomName")
        self._name_display.setWordWrap(True)
        name_row.addWidget(self._name_display, 1)
        self._rename_btn = _btn("✏️", "InfoEditBtn")
        self._rename_btn.setFixedSize(26, 26)
        self._rename_btn.setToolTip("修改名称")
        self._rename_btn.hide()
        self._rename_btn.clicked.connect(self._on_rename)
        name_row.addWidget(self._rename_btn)
        lay.addLayout(name_row)

        # Creator / created-at labels
        self._creator_lbl  = _lbl("", "InfoMeta")
        self._created_lbl  = _lbl("", "InfoMeta")
        lay.addWidget(self._creator_lbl)
        lay.addWidget(self._created_lbl)

        lay.addStretch()

    def update_room(self, room_id: str, name: str, creator: str,
                    created_at: float, icon: str, is_creator: bool):
        self._room_id    = room_id
        self._is_creator = is_creator
        self._current_icon = icon

        self._big_avatar.set_name(name)
        if icon:
            self._icon_lbl.setText(icon)
            self._icon_lbl.show()
            self._big_avatar.hide()
        else:
            self._icon_lbl.hide()
            self._big_avatar.show()

        self._name_display.setText(name)
        self._creator_lbl.setText(f"创建者：{creator}")
        from datetime import datetime as _dt
        ts_str = _dt.fromtimestamp(created_at).strftime("%Y-%m-%d %H:%M") if created_at else ""
        self._created_lbl.setText(f"创建时间：{ts_str}")

        self._rename_btn.setVisible(is_creator)
        self._change_icon_btn.setVisible(is_creator)
        self._icon_picker.hide()

    def update_name(self, name: str):
        self._name_display.setText(name)
        self._big_avatar.set_name(name)

    def update_icon(self, icon: str):
        self._current_icon = icon
        if icon:
            self._icon_lbl.setText(icon)
            self._icon_lbl.show()
            self._big_avatar.hide()
        else:
            self._icon_lbl.hide()
            self._big_avatar.show()

    def _on_rename(self):
        from PyQt6.QtWidgets import QInputDialog
        new_name, ok = QInputDialog.getText(
            self, "修改聊天室名称", "新名称：",
            text=self._name_display.text()
        )
        if ok and new_name.strip():
            self.rename_requested.emit(self._room_id, new_name.strip())

    def _on_change_icon(self):
        self._icon_picker.setVisible(not self._icon_picker.isVisible())

    def _apply_icon(self, emoji: str):
        self._icon_picker.hide()
        self.icon_change_requested.emit(self._room_id, emoji)


# ── Room search dialog ────────────────────────────────────────────────────────

class RoomSearchDialog(QDialog):
    join_requested = pyqtSignal(str, str)  # room_id, password

    def __init__(self, rooms: dict, current_username: str, parent=None):
        super().__init__(parent)
        self._rooms = rooms
        self._username = current_username
        self.setObjectName("Dialog")
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setModal(True)
        self.setMinimumWidth(420)
        self.setMinimumHeight(480)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(10)

        # Title
        title_row = QHBoxLayout()
        title_lbl = _lbl("🔍  搜索聊天室", "DialogTitle")
        title_row.addWidget(title_lbl)
        title_row.addStretch()
        close_btn = _btn("×", "DialogCloseBtn")
        close_btn.setFixedSize(28, 28)
        close_btn.clicked.connect(self.reject)
        title_row.addWidget(close_btn)
        lay.addLayout(title_row)

        # Search input
        self._search = QLineEdit()
        self._search.setObjectName("SearchBox")
        self._search.setPlaceholderText("输入聊天室名称搜索（留空显示全部）…")
        self._search.textChanged.connect(self._filter)
        lay.addWidget(self._search)

        # Results list
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._results_widget = QWidget()
        self._results_lay = QVBoxLayout(self._results_widget)
        self._results_lay.setContentsMargins(0, 0, 0, 0)
        self._results_lay.setSpacing(4)
        self._results_lay.addStretch()
        scroll.setWidget(self._results_widget)
        lay.addWidget(scroll, 1)

        self._row_widgets: list[tuple[str, QWidget]] = []
        self._populate()

    def _populate(self):
        # Clear existing rows
        while self._results_lay.count() > 1:
            item = self._results_lay.takeAt(0)
            if w := item.widget():
                w.deleteLater()
        self._row_widgets.clear()

        # Show all non-DM rooms
        for rid, info in self._rooms.items():
            if rid.startswith("@"):
                continue
            row_w = self._make_room_row(rid, info)
            self._results_lay.insertWidget(self._results_lay.count() - 1, row_w)
            self._row_widgets.append((rid, row_w))

        if not self._row_widgets:
            empty = _lbl("暂无聊天室", "EmptyDesc")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._results_lay.insertWidget(0, empty)

    def _make_room_row(self, room_id: str, info: dict) -> QWidget:
        w = QWidget()
        w.setObjectName("SearchResultRow")
        lay = QHBoxLayout(w)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(8)

        av = Avatar(info.get("name", room_id), 36)
        icon = info.get("icon", "")
        if icon:
            icon_lbl = QLabel(icon)
            icon_lbl.setStyleSheet("font-size: 22px;")
            icon_lbl.setFixedSize(36, 36)
            icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lay.addWidget(icon_lbl)
        else:
            lay.addWidget(av)

        meta = QVBoxLayout()
        meta.setSpacing(2)
        name_str = ("🔒 " if info.get("locked") else "") + info.get("name", room_id)
        name_lbl = _lbl(name_str, "ConvRowName")
        members  = info.get("members", [])
        count = len(members) if isinstance(members, list) else members
        sub_lbl = _lbl(f"👥 {count}  ·  创建者: {info.get('creator', '')}", "ConvRowPreview")
        meta.addWidget(name_lbl)
        meta.addWidget(sub_lbl)
        lay.addLayout(meta, 1)

        members_set = set(members) if isinstance(members, list) else set()
        if self._username in members_set:
            status = _lbl("已加入", "InfoMeta")
            lay.addWidget(status)
        else:
            join_btn = _btn("加入", "PrimaryBtn")
            join_btn.setFixedSize(52, 30)
            join_btn.clicked.connect(lambda checked, r=room_id, lk=info.get("locked", False):
                                     self._on_join(r, lk))
            lay.addWidget(join_btn)

        w.setProperty("room_id", room_id)
        w.setProperty("room_name", info.get("name", ""))
        return w

    def _filter(self, query: str):
        q = query.strip().lower()
        for rid, row_w in self._row_widgets:
            name = (row_w.property("room_name") or "").lower()
            row_w.setVisible(not q or q in name)

    def _on_join(self, room_id: str, locked: bool):
        password = ""
        if locked:
            from PyQt6.QtWidgets import QInputDialog
            pw, ok = QInputDialog.getText(
                self, "密码保护", "请输入房间密码：",
                QLineEdit.EchoMode.Password
            )
            if not ok:
                return
            password = pw
        self.join_requested.emit(room_id, password)
        self.accept()


# ── Full chat panel ───────────────────────────────────────────────────────────

class ChatPanel(QWidget):
    reply_requested = pyqtSignal(str, str, int)   # sender, text, seq

    def __init__(self, theme: str = "light", parent=None):
        super().__init__(parent)
        self._theme      = theme
        self._room_id:   str | None = None
        self._own_name:  str = ""
        self._own_pixmap: QPixmap | None = None
        self._peer_pixmaps: dict[str, QPixmap] = {}
        self.setObjectName("ChatPanel")

        self._stack = QStackedWidget(self)
        main_lay = QVBoxLayout(self)
        main_lay.setContentsMargins(0, 0, 0, 0)
        main_lay.setSpacing(0)
        main_lay.addWidget(self._stack)

        # Empty page
        self._empty = EmptyPanel()
        self._stack.addWidget(self._empty)

        # Chat page
        self._chat_widget = QWidget()
        self._chat_widget.setObjectName("ChatPanel")
        chat_lay = QVBoxLayout(self._chat_widget)
        chat_lay.setContentsMargins(0, 0, 0, 0)
        chat_lay.setSpacing(0)

        self._header  = ChatHeader()

        # Per-room message areas — keyed by room_id
        self._msgs_by_room: dict[str, MessagesArea] = {}
        self._msgs_stack = QStackedWidget()
        self._msgs_placeholder = QWidget()   # shown when no room is active
        self._msgs_stack.addWidget(self._msgs_placeholder)

        # Typing indicator (between messages and emoji panel)
        self._typing = TypingWidget(theme=theme)

        # Emoji panel (toggleable)
        self._emoji_panel = EmojiPanel(theme=theme)
        self._emoji_panel.hide()

        self._composer = Composer()
        self._composer.set_enabled(False)
        self._composer.emoji_toggled.connect(self._toggle_emoji)
        self._composer.typing_started.connect(self._on_typing_start)
        self._composer.typing_stopped.connect(self._on_typing_stop)
        self._emoji_panel.emoji_selected.connect(self._composer.insert_emoji)
        self._emoji_panel.emoji_selected.connect(lambda _: self._emoji_panel.hide())

        # Middle row: messages + collapsible info panel
        middle = QWidget()
        middle.setObjectName("ChatMiddle")
        middle_lay = QHBoxLayout(middle)
        middle_lay.setContentsMargins(0, 0, 0, 0)
        middle_lay.setSpacing(0)
        middle_lay.addWidget(self._msgs_stack, 1)
        self._info_panel = RoomInfoPanel(theme)
        self._info_panel.hide()
        middle_lay.addWidget(self._info_panel)

        self._header.info_toggled.connect(self._toggle_info_panel)

        chat_lay.addWidget(self._header)
        chat_lay.addWidget(middle, 1)
        chat_lay.addWidget(self._typing)
        chat_lay.addWidget(self._emoji_panel)
        chat_lay.addWidget(self._composer)
        self._stack.addWidget(self._chat_widget)

        self._stack.setCurrentWidget(self._empty)

        # Typing signals — forwarded from composer, emitted so MainWindow can bridge
        self._typing_started_cb = None
        self._typing_stopped_cb = None

    def set_typing_callbacks(self, start_cb, stop_cb):
        self._typing_started_cb = start_cb
        self._typing_stopped_cb = stop_cb

    def set_own_avatar(self, name: str, pixmap: QPixmap | None):
        self._own_name   = name
        self._own_pixmap = pixmap
        for msgs in self._msgs_by_room.values():
            msgs._own_name = name
            msgs.set_own_avatar(pixmap)

    def set_peer_avatar(self, name: str, pixmap: QPixmap) -> None:
        self._peer_pixmaps[name] = pixmap
        for msgs in self._msgs_by_room.values():
            msgs.set_peer_avatar(name, pixmap)

    def _on_typing_start(self):
        if self._typing_started_cb:
            self._typing_started_cb()

    def _on_typing_stop(self):
        if self._typing_stopped_cb:
            self._typing_stopped_cb()

    def _toggle_emoji(self):
        self._emoji_panel.setVisible(not self._emoji_panel.isVisible())

    def _toggle_info_panel(self):
        self._info_panel.setVisible(not self._info_panel.isVisible())

    def open_room(self, room_id: str, name: str, members: list[str], locked: bool,
                  conn_state: str = "ok", creator: str = "",
                  created_at: float = 0.0, icon: str = "",
                  is_creator: bool = False):
        self._room_id = room_id
        self._header.update_room(name, members, locked, conn_state, icon)
        self._info_panel.update_room(room_id, name, creator, created_at, icon, is_creator)

        self._ensure_messages_area(room_id)

        self._msgs_stack.setCurrentWidget(self._msgs_by_room[room_id])
        self._composer.set_enabled(True)
        self._composer.clear_reply()
        self._typing.hide_typing()
        self._emoji_panel.hide()
        self._stack.setCurrentWidget(self._chat_widget)

    def close_room(self, remove_history: bool = False):
        rid = self._room_id
        self._room_id = None
        self._msgs_stack.setCurrentWidget(self._msgs_placeholder)

        if remove_history and rid and rid in self._msgs_by_room:
            msgs = self._msgs_by_room.pop(rid)
            self._msgs_stack.removeWidget(msgs)
            msgs.deleteLater()

        self._composer.set_enabled(False)
        self._composer.clear_reply()
        self._typing.hide_typing()
        self._emoji_panel.hide()
        self._stack.setCurrentWidget(self._empty)

    def update_member_count(self, count: int):
        self._header.update_member_count(count)

    def update_room_name(self, room_id: str, name: str, locked: bool = False):
        if self._room_id == room_id:
            self._header.update_room_name(name, locked)
            self._info_panel.update_name(name)

    def update_room_icon(self, room_id: str, icon: str):
        if self._room_id == room_id:
            self._info_panel.update_icon(icon)

    def _active_msgs(self) -> MessagesArea | None:
        w = self._msgs_stack.currentWidget()
        return w if isinstance(w, MessagesArea) else None

    def add_message(self, sender: str, text: str, ts: float,
                    outgoing: bool, seq: int = 0,
                    quote: dict | None = None) -> BubbleWidget | None:
        if msgs := self._active_msgs():
            return msgs.add_message(sender, text, ts, outgoing, seq=seq, quote=quote)
        return None

    def add_sys(self, text: str):
        if msgs := self._active_msgs():
            msgs.add_sys_msg(text)

    def add_file_card(self, card):
        if msgs := self._active_msgs():
            msgs.add_file_card(card)

    def add_file_card_to_room(self, room_id: str, card):
        msgs = self._ensure_messages_area(room_id)
        msgs.add_file_card(card)

    def _ensure_messages_area(self, room_id: str) -> MessagesArea:
        if room_id not in self._msgs_by_room:
            msgs = MessagesArea(theme=self._theme, own_name=self._own_name)
            msgs.set_own_avatar(self._own_pixmap)
            for peer, px in self._peer_pixmaps.items():
                msgs.set_peer_avatar(peer, px)
            msgs.reply_requested.connect(self.reply_requested)
            self._msgs_by_room[room_id] = msgs
            self._msgs_stack.addWidget(msgs)
        return self._msgs_by_room[room_id]

    def show_typing(self, username: str):
        self._typing.show_typing(username)

    def hide_typing(self, username: str = ""):
        self._typing.hide_typing()

    def set_conn_state(self, state: str):
        self._header.set_conn_state(state)

    @property
    def send_message(self):
        return self._composer.send_message

    @property
    def current_room_id(self) -> str | None:
        return self._room_id

    @property
    def composer(self) -> Composer:
        return self._composer


# ── Files panel ───────────────────────────────────────────────────────────────

class _FileRow(QWidget):
    def __init__(self, filename: str, from_user: str, room_name: str,
                 size: int, save_path: str, parent=None):
        super().__init__(parent)
        self.setObjectName("ConvRow")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(10)

        from .widgets import _file_icon
        icon = QLabel(_file_icon(filename))
        icon.setFixedWidth(26)
        lay.addWidget(icon)

        info = QVBoxLayout()
        info.setSpacing(2)
        name_w = QLabel(filename)
        name_w.setObjectName("ConvRowName")
        name_w.setMaximumWidth(155)
        from_w = QLabel(f"{from_user}  ·  {room_name}")
        from_w.setObjectName("ConvRowPreview")
        if size < 1024:
            sz = f"{size} B"
        elif size < 1024 * 1024:
            sz = f"{size / 1024:.0f} KB"
        else:
            sz = f"{size / 1024 / 1024:.1f} MB"
        size_w = QLabel(sz)
        size_w.setObjectName("ConvRowTime")
        info.addWidget(name_w)
        info.addWidget(from_w)
        info.addWidget(size_w)
        lay.addLayout(info, 1)

        if os.path.exists(save_path):
            btn = QPushButton("打开")
            btn.setObjectName("BtnGhost")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda: os.startfile(save_path))
            lay.addWidget(btn)


class FilesPanel(QWidget):
    def __init__(self, theme: str = "light", parent=None):
        super().__init__(parent)
        self.setObjectName("ConvPanel")
        self.setFixedWidth(300)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        hdr = QWidget()
        hdr.setObjectName("ConvHeader")
        hlay = QHBoxLayout(hdr)
        hlay.setContentsMargins(16, 14, 16, 10)
        hlay.addWidget(_lbl("Files", "ConvTitle"))
        hlay.addStretch()
        lay.addWidget(hdr)

        scroll = QScrollArea()
        scroll.setObjectName("ConvScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._inner = QWidget()
        self._inner.setObjectName("ConvList")
        self._inner_lay = QVBoxLayout(self._inner)
        self._inner_lay.setContentsMargins(0, 0, 0, 0)
        self._inner_lay.setSpacing(0)
        self._empty_lbl = _lbl("暂无共享文件", "EmptyDesc")
        self._empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_lbl.setContentsMargins(20, 40, 20, 40)
        self._inner_lay.addWidget(self._empty_lbl)
        self._inner_lay.addStretch()

        scroll.setWidget(self._inner)
        lay.addWidget(scroll, 1)

    def add_file(self, filename: str, from_user: str, room_name: str,
                 size: int, save_path: str):
        self._empty_lbl.hide()
        row = _FileRow(filename, from_user, room_name, size, save_path)
        self._inner_lay.insertWidget(1, row)   # newest at top, after hidden empty


# ── Main window ───────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self, server_url: str = "ws://localhost:8765",
                 username: str = "", theme: str = "light"):
        super().__init__()
        self._server_url = server_url
        self._username   = username or "me"
        self._theme      = theme
        self._bridge: WSBridge | None = None

        # Room state: room_id → {name, members, locked, key}
        self._rooms: dict[str, dict] = {}
        # Server-tracked room (may differ from displayed room when viewing a DM)
        self._server_room_id: str = ""
        # Room to re-join after auto-reconnect
        self._reconnect_room_id: str = ""
        # True when we're leaving a room to join/create another (don't remove sidebar entry)
        self._implicit_leave: bool = False

        # Bubble tracking for delivery receipts
        # client_mid (local int) → BubbleWidget, moved to seq key after SEND_ACK
        self._pending_bubbles: dict[int, BubbleWidget] = {}
        self._seq_bubbles:     dict[int, BubbleWidget] = {}
        self._msg_counter = 0

        # DM state: "@peer" → peer username
        self._dms: dict[str, str] = {}

        # File transfer state — configurable download directory
        downloads = self._load_download_dir()
        ice_servers = self._load_ice_servers()
        self._ft_manager = FileTransferManager(downloads_dir=downloads)
        self._ft_cards: dict[str, "FileCard"] = {}
        self._room_file_senders: dict[str, RoomFileSender] = {}
        self._direct_file_senders: dict[str, DirectFileSender] = {}
        self._current_peer: str = ""
        self._identified = False
        self._webrtc_file_pending: dict[str, dict] = {}
        self._webrtc_supported = True
        self._ice_servers = ice_servers
        self._webrtc_transfer = self._new_webrtc_transfer(downloads, ice_servers)

        # Voice call state
        self._voice_call: VoiceCall | None = None   # created after bridge connects
        self._call_widget: CallWidget | None = None
        self._incoming_dlg: IncomingCallDialog | None = None

        # Typing state
        self._is_typing = False
        self._typing_timer = QTimer(self)
        self._typing_timer.setSingleShot(True)
        self._typing_timer.setInterval(3000)
        self._typing_timer.timeout.connect(self._on_typing_stop)

        # Tray / close preference: None=ask, "tray"=minimize, "quit"=quit
        self._close_pref: str | None = self._load_close_pref()
        # Pending tray notifications: list of (source, text) for tooltip
        self._tray_msgs: list[tuple[str, str]] = []

        self._build_ui()
        self._apply_theme()
        self.setWindowTitle("Beam — P2P Chat")
        self.resize(1100, 720)
        self.setMinimumSize(800, 560)
        self.statusBar().setSizeGripEnabled(False)
        self.statusBar().hide()
        self._load_avatar()
        self._setup_tray()

        # Clean up any leftover update temp files from a previous update attempt
        if getattr(sys, "frozen", False):
            _exe = pathlib.Path(sys.executable)
            for _tmp in (_exe.with_name("_BeamChat_update.exe"),
                         _exe.with_name("_update.bat")):
                try:
                    if _tmp.exists():
                        _tmp.unlink()
                except Exception:
                    pass

        QTimer.singleShot(100, self._connect)
        # Check for updates in background — only when running as frozen EXE
        if getattr(sys, "frozen", False):
            QTimer.singleShot(3000, self._check_update_bg)

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        central.setObjectName("AppRoot")
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._rail = Rail()
        self._rail.set_username(self._username)
        self._rail._settings_btn.clicked.connect(self._open_settings)
        self._rail._btns["chats"].clicked.connect(self._on_rail_chats)
        self._rail._btns["peers"].clicked.connect(self._on_rail_peers)
        self._rail._btns["files"].clicked.connect(self._on_rail_files)
        self._rail.avatar_change.connect(self._on_change_avatar)
        self._rail.set_active("chats")
        root.addWidget(self._rail)

        # Side panel: update banner + stacked pages
        side_col = QWidget()
        side_col.setFixedWidth(300)
        side_col_lay = QVBoxLayout(side_col)
        side_col_lay.setContentsMargins(0, 0, 0, 0)
        side_col_lay.setSpacing(0)

        # Update banner (hidden until a new version is found)
        self._update_bar = QWidget()
        self._update_bar.setObjectName("UpdateBar")
        self._update_bar.setFixedHeight(36)
        self._update_bar.hide()
        ub_lay = QHBoxLayout(self._update_bar)
        ub_lay.setContentsMargins(10, 0, 6, 0)
        self._update_lbl = QLabel()
        self._update_lbl.setObjectName("UpdateBarLabel")
        ub_lay.addWidget(self._update_lbl, 1)
        ub_btn = _btn("立即更新", "UpdateBarBtn")
        ub_btn.setFixedHeight(24)
        ub_btn.clicked.connect(self._on_do_update)
        ub_lay.addWidget(ub_btn)
        side_col_lay.addWidget(self._update_bar)

        self._side_stack = QStackedWidget()
        self._conv = ConvPanel(self._theme)
        self._conv.room_selected.connect(self._on_room_selected)
        self._conv.room_right_clicked.connect(self._on_room_right_clicked)
        self._conv.create_room.connect(self._on_create_room)
        self._conv.search_rooms.connect(self._on_search_rooms)
        self._side_stack.addWidget(self._conv)           # index 0: chats

        self._files_panel = FilesPanel(self._theme)
        self._side_stack.addWidget(self._files_panel)    # index 1: files

        side_col_lay.addWidget(self._side_stack)
        root.addWidget(side_col)

        self._chat = ChatPanel(self._theme)
        self._chat.send_message.connect(self._on_send_message)
        self._chat.reply_requested.connect(self._on_reply_requested)
        self._chat.set_typing_callbacks(self._on_typing_start, self._on_typing_stop)
        self._chat.composer.file_selected.connect(self._start_file_send)
        self._chat._info_panel.rename_requested.connect(self._on_room_rename)
        self._chat._info_panel.icon_change_requested.connect(self._on_room_icon_change)
        root.addWidget(self._chat)

    def _apply_theme(self):
        self.setStyleSheet(make_qss(self._theme))

    # ── WebSocket connection ──────────────────────────────────────────────────

    def _connect(self):
        if self._bridge:
            self._bridge.close()
            self._bridge.wait(2000)

        self._bridge = WSBridge(self._server_url)
        self._bridge.received.connect(self._on_frame)
        self._bridge.connected.connect(self._on_connected)
        self._bridge.disconnected.connect(self._on_disconnected)
        self._bridge.reconnecting.connect(self._on_reconnecting)
        self._bridge.start()

    @pyqtSlot()
    def _on_connected(self):
        if self._voice_call is None:
            self._voice_call = VoiceCall(self._bridge, self._username, self)
            self._voice_call.incoming_call.connect(self._on_incoming_call)
            self._voice_call.call_ended.connect(self._on_call_ended)
            self._voice_call.state_changed.connect(self._on_call_state_changed)
            self._voice_call.mode_changed.connect(self._on_call_mode_changed)
            self._voice_call.duration_tick.connect(self._on_call_duration)
        self._identified = False
        self.setWindowTitle("Beam — P2P Chat")
        self._bridge.send_frame(T.SET_NAME, name=self._username)
        # Mark all known rooms online
        for rid in self._rooms:
            self._conv.set_conn_state(rid, "ok")
        self._chat.set_conn_state("ok")

    @pyqtSlot(int)
    def _on_reconnecting(self, attempt: int):
        self.setWindowTitle(f"Beam — P2P Chat  [重连中... #{attempt}]")

    @pyqtSlot(str)
    def _on_disconnected(self, reason: str):
        if self._voice_call and self._voice_call.state.name != "IDLE":
            self._voice_call.hangup()
        _log.error("bridge disconnected: %s", reason)
        self._identified = False
        self.setWindowTitle("Beam — P2P Chat  [断线]")
        # Save room for auto-reconnect before clearing server state
        self._reconnect_room_id = self._server_room_id
        self._server_room_id = ""
        self._implicit_leave = False
        self._chat.close_room()
        # Mark all conv rows offline
        for rid in self._rooms:
            self._conv.set_conn_state(rid, "offline")
        self._chat.set_conn_state("offline")
        # Cancel all in-progress file transfers
        for _tid, _card in list(self._ft_cards.items()):
            _card.set_error("连接断开")
            self._ft_manager.cancel(_tid)
        self._ft_cards.clear()
        self._room_file_senders.clear()
        self._direct_file_senders.clear()

    # ── Incoming frame dispatcher ─────────────────────────────────────────────

    @pyqtSlot(str)
    def _on_frame(self, raw: str):
        try:
            msg = unpack(raw)
        except Exception:
            return
        mtype   = msg.get("type", "")
        payload = msg.get("payload", {})
        ts      = msg.get("ts", time.time())
        try:
            self._dispatch_frame(mtype, payload, ts)
        except Exception as exc:
            import traceback
            _log.error("frame dispatch error (type=%s): %s\n%s",
                       mtype, exc, traceback.format_exc())
            traceback.print_exc()

    def _dispatch_frame(self, mtype: str, payload: dict, ts: float):

        if mtype == T.WELCOME:
            pass

        elif mtype == T.SYSTEM:
            if payload.get("message", "").startswith("Name set to"):
                self._identified = True
                self._send_avatar()
                self._bridge.send_frame(T.LIST_ROOMS)
                rejoin = self._reconnect_room_id
                self._reconnect_room_id = ""
                if rejoin and rejoin in self._rooms:
                    pw = self._rooms[rejoin].get("_password", "")
                    self._rooms[rejoin]["_pending_key"] = derive_key(rejoin, pw)
                    _log.info("Reconnect: re-joining room %s", rejoin)
                    self._bridge.send_frame(T.JOIN_ROOM, room_id=rejoin, password=pw)

        elif mtype == T.ERROR:
            msg = payload.get("message", "")
            # Silently swallow avatar errors — old servers don't know SET_AVATAR/USER_AVATAR
            if "AVATAR" in msg.upper():
                _log.warning("server avatar error (suppressed): %s", msg)
                return
            if msg == "SET_NAME first":
                _log.warning("transient auth error during reconnect: %s", msg)
                return
            if "already taken" in msg and not self._identified:
                _log.warning("name temporarily taken during reconnect: %s", msg)
                self.setWindowTitle("Beam — P2P Chat  [等待重连身份确认]")
                return
            if "Unknown type 'WEBRTC_" in msg:
                _log.warning("server does not support WebRTC signaling: %s", msg)
                self._webrtc_supported = False
                self._fallback_all_webrtc_files("server-unsupported")
                return
            pending_room = "__pending__" in self._rooms
            if pending_room:
                _log.error("CREATE_ROOM error: %s", msg)
                self._rooms.pop("__pending__", None)
                QMessageBox.critical(self, "创建房间失败", msg)
            else:
                _log.warning("server error: %s", msg)
                QMessageBox.warning(self, "错误", msg)

        elif mtype == T.ROOM_CREATED:
            rid        = payload["room_id"]
            name       = payload["name"]
            locked     = payload.get("locked", False)
            created_at = payload.get("created_at", time.time())
            pending    = self._rooms.pop("__pending__", {})
            pw  = pending.get("_pending_pw", "")
            key = derive_key(rid, pw)
            self._rooms[rid] = {"name": name, "members": [self._username],
                                "locked": locked, "key": key, "_password": pw,
                                "creator": self._username,
                                "created_at": created_at, "icon": ""}
            self._conv.upsert_room(rid, name, self._username, 1, locked)
            self._conv.set_active(rid)
            self._conv.set_conn_state(rid, "ok")
            self._server_room_id = rid
            self._chat.open_room(rid, name, [self._username], locked,
                                 creator=self._username, created_at=created_at,
                                 icon="", is_creator=True)

        elif mtype == T.ROOM_JOINED:
            rid        = payload["room_id"]
            name       = payload["name"]
            members    = payload.get("members", [])
            locked     = payload.get("locked", False)
            creator    = payload.get("creator", "")
            created_at = payload.get("created_at", 0.0)
            icon       = payload.get("icon", "")
            pw         = self._rooms.get(rid, {}).get("_password", "")
            # _pending_key is set by _on_join_from_search / _on_room_selected.
            # Fall back to deriving from stored password so the key is never wiped.
            key        = self._rooms.get(rid, {}).get("_pending_key") or derive_key(rid, pw)
            self._rooms[rid] = {"name": name, "members": members,
                                "locked": locked, "key": key, "_password": pw,
                                "creator": creator,
                                "created_at": created_at, "icon": icon}
            self._conv.upsert_room(rid, name, creator, len(members), locked)
            self._conv.set_active(rid)
            self._conv.set_conn_state(rid, "ok")
            self._chat.open_room(rid, name, members, locked,
                                 creator=creator, created_at=created_at, icon=icon,
                                 is_creator=(creator == self._username))
            # Track first non-self member as file transfer peer
            others = [m for m in members if m != self._username]
            self._current_peer = others[0] if others else ""
            self._server_room_id = rid

        elif mtype == T.ROOM_LEFT:
            rid = self._server_room_id
            self._server_room_id = ""
            if rid:
                if self._implicit_leave:
                    # Switching to another room — preserve sidebar entry and history
                    self._implicit_leave = False
                    if self._chat.current_room_id == rid:
                        self._chat.close_room(remove_history=False)
                else:
                    # Explicit leave or room dissolved — discard
                    self._rooms.pop(rid, None)
                    self._conv.remove_room(rid)
                    if self._chat.current_room_id == rid:
                        self._chat.close_room(remove_history=True)

        elif mtype == T.USER_JOINED:
            uname = payload.get("username", "")
            rid   = payload.get("room_id", "")
            if rid in self._rooms:
                members = self._rooms[rid].get("members", [])
                if uname not in members:
                    members.append(uname)
                count = len(self._rooms[rid]["members"])
                self._conv.update_members(rid, count)
                if rid == self._chat.current_room_id:
                    self._chat.update_member_count(count)
            if rid == self._chat.current_room_id:
                self._chat.add_sys(f"{uname} joined")

        elif mtype == T.USER_LEFT:
            uname = payload.get("username", "")
            rid   = payload.get("room_id", "")
            if rid in self._rooms:
                self._rooms[rid]["members"] = [
                    m for m in self._rooms[rid].get("members", []) if m != uname
                ]
                count = len(self._rooms[rid]["members"])
                self._conv.update_members(rid, count)
                if rid == self._chat.current_room_id:
                    self._chat.update_member_count(count)
            if rid == self._chat.current_room_id:
                self._chat.add_sys(f"{uname} left")

        elif mtype == T.NEW_MSG:
            sender    = payload.get("sender", "?")
            text      = payload.get("text", "")
            encrypted = payload.get("encrypted", False)
            rid       = payload.get("room_id", self._chat.current_room_id or "")
            seq       = payload.get("seq", 0)
            reply_to  = payload.get("reply_to")

            # Decrypt if needed
            if encrypted:
                key = self._rooms.get(rid, {}).get("key") or \
                      self._rooms.get(self._chat.current_room_id or "", {}).get("key")
                if key:
                    plain = decrypt(key, text)
                    text  = plain if plain else "[decryption failed]"
                    if reply_to and reply_to.get("text"):
                        dec = decrypt(key, reply_to["text"])
                        if dec:
                            reply_to = dict(reply_to, text=dec)
                else:
                    text = "[encrypted — join with room password]"

            active = (rid == self._chat.current_room_id) or \
                     (rid == "" and self._chat.current_room_id is not None)

            if active:
                self._chat.add_message(sender, text, ts, outgoing=False,
                                       seq=seq, quote=reply_to)
                # Received while room is visible → mark as read
                if seq and self._bridge:
                    self._bridge.send_frame(T.MSG_ACK, seq=seq, status="read")
            else:
                self._conv.set_preview(rid, f"{sender}: {text}", ts)
                # Delivered but not yet read
                if seq and self._bridge:
                    self._bridge.send_frame(T.MSG_ACK, seq=seq, status="delivered")
            # Flash taskbar / tray when window is not in focus
            if not self.isActiveWindow():
                QApplication.alert(self, 0)
                if not self.isVisible():
                    room_name = self._rooms.get(rid, {}).get("name", rid)
                    self._notify_tray(f"{room_name}", f"{sender}: {text}")

        elif mtype == T.ROOM_LIST:
            for r in payload.get("rooms", []):
                rid        = r["id"]
                creator    = r.get("creator", "")
                created_at = r.get("created_at", 0.0)
                icon       = r.get("icon", "")
                self._conv.upsert_room(
                    rid, r["name"], creator,
                    r.get("members", 0), r.get("locked", False)
                )
                if rid not in self._rooms:
                    self._rooms[rid] = {"name": r["name"], "members": [],
                                        "locked": r.get("locked", False),
                                        "key": None, "creator": creator,
                                        "created_at": created_at, "icon": icon}
                else:
                    # Update fields that can change
                    self._rooms[rid]["created_at"] = created_at
                    self._rooms[rid]["icon"] = icon

        elif mtype == T.ROOM_DELETED:
            rid = payload.get("room_id", "")
            self._rooms.pop(rid, None)
            self._conv.remove_room(rid)
            if rid == self._server_room_id:
                self._server_room_id = ""
            if self._chat.current_room_id == rid:
                self._chat.close_room(remove_history=True)
                QMessageBox.information(self, "聊天室已删除", "该聊天室已被创建者删除。")

        elif mtype == T.ROOM_NAME_UPDATED:
            rid  = payload.get("room_id", "")
            name = payload.get("name", "")
            if rid in self._rooms:
                locked = self._rooms[rid].get("locked", False)
                self._rooms[rid]["name"] = name
                self._conv.update_room_name(rid, name)
                self._chat.update_room_name(rid, name, locked)

        elif mtype == T.ROOM_ICON_UPDATED:
            rid  = payload.get("room_id", "")
            icon = payload.get("icon", "")
            if rid in self._rooms:
                self._rooms[rid]["icon"] = icon
                self._chat.update_room_icon(rid, icon)

        # ── New protocol messages ─────────────────────────────────────────────

        elif mtype == T.USER_TYPING:
            uname  = payload.get("username", "")
            typing = payload.get("typing", False)
            rid    = payload.get("room_id", "")
            if rid == self._chat.current_room_id:
                if typing:
                    self._chat.show_typing(uname)
                else:
                    self._chat.hide_typing(uname)

        elif mtype == T.SEND_ACK:
            # Server confirmed our message was received and assigned a seq
            client_mid = payload.get("client_mid", -1)
            seq        = payload.get("seq", 0)
            if client_mid in self._pending_bubbles:
                bubble = self._pending_bubbles.pop(client_mid)
                self._seq_bubbles[seq] = bubble
                bubble.set_status("sent")

        elif mtype == T.MSG_STATUS:
            seq    = payload.get("seq", 0)
            status = payload.get("status", "delivered")
            if seq in self._seq_bubbles:
                self._seq_bubbles[seq].set_status(status)

        elif mtype == T.USER_LIST:
            self._show_peers_dialog(payload.get("users", []))

        elif mtype == T.RECV_DM:
            peer   = payload.get("from", "")
            text   = payload.get("text", "")
            dm_id  = f"@{peer}"
            if dm_id not in self._dms:
                self._dms[dm_id] = peer
                self._conv.upsert_room(dm_id, f"@ {peer}", peer, 0, False)
                # show "Direct Message" instead of "0 members"
                if row := self._conv._rows.get(dm_id):
                    row.set_preview("Direct Message")
            if self._chat.current_room_id == dm_id:
                self._chat.add_message(peer, text, ts, outgoing=False)
            else:
                self._conv.set_preview(dm_id, f"{peer}: {text}", ts)
            if not self.isActiveWindow():
                QApplication.alert(self, 0)
                if not self.isVisible():
                    self._notify_tray(f"@ {peer}", text)

        elif mtype == T.DM_ACK:
            client_mid = payload.get("client_mid", -1)
            if client_mid in self._pending_bubbles:
                bubble = self._pending_bubbles.pop(client_mid)
                bubble.set_status("delivered")

        elif mtype == T.FILE_OFFER:
            self._on_file_offer(payload)
        elif mtype == T.FILE_ACCEPT:
            self._on_file_accept(payload)
        elif mtype == T.FILE_REJECT:
            self._on_file_reject(payload)
        elif mtype == T.FILE_CHUNK:
            self._on_file_chunk(payload)
        elif mtype == T.FILE_DONE:
            self._on_file_done(payload)
        elif mtype == T.FILE_ERROR:
            self._on_file_error(payload)
        elif mtype == T.FILE_ROOM_SHARE:
            self._on_file_room_share(payload)
        elif mtype == T.FILE_ROOM_CHUNK:
            self._on_file_room_chunk(payload)
        elif mtype == T.FILE_ROOM_CHUNK_ACK:
            self._on_file_room_chunk_ack(payload)
        elif mtype == T.FILE_ROOM_DONE:
            self._on_file_room_done(payload)
        elif mtype == T.FILE_ROOM_DONE_ACK:
            self._on_file_room_done_ack(payload)
        elif mtype == T.FILE_ROOM_ERROR:
            self._on_file_room_error(payload)
        elif mtype == T.CALL_OFFER:
            peer    = payload.get("from", "")
            room_id = payload.get("room_id", "")
            if self._voice_call:
                self._voice_call.on_call_offer(peer, room_id)

        elif mtype == T.CALL_ANSWER:
            if self._voice_call:
                self._voice_call.on_call_answer()

        elif mtype == T.CALL_REJECT:
            reason = payload.get("reason", "")
            if self._voice_call:
                self._voice_call.on_call_reject(reason)

        elif mtype == T.CALL_HANGUP:
            if self._voice_call:
                self._voice_call.on_call_hangup()

        elif mtype == T.CALL_ICE:
            candidate = payload.get("candidate", {})
            if self._voice_call:
                self._voice_call.on_call_ice(candidate)

        elif mtype == T.VOICE_CHUNK:
            data = payload.get("data", "")
            if self._voice_call:
                self._voice_call.on_voice_chunk(data)

        elif mtype == T.WEBRTC_OFFER:
            peer = payload.get("from", "")
            if peer:
                self._ensure_dm_conversation(peer)
            self._run_webrtc_task(self._webrtc_transfer.handle_offer(payload))

        elif mtype == T.WEBRTC_ANSWER:
            self._run_webrtc_task(self._webrtc_transfer.handle_answer(payload))

        elif mtype == T.WEBRTC_ICE:
            self._run_webrtc_task(self._webrtc_transfer.handle_ice(payload))

        elif mtype in (T.WEBRTC_CLOSE, T.WEBRTC_ERROR):
            session_id = str(payload.get("session_id", ""))
            if session_id:
                message = "对端已关闭传输" if mtype == T.WEBRTC_CLOSE else payload.get("message", "WebRTC 传输失败")
                self._mark_webrtc_transfer_closed(session_id, message)
                self._run_webrtc_task(self._webrtc_transfer.close(session_id))

        elif mtype == T.USER_AVATAR:
            self._on_user_avatar(payload)

    def _run_webrtc_task(self, coro, *, raise_errors: bool = False):
        try:
            asyncio.run(coro)
        except Exception as exc:
            _log.error("WebRTC task failed: %s", exc)
            if raise_errors:
                raise

    def _new_webrtc_transfer(self, downloads: pathlib.Path,
                             ice_servers: list[dict] | None = None) -> WebRTCTransfer:
        return WebRTCTransfer(
            lambda msg_type, **payload: self._bridge.send_frame(msg_type, **payload)
            if self._bridge else False,
            downloads_dir=downloads,
            on_file_received=self._on_webrtc_file_received,
            on_file_sent=self._on_webrtc_file_sent,
            on_file_progress=self._on_webrtc_file_progress,
            on_channel_open=self._on_webrtc_channel_open,
            on_session_closed=self._on_webrtc_session_closed,
            ice_servers=ice_servers,
        )

    def _on_webrtc_file_received(self, save_path: pathlib.Path, meta: dict):
        filename = str(meta.get("filename", save_path.name))
        from_user = str(meta.get("from_user", "?"))
        size = int(meta.get("size", save_path.stat().st_size))
        _log.info("WEBRTC file received session=%s peer=%s filename=%s size=%d path=%s",
                  meta.get("transfer_id", ""), from_user, filename, size, save_path)
        self._files_panel.add_file(filename, from_user, "WebRTC", size, str(save_path))
        theme = self.__dict__.get("_theme", "light")
        card = FileCard(str(meta.get("transfer_id", "")), filename, size,
                        outgoing=False, theme=theme)
        card.set_done(save_path=str(save_path))
        self._add_dm_file_card(from_user, card)

    def _on_webrtc_file_sent(self, source_path: pathlib.Path, meta: dict):
        tid = str(meta.get("transfer_id", ""))
        self._webrtc_file_pending.pop(tid, None)
        filename = str(meta.get("filename", source_path.name))
        to_user = str(meta.get("to_user", "?"))
        size = int(meta.get("size", source_path.stat().st_size))
        _log.info("WEBRTC file sent session=%s peer=%s filename=%s size=%d",
                  tid, to_user, filename, size)
        if card := self._ft_cards.pop(tid, None):
            card.set_done()
        self._files_panel.add_file(filename, self._username, f"@ {to_user}", size, str(source_path))

    def _ensure_dm_conversation(self, peer: str) -> str:
        dm_id = f"@{peer}"
        dms = self.__dict__.setdefault("_dms", {})
        if dm_id not in dms:
            dms[dm_id] = peer
            conv = self.__dict__.get("_conv")
            if conv is not None:
                conv.upsert_room(dm_id, f"@ {peer}", peer, 0, False)
            rows = getattr(conv, "_rows", None) if conv is not None else None
            if rows is not None and (row := rows.get(dm_id)):
                row.set_preview("Direct Message")
        return dm_id

    def _add_dm_file_card(self, peer: str, card):
        dm_id = self._ensure_dm_conversation(peer)
        chat = self.__dict__.get("_chat")
        if chat is not None:
            chat.add_file_card_to_room(dm_id, card)
        conv = self.__dict__.get("_conv")
        if conv is not None:
            filename = getattr(card, "_filename", "文件")
            conv.set_preview(dm_id, f"{peer}: 📎 {filename}", time.time())
            current_room = getattr(chat, "current_room_id", None) if chat is not None else None
            if current_room != dm_id and hasattr(conv, "increment_unread"):
                conv.increment_unread(dm_id)

    def _on_webrtc_channel_open(self, meta: dict):
        session_id = str(meta.get("session_id", ""))
        if session_id:
            _log.info("WEBRTC channel open session=%s peer=%s filename=%s size=%s",
                      session_id, meta.get("peer", ""), meta.get("filename", ""),
                      meta.get("size", ""))
            self._webrtc_file_pending.pop(session_id, None)

    def _on_webrtc_file_progress(self, meta: dict):
        tid = str(meta.get("transfer_id", ""))
        if not tid:
            return
        if card := self._ft_cards.get(tid):
            card.set_progress(int(meta.get("progress", 0)))

    def _mark_webrtc_transfer_closed(self, session_id: str, message: str):
        self._webrtc_file_pending.pop(session_id, None)
        _log.info("WEBRTC transfer closed session=%s message=%s", session_id, message)
        cards = self.__dict__.get("_ft_cards", {})
        if card := cards.pop(session_id, None):
            card.set_error(message)

    def _on_webrtc_session_closed(self, meta: dict):
        session_id = str(meta.get("session_id", ""))
        if session_id:
            self._mark_webrtc_transfer_closed(session_id, str(meta.get("message", "WebRTC 传输已关闭")))

    def _on_user_avatar(self, payload: dict):
        import base64
        name = payload.get("name", "")
        data = payload.get("data", "")
        if not name or not data:
            return
        try:
            raw = base64.b64decode(data)
        except Exception:
            return
        px = QPixmap()
        if not px.loadFromData(raw):
            return
        self._chat.set_peer_avatar(name, px)

    # ── Voice call slots ──────────────────────────────────────────────────────

    def _on_incoming_call(self, peer: str) -> None:
        QApplication.beep()
        self._incoming_dlg = IncomingCallDialog(peer, self._theme, self)
        self._incoming_dlg.setStyleSheet(self.styleSheet())
        self._incoming_dlg.accepted_signal.connect(
            lambda: self._voice_call.accept_call() if self._voice_call else None
        )
        self._incoming_dlg.rejected_signal.connect(
            lambda: self._voice_call.reject_call(peer) if self._voice_call else None
        )
        self._incoming_dlg.show()

    def _on_call_state_changed(self, state_name: str) -> None:
        if state_name == "CONNECTED":
            if self._incoming_dlg:
                self._incoming_dlg.close()
                self._incoming_dlg = None
            peer = self._voice_call.peer if self._voice_call else ""
            self._call_widget = CallWidget(peer, self._theme)
            self._call_widget.setStyleSheet(self.styleSheet())
            self._call_widget.hangup_requested.connect(
                lambda: self._voice_call.hangup() if self._voice_call else None
            )
            self._call_widget.mute_toggled.connect(self._on_call_mute_toggled)
            geo = self.geometry()
            self._call_widget.move(
                geo.right() - self._call_widget.width() - 20,
                geo.bottom() - self._call_widget.height() - 60,
            )
            self._call_widget.show()
        elif state_name == "IDLE":
            if self._call_widget:
                self._call_widget.close()
                self._call_widget = None
            if self._incoming_dlg:
                self._incoming_dlg.close()
                self._incoming_dlg = None

    def _on_call_mode_changed(self, mode: str) -> None:
        if self._call_widget:
            self._call_widget.set_mode(mode)

    def _on_call_duration(self, seconds: int) -> None:
        if self._call_widget:
            self._call_widget.set_duration(seconds)

    def _on_call_mute_toggled(self) -> None:
        if self._voice_call:
            muted = self._voice_call.toggle_mute()
            if self._call_widget:
                self._call_widget.set_muted(muted)

    def _on_call_ended(self, reason: str, duration: int) -> None:
        """Write a system message into the room where the call was initiated."""
        room_id = ""
        if self._voice_call:
            room_id = self._voice_call.room_id
        if not room_id:
            room_id = self._chat.current_room_id or ""
        if not room_id:
            return
        # Switch to the room's MessagesArea directly
        if room_id in getattr(self._chat, "_msgs_by_room", {}):
            msgs = self._chat._msgs_by_room[room_id]
            if reason in ("hangup", "remote_hangup"):
                m, s = divmod(duration, 60)
                msgs.add_sys_msg(f"📞 通话时长 {m:02d}:{s:02d}")
            elif reason == "rejected":
                msgs.add_sys_msg("📵 通话未接通")

    def _start_voice_call(self, peer: str) -> None:
        if not self._voice_call:
            return
        if self._voice_call.state != CallState.IDLE:
            QMessageBox.information(self, "通话中", "当前已有通话进行中。")
            return
        room_id = self._chat.current_room_id or ""
        self._voice_call.start_call(peer, room_id)

    # ── Room management ───────────────────────────────────────────────────────

    def _on_room_right_clicked(self, room_id: str):
        room = self._rooms.get(room_id, {})
        if room.get("creator") != self._username:
            return
        from PyQt6.QtGui import QCursor
        menu = QMenu(self)
        delete_action = menu.addAction("删除聊天室")
        if menu.exec(QCursor.pos()) == delete_action:
            reply = QMessageBox.question(
                self, "删除聊天室",
                f"确定要永久删除聊天室「{room.get('name', room_id)}」吗？\n所有成员将被踢出，无法恢复。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self._bridge.send_frame(T.DELETE_ROOM, room_id=room_id)

    # ── Typing ────────────────────────────────────────────────────────────────

    def _on_typing_start(self):
        if not self._is_typing:
            self._is_typing = True
            if self._bridge:
                self._bridge.send_frame(T.TYPING, typing=True)
        self._typing_timer.start()

    def _on_typing_stop(self):
        self._typing_timer.stop()
        if self._is_typing:
            self._is_typing = False
            if self._bridge:
                self._bridge.send_frame(T.TYPING, typing=False)

    # ── Rail navigation ──────────────────────────────────────────────────────

    def _on_rail_chats(self):
        self._rail.set_active("chats")
        self._side_stack.setCurrentIndex(0)

    def _on_rail_files(self):
        self._rail.set_active("files")
        self._side_stack.setCurrentIndex(1)

    def _on_rail_peers(self):
        self._rail.set_active("peers")
        if self._bridge:
            self._bridge.send_frame(T.LIST_USERS)

    def _start_dm(self, peer: str):
        """Open (or focus) a DM conversation with peer."""
        dm_id = f"@{peer}"
        if dm_id not in self._dms:
            self._dms[dm_id] = peer
            self._conv.upsert_room(dm_id, f"@ {peer}", peer, 0, False)
            if row := self._conv._rows.get(dm_id):
                row.set_preview("Direct Message")
        self._conv.set_active(dm_id)
        self._current_peer = peer
        self._chat.open_room(dm_id, f"@ {peer}", [peer, self._username], False)
        self._rail.set_active("chats")

    def _show_peers_dialog(self, users: list[str]):
        """Display online users; double-click to start a DM."""
        dlg = QDialog(self)
        dlg.setObjectName("Dialog")
        dlg.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        dlg.setModal(True)
        dlg.setMinimumWidth(280)

        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(10)

        lay.addWidget(_lbl("Online Users", "DialogTitle"))

        if not users:
            lay.addWidget(_lbl("No users online right now.", "EmptyDesc"))
        else:
            list_w = QListWidget()
            for u in users:
                item = QListWidgetItem(f"{u}（我）" if u == self._username else u)
                item.setData(Qt.ItemDataRole.UserRole, u)
                list_w.addItem(item)

            def _on_double_click(item):
                uid = item.data(Qt.ItemDataRole.UserRole)
                if uid != self._username:
                    self._start_dm(uid)
                    dlg.accept()

            def _on_call_click():
                items = list_w.selectedItems()
                if not items:
                    return
                uid = items[0].data(Qt.ItemDataRole.UserRole)
                if uid and uid != self._username:
                    self._start_voice_call(uid)
                    dlg.accept()

            list_w.itemDoubleClicked.connect(_on_double_click)
            lay.addWidget(list_w)

            call_btn = _btn("📞 发起通话", "BtnGhost")
            call_btn.setMinimumWidth(112)
            call_btn.clicked.connect(_on_call_click)
            lay.addWidget(call_btn)

            hint = _lbl("双击发私信，选中后点按钮发起通话", "FormLabel")
            hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lay.addWidget(hint)

        close_btn = _btn("Close", "BtnGhost")
        close_btn.clicked.connect(dlg.reject)
        lay.addWidget(close_btn)

        self._style_dialog(dlg)
        dlg.exec()

    # ── File transfer ─────────────────────────────────────────────────────────

    def _start_file_send(self, path: str):
        rid = self._chat.current_room_id
        if not rid:
            QMessageBox.warning(self, "发送失败", "请先加入一个聊天室再发送文件。")
            return
        if not self._bridge or not self._bridge.is_connected():
            QMessageBox.critical(self, "未连接", "尚未连接到服务器。")
            return

        source_path = pathlib.Path(path)
        size = source_path.stat().st_size
        if size > 50 * 1024 * 1024:
            QMessageBox.warning(self, "文件过大", "文件大小不能超过 50 MB。")
            return
        if rid.startswith("@"):
            peer = self._dms.get(rid, rid[1:])
            self._start_peer_file_send(peer, source_path)
            return

        filename = source_path.name
        tid    = uuid.uuid4().hex[:12]
        sender = RoomFileSender(source_path)
        self._room_file_senders[tid] = {
            "sender": sender,
            "path": source_path,
            "filename": filename,
            "size": size,
            "room_id": rid,
            "done_sent": False,
        }

        mime = guess_mime(filename)
        if mime.startswith("video/"):
            card = VideoCard(tid, filename, size, outgoing=True)
        else:
            card = FileCard(tid, filename, size, outgoing=True, theme=self._theme)
        if hasattr(card, "cancel_requested"):
            card.cancel_requested.connect(self._cancel_transfer)
        self._ft_cards[tid] = card
        self._chat.add_file_card(card)

        total = max(1, (size + CHUNK_SIZE - 1) // CHUNK_SIZE)
        _log.info("File send start: %r  size=%d  chunks=%d", filename, size, total)
        if not self._bridge.send_frame(T.FILE_ROOM_SHARE,
                                       room_id=rid, transfer_id=tid,
                                       filename=filename, size=size, mime=mime):
            card.set_error("连接不可用")
            self._ft_cards.pop(tid, None)
            self._room_file_senders.pop(tid, None)
            return
        self._pump_room_file_sender(tid)

    def _start_peer_file_send(self, peer: str, source_path: pathlib.Path):
        if not self.__dict__.get("_webrtc_supported", True):
            self._start_peer_file_relay(peer, source_path)
            return

        filename = source_path.name
        size = source_path.stat().st_size
        tid = uuid.uuid4().hex[:12]
        _log.info("WEBRTC file start session=%s peer=%s filename=%s size=%d",
                  tid, peer, filename, size)
        card = FileCard(tid, filename, size, outgoing=True, theme=self._theme)
        if hasattr(card, "cancel_requested"):
            card.cancel_requested.connect(self._cancel_transfer)
        self._ft_cards[tid] = card
        self._chat.add_file_card(card)
        try:
            self._run_webrtc_task(
                self._webrtc_transfer.start_offer(peer, source_path, session_id=tid),
                raise_errors=True,
            )
            self._webrtc_file_pending[tid] = {"peer": peer, "path": source_path}
            QTimer.singleShot(WEBRTC_FILE_FALLBACK_MS,
                              lambda tid=tid: self._fallback_webrtc_file_if_pending(tid))
            card.set_progress(1)
        except Exception as exc:
            _log.warning("WebRTC file offer failed, falling back to relay: %s", exc)
            self._start_peer_file_relay(peer, source_path, transfer_id=tid,
                                        existing_card=card)

    def _fallback_webrtc_file_if_pending(self, tid: str):
        pending = self._webrtc_file_pending.pop(tid, None)
        if pending is None:
            return
        _log.info("WEBRTC file fallback session=%s peer=%s filename=%s reason=timeout",
                  tid, pending.get("peer", ""), pending.get("path", pathlib.Path("")).name)
        self._run_webrtc_task(self._webrtc_transfer.close(tid))
        self._start_peer_file_relay(pending["peer"], pending["path"], transfer_id=tid,
                                    existing_card=self._ft_cards.get(tid))

    def _fallback_all_webrtc_files(self, reason: str):
        pending_items = list(self._webrtc_file_pending.items())
        self._webrtc_file_pending.clear()
        for tid, pending in pending_items:
            _log.info("WEBRTC file fallback session=%s peer=%s filename=%s reason=%s",
                      tid, pending.get("peer", ""), pending.get("path", pathlib.Path("")).name,
                      reason)
            self._run_webrtc_task(self._webrtc_transfer.close(tid))
            self._start_peer_file_relay(pending["peer"], pending["path"], transfer_id=tid,
                                        existing_card=self._ft_cards.get(tid))

    def _start_peer_file_relay(self, peer: str, source_path: pathlib.Path, *,
                               transfer_id: str | None = None,
                               existing_card=None):
        tid = transfer_id or self._ft_manager.register_outgoing_path(peer, source_path)
        if tid not in self._ft_manager.outgoing:
            self._ft_manager.outgoing[tid] = {
                "to": peer,
                "filename": source_path.name,
                "path": source_path,
                "sender": DirectFileSender(source_path),
                "mime": guess_mime(source_path.name),
                "size": source_path.stat().st_size,
            }
        info = self._ft_manager.outgoing[tid]
        _log.info("Relay file offer session=%s peer=%s filename=%s size=%d",
                  tid, peer, source_path.name, info["size"])
        card = existing_card or self._ft_cards.get(tid)
        if card is None:
            card = FileCard(tid, source_path.name, source_path.stat().st_size,
                            outgoing=True, theme=self._theme)
            if hasattr(card, "cancel_requested"):
                card.cancel_requested.connect(self._cancel_transfer)
            self._chat.add_file_card(card)
        self._ft_cards[tid] = card
        if not self._bridge.send_frame(T.FILE_OFFER,
                                       to=peer,
                                       transfer_id=tid,
                                       filename=source_path.name,
                                       size=info["size"],
                                       mime=info["mime"]):
            card.set_error("连接不可用")
            self._ft_cards.pop(tid, None)
            self._ft_manager.cancel(tid)

    def _on_file_offer(self, p: dict):
        tid, from_user = p["transfer_id"], p["from"]
        filename = p["filename"]
        size = p["size"]
        mime = p.get("mime", "")
        self._ft_manager.begin_incoming(tid, from_user, filename, size, mime)
        self._current_peer = from_user

        card = FileCard(tid, filename, size, outgoing=False, theme=self._theme)
        card.cancel_requested.connect(self._cancel_transfer)
        self._ft_cards[tid] = card
        self._add_dm_file_card(from_user, card)

        # Auto-accept
        self._bridge.send_frame(T.FILE_ACCEPT, to=from_user, transfer_id=tid)

    def _on_file_accept(self, p: dict):
        tid = p["transfer_id"]
        info = self._ft_manager.outgoing.get(tid)
        if not info:
            return
        sender = info.get("sender")
        if isinstance(sender, DirectFileSender):
            self._direct_file_senders[tid] = sender
            self._pump_direct_file_sender(tid)
            return

        # Backward-compatible fallback for any legacy in-memory transfers that
        # may still exist in state from older code paths.
        from protocol import pack as _pack

        chunks = info["chunks"]
        total = len(chunks)

        def send_chunk(i: int):
            if i >= total:
                self._bridge.send_frame(T.FILE_DONE,
                                        to=info["to"], transfer_id=tid,
                                        sha256=hashlib.sha256(info["data"]).hexdigest())
                if card := self._ft_cards.get(tid):
                    card.set_done()
                self._ft_manager.outgoing.pop(tid, None)
                return
            q = self._bridge._queue
            if q is not None and q.qsize() >= 4:
                QTimer.singleShot(50, lambda: send_chunk(i))
                return
            raw = _pack(T.FILE_CHUNK,
                        to=info["to"], transfer_id=tid,
                        index=i, total=total, data=chunks[i])
            self._bridge.send_raw_frame(raw)
            if card := self._ft_cards.get(tid):
                card.set_progress(int((i + 1) / total * 100))
            QTimer.singleShot(5, lambda: send_chunk(i + 1))

        send_chunk(0)

    def _on_file_reject(self, p: dict):
        tid = p["transfer_id"]
        if card := self._ft_cards.pop(tid, None):
            card.set_error(p.get("reason", "Rejected"))
        self._ft_manager.cancel(tid)
        self._direct_file_senders.pop(tid, None)

    def _on_file_chunk(self, p: dict):
        tid = p["transfer_id"]
        accepted = self._ft_manager.add_chunk(tid, p["index"], p["total"], p["data"])
        if not accepted:
            _log.warning("ignored invalid FILE_CHUNK tid=%s index=%s", tid, p.get("index"))
            return
        pct = int((p["index"] + 1) / max(p["total"], 1) * 100)
        if card := self._ft_cards.get(tid):
            card.set_progress(pct)

    def _on_file_done(self, p: dict):
        tid = p["transfer_id"]
        path = self._ft_manager.finish_incoming(tid, p["sha256"])
        if card := self._ft_cards.pop(tid, None):
            if path:
                card.set_done(save_path=str(path))
            else:
                card.set_error("Checksum mismatch")
        self._direct_file_senders.pop(tid, None)

    def _on_file_error(self, p: dict):
        tid = p["transfer_id"]
        if card := self._ft_cards.pop(tid, None):
            card.set_error(p.get("message", "Transfer error"))
        self._ft_manager.cancel(tid)
        self._direct_file_senders.pop(tid, None)

    def _cancel_transfer(self, tid: str):
        pending_webrtc = self._webrtc_file_pending.pop(tid, None)
        if pending_webrtc is not None:
            peer = pending_webrtc.get("peer")
            _log.info("WEBRTC file cancel session=%s peer=%s", tid, peer or "")
            if peer:
                self._bridge.send_frame(T.WEBRTC_CLOSE, to=peer, session_id=tid)
            self._run_webrtc_task(self._webrtc_transfer.close(tid))
            if card := self._ft_cards.pop(tid, None):
                card.set_error("已取消")
            return

        info = self._ft_manager.outgoing.get(tid)
        if info:
            self._bridge.send_frame(T.FILE_ERROR,
                                    to=info["to"], transfer_id=tid,
                                    message="Cancelled by sender")
        else:
            rec = self._ft_manager.incoming.get(tid)
            if rec:
                self._bridge.send_frame(T.FILE_REJECT,
                                        to=rec["from"], transfer_id=tid,
                                        reason="Cancelled by receiver")
        self._ft_manager.cancel(tid)
        self._ft_cards.pop(tid, None)
        self._direct_file_senders.pop(tid, None)

    def _on_file_room_share(self, p: dict):
        """Server relayed a FILE_ROOM_SHARE announcement — another user is sending a file."""
        tid       = p["transfer_id"]
        filename  = p["filename"]
        size      = int(p["size"])
        mime      = p.get("mime", "")
        from_user = p.get("from_user", "?")
        room_id   = p.get("room_id", "")

        self._ft_manager.begin_incoming(tid, from_user, filename, size, mime)

        if room_id != self._chat.current_room_id:
            return
        card = FileCard(tid, filename, size, outgoing=False, theme=self._theme)
        self._ft_cards[tid] = card
        self._chat.add_file_card(card)

    def _on_file_room_chunk(self, p: dict):
        """Server relayed a FILE_ROOM_CHUNK — accumulate into ft_manager."""
        tid   = p["transfer_id"]
        index = int(p.get("index", 0))
        total = int(p.get("total", 1))
        accepted = self._ft_manager.add_chunk(tid, index, total, p.get("data", ""))
        if not accepted:
            _log.warning("ignored invalid FILE_ROOM_CHUNK tid=%s index=%s", tid, index)
            return
        pct = int((index + 1) / max(total, 1) * 100)
        if card := self._ft_cards.get(tid):
            card.set_progress(pct)

    def _on_file_room_chunk_ack(self, p: dict):
        tid = p["transfer_id"]
        sender_info = self._room_file_senders.get(tid)
        if sender_info is None:
            return
        sender = sender_info["sender"]
        sender.acknowledge(int(p.get("index", -1)))
        if card := self._ft_cards.get(tid):
            card.set_progress(int(sender.acked_chunks / max(sender.total_chunks, 1) * 100))
        self._pump_room_file_sender(tid)

    def _on_file_room_done(self, p: dict):
        """Server relayed FILE_ROOM_DONE — verify checksum, save, update UI."""
        tid       = p["transfer_id"]
        sha       = p["sha256"]
        filename  = p.get("filename", "")
        size      = int(p.get("size", 0))
        mime      = p.get("mime", "")
        from_user = p.get("from_user", "?")
        room_id   = p.get("room_id", "")

        save_path = self._ft_manager.finish_incoming(tid, sha)
        if save_path is None:
            _log.error("FILE_ROOM_DONE checksum mismatch tid=%s", tid)
            if card := self._ft_cards.pop(tid, None):
                card.set_error("校验失败")
            return

        # Replace progress FileCard with appropriate display card
        if card := self._ft_cards.pop(tid, None):
            if mime.startswith("image/"):
                data = save_path.read_bytes()
                new_card = ImageCard(tid, filename, data, outgoing=False)
                new_card.set_done(save_path=str(save_path))
                self._chat.add_file_card(new_card)
                card.setParent(None)
                card.deleteLater()
            elif mime.startswith("video/"):
                new_card = VideoCard(tid, filename, size, outgoing=False)
                new_card.set_done(save_path=str(save_path))
                self._chat.add_file_card(new_card)
                card.setParent(None)
                card.deleteLater()
            else:
                card.set_done(save_path=str(save_path))

        room_name = self._rooms.get(room_id, {}).get("name", room_id)
        self._files_panel.add_file(filename, from_user, room_name,
                                   size, str(save_path))
        if self._bridge and self._bridge.is_connected():
            self._bridge.send_frame(T.FILE_ROOM_RECEIVED,
                                    transfer_id=tid, sha256=sha)

    def _on_file_room_done_ack(self, p: dict):
        tid = p["transfer_id"]
        sender_info = self._room_file_senders.pop(tid, None)
        if card := self._ft_cards.pop(tid, None):
            save_path = None
            if sender_info is not None:
                save_path = str(sender_info["path"])
            card.set_done(save_path=save_path)
            if sender_info is not None:
                room_name = self._rooms.get(sender_info["room_id"], {}).get("name", sender_info["room_id"])
                self._files_panel.add_file(
                    sender_info["filename"],
                    self._username,
                    room_name,
                    sender_info["size"],
                    str(sender_info["path"]),
                )

    def _pump_room_file_sender(self, tid: str):
        if tid not in self._ft_cards:
            self._room_file_senders.pop(tid, None)
            return
        sender_info = self._room_file_senders.get(tid)
        if sender_info is None:
            return
        sender = sender_info["sender"]
        if not self._bridge or not self._bridge.is_connected():
            if c := self._ft_cards.pop(tid, None):
                c.set_error("传输中断")
            self._room_file_senders.pop(tid, None)
            return
        q = self._bridge._queue
        if q is not None and q.qsize() >= 4:
            QTimer.singleShot(50, lambda: self._pump_room_file_sender(tid))
            return
        if sender.ready_to_finish():
            if sender_info["done_sent"]:
                return
            sender_info["done_sent"] = True
            if not self._bridge.send_frame(T.FILE_ROOM_DONE,
                                           transfer_id=tid, sha256=sender.sha256_hex):
                if c := self._ft_cards.pop(tid, None):
                    c.set_error("传输中断")
                self._room_file_senders.pop(tid, None)
            return
        sent_any = False
        for index, total_chunks, payload in sender.next_payloads():
            sent_any = True
            if index % max(1, total_chunks // 10) == 0:
                _log.info("File send progress: %d/%d (%.0f%%)", index, total_chunks, index / total_chunks * 100)
            if not self._bridge.send_raw_frame(pack(T.FILE_ROOM_CHUNK,
                                                    transfer_id=tid, index=index,
                                                    total=total_chunks, data=payload)):
                if c := self._ft_cards.pop(tid, None):
                    c.set_error("传输中断")
                self._room_file_senders.pop(tid, None)
                return
        if sent_any:
            QTimer.singleShot(5, lambda: self._pump_room_file_sender(tid))

    def _pump_direct_file_sender(self, tid: str):
        if tid not in self._ft_cards:
            self._direct_file_senders.pop(tid, None)
            self._ft_manager.outgoing.pop(tid, None)
            return
        info = self._ft_manager.outgoing.get(tid)
        sender = self._direct_file_senders.get(tid)
        if info is None or sender is None:
            return
        if not self._bridge or not self._bridge.is_connected():
            if card := self._ft_cards.pop(tid, None):
                card.set_error("传输中断")
            self._direct_file_senders.pop(tid, None)
            self._ft_manager.outgoing.pop(tid, None)
            return
        q = self._bridge._queue
        if q is not None and q.qsize() >= 4:
            QTimer.singleShot(50, lambda: self._pump_direct_file_sender(tid))
            return
        if sender.ready_to_finish():
            if not self._bridge.send_frame(T.FILE_DONE,
                                           to=info["to"], transfer_id=tid,
                                           sha256=sender.sha256_hex):
                if card := self._ft_cards.pop(tid, None):
                    card.set_error("传输中断")
                self._direct_file_senders.pop(tid, None)
                self._ft_manager.outgoing.pop(tid, None)
                return
            if card := self._ft_cards.get(tid):
                card.set_done()
            self._direct_file_senders.pop(tid, None)
            self._ft_manager.outgoing.pop(tid, None)
            return
        payload = sender.next_payload()
        if payload is None:
            QTimer.singleShot(5, lambda: self._pump_direct_file_sender(tid))
            return
        index, total, data = payload
        if not self._bridge.send_raw_frame(pack(T.FILE_CHUNK,
                                                to=info["to"], transfer_id=tid,
                                                index=index, total=total, data=data)):
            if card := self._ft_cards.pop(tid, None):
                card.set_error("传输中断")
            self._direct_file_senders.pop(tid, None)
            self._ft_manager.outgoing.pop(tid, None)
            return
        if card := self._ft_cards.get(tid):
            card.set_progress(int((index + 1) / max(total, 1) * 100))
        QTimer.singleShot(5, lambda: self._pump_direct_file_sender(tid))

    def _on_file_room_error(self, p: dict):
        tid = p["transfer_id"]
        msg = p.get("message", "上传失败")
        _log.error("FILE_ROOM_ERROR tid=%s: %s", tid, msg)
        if card := self._ft_cards.pop(tid, None):
            card.set_error(msg)
        else:
            QMessageBox.warning(self, "文件上传失败", msg)

    # ── Reply ─────────────────────────────────────────────────────────────────

    @pyqtSlot(str, str, int)
    def _on_reply_requested(self, sender: str, text: str, seq: int):
        self._chat.composer.set_reply(sender, text, seq)

    # ── User actions ──────────────────────────────────────────────────────────

    @pyqtSlot(str)
    def _on_room_selected(self, room_id: str):
        if room_id == self._chat.current_room_id:
            return
        # DM conversations are local — no server JOIN/LEAVE
        if room_id.startswith("@"):
            peer = self._dms.get(room_id, room_id[1:])
            self._current_peer = peer
            self._chat.open_room(room_id, f"@ {peer}", [peer, self._username], False)
            return
        room = self._rooms.get(room_id, {})
        pw = room.get("_password", "")
        # Locked room with no stored password — ask the user
        if room.get("locked") and not pw:
            from PyQt6.QtWidgets import QInputDialog
            dlg = QInputDialog(self)
            dlg.setWindowTitle("加入房间")
            dlg.setLabelText("请输入房间密码：")
            dlg.setTextEchoMode(QLineEdit.EchoMode.Password)
            self._style_dialog(dlg)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            pw = dlg.textValue()
        if self._server_room_id:
            self._on_typing_stop()
            self._implicit_leave = True
            self._bridge.send_frame(T.LEAVE_ROOM)
        # Pre-compute key so ROOM_JOINED handler doesn't wipe it
        self._rooms.setdefault(room_id, {})["_pending_key"] = derive_key(room_id, pw)
        self._rooms[room_id]["_password"] = pw
        self._bridge.send_frame(T.JOIN_ROOM, room_id=room_id, password=pw)

    @pyqtSlot()
    def _on_create_room(self):
        if not self._bridge or not self._bridge._queue:
            _log.error("CREATE_ROOM aborted: not connected to server")
            QMessageBox.critical(self, "未连接", "尚未连接到服务器，无法创建房间。\n请检查服务器地址后重试。")
            return
        dlg = RoomDialog("create", self)
        self._style_dialog(dlg)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        v   = dlg.values()
        key = derive_key("__pending__", v["password"])
        self._rooms.setdefault("__pending__", {})["_pending_key"] = key
        if v["password"]:
            self._rooms["__pending__"]["_pending_pw"] = v["password"]
        _log.info("CREATE_ROOM request: name=%r locked=%s", v["name"], bool(v["password"]))
        if self._server_room_id:
            self._implicit_leave = True
        self._bridge.send_frame(T.CREATE_ROOM, name=v["name"], password=v["password"])

    @pyqtSlot()
    def _on_search_rooms(self):
        if not self._bridge or not self._bridge._queue:
            QMessageBox.critical(self, "未连接", "尚未连接到服务器。\n请检查服务器地址后重试。")
            return
        # Refresh room list before opening dialog
        self._bridge.send_frame(T.LIST_ROOMS)
        dlg = RoomSearchDialog(self._rooms, self._username, self)
        self._style_dialog(dlg)
        dlg.join_requested.connect(self._on_join_from_search)
        dlg.exec()

    @pyqtSlot(str, str)
    def _on_join_from_search(self, room_id: str, password: str):
        if not self._bridge or not self._bridge._queue:
            return
        key = derive_key(room_id, password)
        self._rooms.setdefault(room_id, {})["_pending_key"] = key
        self._rooms[room_id]["_password"] = password
        if self._server_room_id:
            self._implicit_leave = True
        self._bridge.send_frame(T.JOIN_ROOM, room_id=room_id, password=password)

    @pyqtSlot(str, str)
    def _on_room_rename(self, room_id: str, new_name: str):
        if self._bridge:
            self._bridge.send_frame(T.SET_ROOM_NAME, room_id=room_id, name=new_name)

    @pyqtSlot(str, str)
    def _on_room_icon_change(self, room_id: str, icon: str):
        if self._bridge:
            self._bridge.send_frame(T.SET_ROOM_ICON, room_id=room_id, icon=icon)

    @pyqtSlot(str)
    def _on_send_message(self, text: str):
        rid = self._chat.current_room_id
        if not rid:
            return

        # DM conversation — route directly to peer
        if rid.startswith("@"):
            peer = self._dms.get(rid, rid[1:])
            self._msg_counter += 1
            client_mid = self._msg_counter
            self._bridge.send_frame(T.SEND_DM, to=peer, text=text,
                                    client_mid=client_mid)
            bubble = self._chat.add_message(
                self._username, text, time.time(), outgoing=True, seq=0)
            if bubble:
                self._pending_bubbles[client_mid] = bubble
            self._conv.set_preview(rid, f"You: {text}", time.time())
            return

        key   = self._rooms.get(rid, {}).get("key")
        reply = self._chat.composer.pending_reply

        if key:
            enc_text  = encrypt(key, text)
            encrypted = True
        else:
            enc_text  = text
            encrypted = False

        self._msg_counter += 1
        client_mid = self._msg_counter

        kwargs: dict = dict(text=enc_text, encrypted=encrypted, client_mid=client_mid)
        if reply:
            kwargs["reply_to"] = reply
            self._chat.composer.clear_reply()

        self._bridge.send_frame(T.SEND_MSG, **kwargs)

        # Show locally; bubble tracked by client_mid until SEND_ACK arrives
        bubble = self._chat.add_message(
            self._username, text, time.time(),
            outgoing=True, seq=0, quote=reply
        )
        if bubble:
            self._pending_bubbles[client_mid] = bubble

        self._conv.set_preview(rid, f"You: {text}", time.time())

        # Reset typing state after send
        if self._is_typing:
            self._is_typing = False
            self._typing_timer.stop()

    _AVATAR_PATH = pathlib.Path.home() / ".p2pchat_avatar.png"

    def _load_avatar(self):
        if self._AVATAR_PATH.exists():
            px = QPixmap(str(self._AVATAR_PATH))
            if not px.isNull():
                self._rail.set_avatar_pixmap(px)
                self._chat.set_own_avatar(self._username, px)

    def _send_avatar(self):
        if not self._AVATAR_PATH.exists() or not self._bridge:
            return
        try:
            import base64
            data = base64.b64encode(self._AVATAR_PATH.read_bytes()).decode()
            self._bridge.send_frame(T.SET_AVATAR, data=data)
        except Exception as exc:
            _log.warning("_send_avatar failed: %s", exc)

    @pyqtSlot()
    def _on_change_avatar(self):
        from PyQt6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            self, "选择头像图片", "",
            "图片文件 (*.png *.jpg *.jpeg *.webp *.bmp)"
        )
        if not path:
            return
        px = QPixmap(path)
        if px.isNull():
            return
        # Save as square PNG at 128×128
        scaled = px.scaled(128, 128,
                           Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                           Qt.TransformationMode.SmoothTransformation)
        scaled.save(str(self._AVATAR_PATH), "PNG")
        self._rail.set_avatar_pixmap(scaled)
        self._chat.set_own_avatar(self._username, scaled)
        self._send_avatar()

    @pyqtSlot()
    def _open_settings(self):
        dlg = SettingsDialog(self._server_url, self._username, self._theme,
                             str(self._ft_manager._dir), self._close_pref,
                             self._load_ice_servers_text(), self)
        self._style_dialog(dlg)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        v = dlg.values()
        changed_server = v["server_url"] != self._server_url
        self._server_url = v["server_url"]
        self._username   = v["username"] or self._username
        self._theme      = v["theme"]

        new_dl = pathlib.Path(v["download_dir"]) if v.get("download_dir") else None
        if new_dl and new_dl != self._ft_manager._dir:
            new_dl.mkdir(parents=True, exist_ok=True)
            self._ft_manager._dir = new_dl
            self._save_download_dir(new_dl)

        new_pref = v.get("close_pref")   # None | "tray" | "quit"
        if new_pref != self._close_pref:
            self._save_close_pref(new_pref)

        ice_text = v.get("ice_servers", "")
        if ice_text != self._load_ice_servers_text():
            self._save_ice_servers_text(ice_text)
            self._ice_servers = load_ice_servers({"BEAM_ICE_SERVERS": ice_text})
            self._webrtc_transfer = self._new_webrtc_transfer(self._ft_manager._dir,
                                                              self._ice_servers)

        self._rail.set_username(self._username)
        self._apply_theme()

        if changed_server:
            self._connect()
        else:
            self._bridge.send_frame(T.SET_NAME, name=self._username)

    # ── System tray ───────────────────────────────────────────────────────────

    _PREF_FILE   = pathlib.Path.home() / ".beamchat" / "close_pref.txt"
    _DL_DIR_FILE = pathlib.Path.home() / ".beamchat" / "download_dir.txt"
    _ICE_FILE    = pathlib.Path.home() / ".beamchat" / "ice_servers.txt"
    _DEFAULT_DL  = pathlib.Path.home() / "AppData" / "Local" / "BeamChat" / "downloads"

    def _load_download_dir(self) -> pathlib.Path:
        try:
            p = pathlib.Path(self._DL_DIR_FILE.read_text().strip())
            if p.is_absolute():
                return p
        except Exception:
            pass
        return self._DEFAULT_DL

    def _save_download_dir(self, path: pathlib.Path):
        try:
            self._DL_DIR_FILE.parent.mkdir(parents=True, exist_ok=True)
            self._DL_DIR_FILE.write_text(str(path))
        except Exception:
            pass

    def _load_close_pref(self) -> str | None:
        try:
            v = self._PREF_FILE.read_text().strip()
            return v if v in ("tray", "quit") else None
        except Exception:
            return None

    def _save_close_pref(self, pref: str | None):
        self._close_pref = pref
        try:
            self._PREF_FILE.parent.mkdir(parents=True, exist_ok=True)
            self._PREF_FILE.write_text(pref or "")
        except Exception:
            pass

    def _load_ice_servers_text(self) -> str:
        try:
            return self._ICE_FILE.read_text(encoding="utf-8").strip()
        except Exception:
            return ""

    def _load_ice_servers(self) -> list[dict]:
        raw = self._load_ice_servers_text()
        if raw:
            return load_ice_servers({"BEAM_ICE_SERVERS": raw})
        return load_ice_servers()

    def _save_ice_servers_text(self, value: str):
        try:
            self._ICE_FILE.parent.mkdir(parents=True, exist_ok=True)
            self._ICE_FILE.write_text(value.strip(), encoding="utf-8")
        except Exception:
            pass

    def _setup_tray(self):
        from PyQt6.QtWidgets import QSystemTrayIcon
        from PyQt6.QtGui import QIcon
        icon_path = pathlib.Path(__file__).parent.parent / "assets" / "icon.png"
        if not icon_path.exists() and getattr(sys, "frozen", False):
            icon_path = pathlib.Path(sys._MEIPASS) / "assets" / "icon.png"
        icon = QIcon(str(icon_path)) if icon_path.exists() else self.windowIcon()
        self._tray = QSystemTrayIcon(icon, self)
        self._tray.setToolTip("Beam — P2P Chat")
        menu = QMenu()
        show_act = menu.addAction("显示窗口")
        show_act.triggered.connect(self._tray_show)
        menu.addSeparator()
        quit_act = menu.addAction("退出")
        quit_act.triggered.connect(self._tray_quit)
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()
        # Timer for flashing tray icon when there are unread messages
        self._tray_flash_timer = QTimer(self)
        self._tray_flash_timer.setInterval(600)
        self._tray_flash_timer.timeout.connect(self._tray_flash_tick)
        self._tray_icon_visible = True
        self._tray_icon_obj = icon

    def _tray_show(self):
        self._tray_flash_timer.stop()
        self._tray.setIcon(self._tray_icon_obj)
        self._tray_msgs.clear()
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _tray_quit(self):
        self._tray_flash_timer.stop()
        self._tray.hide()
        self._do_quit()

    def _on_tray_activated(self, reason):
        from PyQt6.QtWidgets import QSystemTrayIcon
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._tray_show()

    def _tray_flash_tick(self):
        from PyQt6.QtGui import QIcon
        if self._tray_icon_visible:
            self._tray.setIcon(QIcon())   # blank frame
        else:
            self._tray.setIcon(self._tray_icon_obj)
        self._tray_icon_visible = not self._tray_icon_visible

    def _notify_tray(self, source: str, text: str):
        """Called on new message while window is hidden or unfocused."""
        self._tray_msgs.append((source, text))
        # Build tooltip showing last 5 unread
        lines = [f"{s}: {t[:30]}" for s, t in self._tray_msgs[-5:]]
        self._tray.setToolTip("Beam — 新消息\n" + "\n".join(lines))
        if not self._tray_flash_timer.isActive():
            self._tray_flash_timer.start()

    # ── Auto-update ───────────────────────────────────────────────────────────

    def _check_update_bg(self):
        import threading
        def _worker():
            try:
                from updater import check_update
                ver, url, _ = check_update()
                if ver and url:
                    self._update_available_ver = ver
                    self._update_available_url = url
                    # Marshal back to GUI thread
                    QTimer.singleShot(0, lambda: self._show_update_bar(ver))
            except Exception:
                pass
        threading.Thread(target=_worker, daemon=True).start()

    def _show_update_bar(self, ver: str):
        from version import __version__
        self._update_lbl.setText(f"新版本 v{ver} 可用 (当前 v{__version__})")
        self._update_bar.show()

    @pyqtSlot()
    def _on_do_update(self):
        url = getattr(self, "_update_available_url", None)
        if not url:
            return
        from updater import UpdateDownloader, apply_update
        dlg = QDialog(self)
        dlg.setObjectName("Dialog")
        dlg.setWindowTitle("正在下载更新…")
        dlg.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        dlg.setFixedWidth(340)
        self._style_dialog(dlg)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(10)
        lay.addWidget(QLabel(f"下载 BeamChat v{self._update_available_ver}…"))
        from PyQt6.QtWidgets import QProgressBar
        bar = QProgressBar()
        bar.setObjectName("UpdateProgress")
        bar.setRange(0, 100)
        bar.setValue(0)
        bar.setTextVisible(False)
        bar.setFixedHeight(6)
        lay.addWidget(bar)
        status = QLabel("准备中…")
        status.setObjectName("FileCardStatus")
        lay.addWidget(status)

        result = {"path": None}

        dl = UpdateDownloader(url, self)
        dl.progress.connect(bar.setValue)
        dl.progress.connect(lambda p: status.setText(f"{p}%"))
        def _on_done(tmp_path: str):
            result["path"] = tmp_path
            dlg.accept()          # exit sub-event-loop only; quit happens below
        def _on_fail(msg: str):
            status.setText(f"下载失败: {msg}")
        dl.finished.connect(_on_done)
        dl.failed.connect(_on_fail)
        dl.start()
        dlg.exec()                # blocks here until _on_done / user closes

        # Now we're back in the main event loop — safe to quit
        if result["path"]:
            apply_update(result["path"])
            self._close_pref = "quit"   # skip the "minimize/quit?" dialog
            self._do_quit()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _style_dialog(self, dlg: QDialog):
        dlg.setStyleSheet(self.styleSheet())

    def closeEvent(self, event):
        pref = self._close_pref
        if pref is None:
            dlg = QDialog(self)
            dlg.setObjectName("Dialog")
            dlg.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
            dlg.setModal(True)
            dlg.setMinimumWidth(300)
            self._style_dialog(dlg)
            lay = QVBoxLayout(dlg)
            lay.setContentsMargins(24, 20, 24, 20)
            lay.setSpacing(14)

            title_row = QHBoxLayout()
            title_row.addWidget(_lbl("关闭 Beam", "DialogTitle"))
            title_row.addStretch()
            x_btn = _btn("✕", "DialogCloseBtn")
            x_btn.setFixedSize(28, 28)
            x_btn.clicked.connect(dlg.reject)
            title_row.addWidget(x_btn)
            lay.addLayout(title_row)
            lay.addWidget(_lbl("请选择关闭方式：", "FormLabel"))

            tray_btn = _btn("最小化到托盘", "BtnPrimary")
            tray_btn.setMinimumWidth(160)
            quit_btn = _btn("退出程序", "BtnGhost")
            quit_btn.setMinimumWidth(160)
            lay.addWidget(tray_btn)
            lay.addWidget(quit_btn)

            remember = QCheckBox("记住我的选择")
            remember.setObjectName("CloseRemember")
            lay.addWidget(remember)

            chosen = {"v": None}
            def _tray():
                chosen["v"] = "tray"
                dlg.accept()
            def _quit():
                chosen["v"] = "quit"
                dlg.accept()
            tray_btn.clicked.connect(_tray)
            quit_btn.clicked.connect(_quit)
            dlg.exec()
            if chosen["v"] is None:
                event.ignore()
                return
            if remember.isChecked():
                self._save_close_pref(chosen["v"])
            pref = chosen["v"]

        if pref == "tray":
            event.ignore()
            self.hide()
        else:
            self._on_typing_stop()
            if self._bridge:
                self._bridge.close()
                self._bridge.wait(1500)
            self._tray.hide()
            event.accept()

    def _do_quit(self):
        self._on_typing_stop()
        if self._bridge:
            self._bridge.close()
            self._bridge.wait(1500)
        QApplication.quit()
