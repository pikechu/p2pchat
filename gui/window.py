"""Main application window — Beam P2P Chat desktop client."""

import json
import pathlib
import time
from datetime import datetime

from file_transfer import FileTransferManager, file_sha256

from PyQt6.QtCore import Qt, QSize, QTimer, pyqtSlot, pyqtSignal
from PyQt6.QtGui import (
    QColor, QIcon, QPainter, QPainterPath, QLinearGradient, QBrush,
    QAction,
)
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QLabel,
    QPushButton, QScrollArea, QLineEdit, QTextEdit,
    QDialog, QDialogButtonBox, QFormLayout, QSizePolicy,
    QFrame, QMessageBox, QMenu, QToolButton, QApplication,
    QCheckBox, QComboBox, QStackedWidget, QListWidget,
)

from protocol import T, unpack
from crypto import derive_key, encrypt, decrypt
from .bridge import WSBridge
from .theme import make_qss, TOKENS
from .widgets import (
    Avatar, StatusDot, BubbleWidget, SysMsgWidget,
    DayMarkWidget, ConvRowWidget, TypingWidget, EmojiPanel, FileCard,
)


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
    def __init__(self, server_url: str, username: str, theme: str, parent=None):
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

        lay.addLayout(form)

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

    def values(self) -> dict:
        return {
            "server_url": self._url.text().strip(),
            "username":   self._user.text().strip(),
            "theme":      self._theme.currentText(),
        }


# ── Rail (left icon bar) ──────────────────────────────────────────────────────

class Rail(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Rail")
        self.setFixedWidth(56)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 12, 8, 12)
        lay.setSpacing(4)
        lay.setAlignment(Qt.AlignmentFlag.AlignTop)

        mark = QLabel("B")
        mark.setObjectName("RailBtn")
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setFixedSize(40, 40)
        mark.setStyleSheet(
            "background: qlineargradient(x1:0,y1:0,x2:1,y2:1,"
            " stop:0 #0088cc, stop:1 #5fc4ee);"
            "color:white; font-size:16px; font-weight:700;"
            "border-radius:8px;"
        )
        lay.addWidget(mark, alignment=Qt.AlignmentFlag.AlignHCenter)
        lay.addSpacing(8)

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
        lay.addWidget(self._avatar, alignment=Qt.AlignmentFlag.AlignHCenter)

    def set_active(self, key: str):
        for k, btn in self._btns.items():
            btn.setProperty("active", k == key)
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def set_username(self, name: str):
        self._avatar.set_name(name)


# ── Conversation list panel ───────────────────────────────────────────────────

class ConvPanel(QWidget):
    room_selected = pyqtSignal(str)   # room_id
    create_room   = pyqtSignal()
    join_room     = pyqtSignal()

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

        join_btn = _btn("⊕", "NewRoomBtn")
        join_btn.setFixedSize(28, 28)
        join_btn.setToolTip("Join room")
        join_btn.clicked.connect(self.join_room)
        title_row.addWidget(join_btn)

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

    def upsert_room(self, room_id: str, name: str, creator: str,
                    members: int = 0, locked: bool = False,
                    unread: int = 0):
        if room_id in self._rows:
            return
        row = ConvRowWidget(room_id, name, creator, members, locked, unread,
                            self._theme, conn_state="ok")
        row.clicked.connect(self._on_row_clicked)
        self._list_lay.insertWidget(self._list_lay.count() - 1, row)
        self._rows[room_id] = row

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
        self._active = room_id

    def set_preview(self, room_id: str, text: str, ts: float = 0):
        if row := self._rows.get(room_id):
            row.set_preview(text, ts)

    def set_conn_state(self, room_id: str, state: str):
        if row := self._rows.get(room_id):
            row.set_conn_state(state)

    def _on_row_clicked(self, room_id: str):
        self.set_active(room_id)
        self.room_selected.emit(room_id)

    def _filter(self, query: str):
        q = query.lower()
        for rid, row in self._rows.items():
            name = row._name_lbl.text().lower()
            row.setVisible(not q or q in rid.lower() or q in name)


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

        for icon in ("🔍", "⋯"):
            btn = QPushButton(icon)
            btn.setObjectName("HeaderBtn")
            btn.setFixedSize(32, 32)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            lay.addWidget(btn)

    def update_room(self, name: str, members: list[str], locked: bool,
                    conn_state: str = "ok"):
        self._avatar.set_name(name)
        self._name_lbl.setText(("🔒 " if locked else "") + name)
        count = len(members)
        self._sub_lbl.setText(f"{count} member{'s' if count != 1 else ''}")
        self._status_dot.set_state(conn_state)

    def set_conn_state(self, state: str):
        self._status_dot.set_state(state)


# ── Messages area ─────────────────────────────────────────────────────────────

class MessagesArea(QScrollArea):
    reply_requested = pyqtSignal(str, str, int)   # sender, text, seq

    def __init__(self, theme: str = "light", parent=None):
        super().__init__(parent)
        self._theme = theme
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

        show_sender = sender != self._last_sender and not outgoing
        bubble = BubbleWidget(sender, text, ts, outgoing, show_sender,
                              self._theme, seq=seq, quote=quote)
        bubble.reply_requested.connect(self.reply_requested)
        align = Qt.AlignmentFlag.AlignRight if outgoing else Qt.AlignmentFlag.AlignLeft
        self._lay.insertWidget(self._lay.count() - 1, bubble, alignment=align)
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
        attach.clicked.connect(self._pick_file)
        inner_lay.addWidget(attach)

        self._input = QLineEdit()
        self._input.setObjectName("ComposerInput")
        self._input.setPlaceholderText("Message…")
        self._input.returnPressed.connect(self._on_send)
        self._input.textChanged.connect(self._on_text_changed)
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

    def _pick_file(self):
        from PyQt6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            self, "Send File", str(pathlib.Path.home()),
            "All Files (*)"
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
        sub = _lbl("Use + to create a room or ⊕ to join one with a room ID", "EmptyDesc")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub.setWordWrap(True)
        lay.addWidget(sub)


# ── Full chat panel ───────────────────────────────────────────────────────────

class ChatPanel(QWidget):
    reply_requested = pyqtSignal(str, str, int)   # sender, text, seq

    def __init__(self, theme: str = "light", parent=None):
        super().__init__(parent)
        self._theme   = theme
        self._room_id: str | None = None
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
        self._msgs    = MessagesArea(theme=theme)
        self._msgs.reply_requested.connect(self.reply_requested)

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

        chat_lay.addWidget(self._header)
        chat_lay.addWidget(self._msgs)
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

    def _on_typing_start(self):
        if self._typing_started_cb:
            self._typing_started_cb()

    def _on_typing_stop(self):
        if self._typing_stopped_cb:
            self._typing_stopped_cb()

    def _toggle_emoji(self):
        self._emoji_panel.setVisible(not self._emoji_panel.isVisible())

    def open_room(self, room_id: str, name: str,
                  members: list[str], locked: bool, conn_state: str = "ok"):
        self._room_id = room_id
        self._header.update_room(name, members, locked, conn_state)
        self._msgs.clear()
        self._composer.set_enabled(True)
        self._composer.clear_reply()
        self._typing.hide_typing()
        self._emoji_panel.hide()
        self._stack.setCurrentWidget(self._chat_widget)

    def close_room(self):
        self._room_id = None
        self._composer.set_enabled(False)
        self._composer.clear_reply()
        self._typing.hide_typing()
        self._emoji_panel.hide()
        self._stack.setCurrentWidget(self._empty)

    def add_message(self, sender: str, text: str, ts: float,
                    outgoing: bool, seq: int = 0,
                    quote: dict | None = None) -> BubbleWidget:
        return self._msgs.add_message(sender, text, ts, outgoing, seq=seq, quote=quote)

    def add_sys(self, text: str):
        self._msgs.add_sys_msg(text)

    def add_file_card(self, card):
        self._msgs.add_file_card(card)

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

        # Bubble tracking for delivery receipts
        # client_mid (local int) → BubbleWidget, moved to seq key after SEND_ACK
        self._pending_bubbles: dict[int, BubbleWidget] = {}
        self._seq_bubbles:     dict[int, BubbleWidget] = {}
        self._msg_counter = 0

        # DM state: "@peer" → peer username
        self._dms: dict[str, str] = {}

        # File transfer state
        downloads = pathlib.Path.home() / "Downloads" / "P2PChat"
        self._ft_manager = FileTransferManager(downloads_dir=downloads)
        self._ft_cards: dict[str, "FileCard"] = {}
        self._current_peer: str = ""

        # Typing state
        self._is_typing = False
        self._typing_timer = QTimer(self)
        self._typing_timer.setSingleShot(True)
        self._typing_timer.setInterval(3000)
        self._typing_timer.timeout.connect(self._on_typing_stop)

        self._build_ui()
        self._apply_theme()
        self.setWindowTitle("Beam — P2P Chat")
        self.resize(1100, 720)
        self.setMinimumSize(800, 560)

        QTimer.singleShot(100, self._connect)

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
        self._rail.set_active("chats")
        root.addWidget(self._rail)

        self._conv = ConvPanel(self._theme)
        self._conv.room_selected.connect(self._on_room_selected)
        self._conv.create_room.connect(self._on_create_room)
        self._conv.join_room.connect(self._on_join_room)
        root.addWidget(self._conv)

        self._chat = ChatPanel(self._theme)
        self._chat.send_message.connect(self._on_send_message)
        self._chat.reply_requested.connect(self._on_reply_requested)
        self._chat.set_typing_callbacks(self._on_typing_start, self._on_typing_stop)
        self._chat.composer.file_selected.connect(self._start_file_send)
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
        self._bridge.start()

    @pyqtSlot()
    def _on_connected(self):
        self.statusBar().showMessage(f"Connected  ·  {self._server_url}", 4000)
        self._bridge.send_frame(T.SET_NAME, name=self._username)
        self._bridge.send_frame(T.LIST_ROOMS)

    @pyqtSlot(str)
    def _on_disconnected(self, reason: str):
        self.statusBar().showMessage(f"Disconnected: {reason}")
        self._chat.close_room()
        # Mark all conv rows offline
        for rid in self._rooms:
            self._conv.set_conn_state(rid, "offline")
            self._chat.set_conn_state("offline")

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
            self.statusBar().showMessage(f"Frame error ({mtype}): {exc}", 6000)
            traceback.print_exc()

    def _dispatch_frame(self, mtype: str, payload: dict, ts: float):

        if mtype == T.WELCOME:
            pass

        elif mtype == T.SYSTEM:
            self.statusBar().showMessage(payload.get("message", ""), 3000)

        elif mtype == T.ERROR:
            self.statusBar().showMessage("⚠  " + payload.get("message", ""), 5000)

        elif mtype == T.ROOM_CREATED:
            rid    = payload["room_id"]
            name   = payload["name"]
            locked = payload.get("locked", False)
            pending = self._rooms.pop("__pending__", {})
            pw  = pending.get("_pending_pw", "")
            key = derive_key(rid, pw) if pw else None
            self._rooms[rid] = {"name": name, "members": [self._username],
                                "locked": locked, "key": key}
            self._conv.upsert_room(rid, name, self._username, 1, locked)
            self._conv.set_active(rid)
            self._conv.set_conn_state(rid, "ok")
            self._chat.open_room(rid, name, [self._username], locked)

        elif mtype == T.ROOM_JOINED:
            rid     = payload["room_id"]
            name    = payload["name"]
            members = payload.get("members", [])
            locked  = payload.get("locked", False)
            key     = self._rooms.get(rid, {}).get("_pending_key")
            self._rooms[rid] = {"name": name, "members": members,
                                "locked": locked, "key": key}
            self._conv.upsert_room(rid, name, self._username, len(members), locked)
            self._conv.set_active(rid)
            self._conv.set_conn_state(rid, "ok")
            self._chat.open_room(rid, name, members, locked)
            # Track first non-self member as file transfer peer
            others = [m for m in members if m != self._username]
            self._current_peer = others[0] if others else ""

        elif mtype == T.ROOM_LEFT:
            rid = self._chat.current_room_id
            if rid:
                self._rooms.pop(rid, None)
                self._conv.remove_room(rid)
                self._chat.close_room()

        elif mtype == T.USER_JOINED:
            uname = payload.get("username", "")
            rid   = payload.get("room_id", "")
            if rid in self._rooms:
                members = self._rooms[rid].get("members", [])
                if uname not in members:
                    members.append(uname)
            if rid == self._chat.current_room_id:
                self._chat.add_sys(f"{uname} joined")

        elif mtype == T.USER_LEFT:
            uname = payload.get("username", "")
            rid   = payload.get("room_id", "")
            if rid in self._rooms:
                self._rooms[rid]["members"] = [
                    m for m in self._rooms[rid].get("members", []) if m != uname
                ]
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
                    # Also decrypt reply_to.text if it was encrypted
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

        elif mtype == T.ROOM_LIST:
            for r in payload.get("rooms", []):
                self._conv.upsert_room(
                    r["id"], r["name"], r.get("creator", ""),
                    r.get("members", 0), r.get("locked", False)
                )

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

        elif mtype == T.DM_ACK:
            client_mid = payload.get("client_mid", -1)
            if client_mid in self._pending_bubbles:
                bubble = self._pending_bubbles.pop(client_mid)
                bubble.set_status("sent")

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
            lay.addWidget(_lbl("No other users online right now.", "EmptyDesc"))
        else:
            list_w = QListWidget()
            list_w.addItems(users)
            list_w.itemDoubleClicked.connect(
                lambda item: (self._start_dm(item.text()), dlg.accept())
            )
            lay.addWidget(list_w)

            hint = _lbl("Double-click to open a direct message", "FormLabel")
            hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lay.addWidget(hint)

        close_btn = _btn("Close", "BtnGhost")
        close_btn.clicked.connect(dlg.reject)
        lay.addWidget(close_btn)

        self._style_dialog(dlg)
        dlg.exec()

    # ── File transfer ─────────────────────────────────────────────────────────

    def _start_file_send(self, path: str):
        if not self._current_peer:
            self.statusBar().showMessage("No peer to send file to", 3000)
            return
        data = pathlib.Path(path).read_bytes()
        filename = pathlib.Path(path).name
        tid = self._ft_manager.register_outgoing(self._current_peer, filename, data)
        info = self._ft_manager.outgoing[tid]

        card = FileCard(tid, filename, len(data), outgoing=True, theme=self._theme)
        card.cancel_requested.connect(self._cancel_transfer)
        self._ft_cards[tid] = card
        self._chat.add_file_card(card)

        self._bridge.send_frame(
            T.FILE_OFFER,
            to=self._current_peer,
            transfer_id=tid,
            filename=filename,
            size=len(data),
            mime=info["mime"],
        )

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
        self._chat.add_file_card(card)

        # Auto-accept
        self._bridge.send_frame(T.FILE_ACCEPT, to=from_user, transfer_id=tid)

    def _on_file_accept(self, p: dict):
        from protocol import pack as _pack
        tid = p["transfer_id"]
        info = self._ft_manager.outgoing.get(tid)
        if not info:
            return
        chunks = info["chunks"]
        total = len(chunks)

        def send_chunk(i: int):
            if i >= total:
                # All chunks sent — send FILE_DONE
                self._bridge.send_frame(T.FILE_DONE,
                                        to=info["to"], transfer_id=tid,
                                        sha256=file_sha256(info["data"]))
                if card := self._ft_cards.get(tid):
                    card.set_done()
                self._ft_manager.outgoing.pop(tid, None)
                return
            raw = _pack(T.FILE_CHUNK,
                        to=info["to"], transfer_id=tid,
                        index=i, total=total, data=chunks[i])
            self._bridge.send_raw_frame(raw)
            if card := self._ft_cards.get(tid):
                card.set_progress(int((i + 1) / total * 100))
            QTimer.singleShot(0, lambda: send_chunk(i + 1))

        send_chunk(0)

    def _on_file_reject(self, p: dict):
        tid = p["transfer_id"]
        if card := self._ft_cards.pop(tid, None):
            card.set_error(p.get("reason", "Rejected"))
        self._ft_manager.cancel(tid)

    def _on_file_chunk(self, p: dict):
        tid = p["transfer_id"]
        self._ft_manager.add_chunk(tid, p["index"], p["total"], p["data"])
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

    def _on_file_error(self, p: dict):
        tid = p["transfer_id"]
        if card := self._ft_cards.pop(tid, None):
            card.set_error(p.get("message", "Transfer error"))
        self._ft_manager.cancel(tid)

    def _cancel_transfer(self, tid: str):
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
        if self._chat.current_room_id and not self._chat.current_room_id.startswith("@"):
            self._on_typing_stop()
            self._bridge.send_frame(T.LEAVE_ROOM)
        self._bridge.send_frame(T.JOIN_ROOM, room_id=room_id)

    @pyqtSlot()
    def _on_create_room(self):
        if not self._bridge or not self._bridge._queue:
            self.statusBar().showMessage("⚠  Not connected to server", 4000)
            return
        dlg = RoomDialog("create", self)
        self._style_dialog(dlg)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        v   = dlg.values()
        key = derive_key("__pending__", v["password"]) if v["password"] else None
        self._rooms.setdefault("__pending__", {})["_pending_key"] = key
        if v["password"]:
            self._rooms["__pending__"]["_pending_pw"] = v["password"]
        self._bridge.send_frame(T.CREATE_ROOM, name=v["name"], password=v["password"])

    @pyqtSlot()
    def _on_join_room(self):
        if not self._bridge or not self._bridge._queue:
            self.statusBar().showMessage("⚠  Not connected to server", 4000)
            return
        dlg = RoomDialog("join", self)
        self._style_dialog(dlg)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        v   = dlg.values()
        rid = v["room_id"]
        key = derive_key(rid, v["password"]) if v["password"] else None
        self._rooms.setdefault(rid, {})["_pending_key"] = key
        self._bridge.send_frame(T.JOIN_ROOM, room_id=rid, password=v["password"])

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
        self._pending_bubbles[client_mid] = bubble

        self._conv.set_preview(rid, f"You: {text}", time.time())

        # Reset typing state after send
        if self._is_typing:
            self._is_typing = False
            self._typing_timer.stop()

    @pyqtSlot()
    def _open_settings(self):
        dlg = SettingsDialog(self._server_url, self._username, self._theme, self)
        self._style_dialog(dlg)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        v = dlg.values()
        changed_server = v["server_url"] != self._server_url
        self._server_url = v["server_url"]
        self._username   = v["username"] or self._username
        self._theme      = v["theme"]

        self._rail.set_username(self._username)
        self._apply_theme()

        if changed_server:
            self._connect()
        else:
            self._bridge.send_frame(T.SET_NAME, name=self._username)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _style_dialog(self, dlg: QDialog):
        dlg.setStyleSheet(self.styleSheet())

    def closeEvent(self, event):
        self._on_typing_stop()
        if self._bridge:
            self._bridge.close()
            self._bridge.wait(1500)
        event.accept()
