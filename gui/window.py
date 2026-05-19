"""Main application window — Beam P2P Chat desktop client."""

import json
import time
from datetime import datetime

from PyQt6.QtCore import Qt, QSize, QTimer, pyqtSlot
from PyQt6.QtGui import (
    QColor, QIcon, QPainter, QPainterPath, QLinearGradient, QBrush,
    QAction,
)
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QLabel,
    QPushButton, QScrollArea, QLineEdit, QTextEdit,
    QDialog, QDialogButtonBox, QFormLayout, QSizePolicy,
    QFrame, QMessageBox, QMenu, QToolButton, QApplication,
    QCheckBox, QComboBox, QStackedWidget,
)

from protocol import T, unpack
from crypto import derive_key, encrypt, decrypt
from .bridge import WSBridge
from .theme import make_qss, TOKENS
from .widgets import (
    Avatar, StatusDot, BubbleWidget, SysMsgWidget,
    DayMarkWidget, ConvRowWidget,
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

        # Logo mark
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

        # ── Header
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

        # ── Scrollable room list
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
        row = ConvRowWidget(room_id, name, creator, members, locked, unread, self._theme)
        row.clicked.connect(self._on_row_clicked)
        # insert before the trailing stretch
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

    def _on_row_clicked(self, room_id: str):
        self.set_active(room_id)
        self.room_selected.emit(room_id)

    def _filter(self, query: str):
        q = query.lower()
        for rid, row in self._rows.items():
            row.setVisible(not q or q in rid.lower())


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

    def update_room(self, name: str, members: list[str], locked: bool):
        self._avatar.set_name(name)
        self._name_lbl.setText(("🔒 " if locked else "") + name)
        count = len(members)
        self._sub_lbl.setText(f"{count} member{'s' if count != 1 else ''}")
        self._status_dot.set_state("ok")


# ── Messages area ─────────────────────────────────────────────────────────────

class MessagesArea(QScrollArea):
    def __init__(self, parent=None):
        super().__init__(parent)
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
        self._last_day: str | None = None

    def add_message(self, sender: str, text: str, ts: float,
                    outgoing: bool = False, theme: str = "light"):
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
        bubble = BubbleWidget(sender, text, ts, outgoing, show_sender, theme)
        align  = Qt.AlignmentFlag.AlignRight if outgoing else Qt.AlignmentFlag.AlignLeft
        self._lay.insertWidget(self._lay.count() - 1, bubble, alignment=align)
        self._last_sender = sender
        # Scroll to bottom
        QTimer.singleShot(50, lambda: self.verticalScrollBar().setValue(
            self.verticalScrollBar().maximum()))

    def add_sys_msg(self, text: str):
        self._lay.insertWidget(self._lay.count() - 1,
                               SysMsgWidget(text),
                               alignment=Qt.AlignmentFlag.AlignHCenter)
        self._last_sender = None

    def clear(self):
        while self._lay.count() > 1:
            item = self._lay.takeAt(0)
            if w := item.widget():
                w.deleteLater()
        self._last_sender = None
        self._last_day    = None


# ── Composer ──────────────────────────────────────────────────────────────────

class Composer(QWidget):
    send_message = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ComposerBar")

        outer = QHBoxLayout(self)
        outer.setContentsMargins(16, 10, 16, 12)
        outer.setSpacing(0)

        inner_frame = QFrame()
        inner_frame.setObjectName("ComposerInner")
        inner_lay = QHBoxLayout(inner_frame)
        inner_lay.setContentsMargins(8, 4, 4, 4)
        inner_lay.setSpacing(4)

        attach = QPushButton("📎")
        attach.setObjectName("ComposerIconBtn")
        inner_lay.addWidget(attach)

        self._input = QLineEdit()
        self._input.setObjectName("ComposerInput")
        self._input.setPlaceholderText("Message…")
        self._input.returnPressed.connect(self._on_send)
        inner_lay.addWidget(self._input)

        emoji = QPushButton("😊")
        emoji.setObjectName("ComposerIconBtn")
        inner_lay.addWidget(emoji)

        send = QPushButton("↑")
        send.setObjectName("SendBtn")
        send.clicked.connect(self._on_send)
        inner_lay.addWidget(send)

        outer.addWidget(inner_frame)

    def _on_send(self):
        text = self._input.text().strip()
        if text:
            self.send_message.emit(text)
            self._input.clear()

    def set_enabled(self, enabled: bool):
        self._input.setEnabled(enabled)
        self._input.setPlaceholderText(
            "Message…" if enabled else "Join a room to start chatting"
        )


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
    def __init__(self, theme: str = "light", parent=None):
        super().__init__(parent)
        self._theme  = theme
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
        self._msgs    = MessagesArea()
        self._composer = Composer()
        self._composer.set_enabled(False)

        chat_lay.addWidget(self._header)
        chat_lay.addWidget(self._msgs)
        chat_lay.addWidget(self._composer)
        self._stack.addWidget(self._chat_widget)

        self._stack.setCurrentWidget(self._empty)

    def open_room(self, room_id: str, name: str,
                  members: list[str], locked: bool):
        self._room_id = room_id
        self._header.update_room(name, members, locked)
        self._msgs.clear()
        self._composer.set_enabled(True)
        self._stack.setCurrentWidget(self._chat_widget)

    def close_room(self):
        self._room_id = None
        self._composer.set_enabled(False)
        self._stack.setCurrentWidget(self._empty)

    def add_message(self, sender: str, text: str, ts: float, outgoing: bool):
        self._msgs.add_message(sender, text, ts, outgoing, self._theme)

    def add_sys(self, text: str):
        self._msgs.add_sys_msg(text)

    @property
    def send_message(self):
        return self._composer.send_message

    @property
    def current_room_id(self) -> str | None:
        return self._room_id


# ── Main window ───────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self, server_url: str = "ws://localhost:8765",
                 username: str = "", theme: str = "light"):
        super().__init__()
        self._server_url = server_url
        self._username   = username or "me"
        self._theme      = theme
        self._bridge: WSBridge | None = None

        # In-memory state
        self._rooms: dict[str, dict] = {}   # room_id → {name, members, locked, key}

        self._build_ui()
        self._apply_theme()
        self.setWindowTitle("Beam — P2P Chat")
        self.resize(1100, 720)
        self.setMinimumSize(800, 560)

        # Connect after UI is ready
        QTimer.singleShot(100, self._connect)

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._rail = Rail()
        self._rail.set_username(self._username)
        self._rail._settings_btn.clicked.connect(self._open_settings)
        root.addWidget(self._rail)

        self._conv = ConvPanel(self._theme)
        self._conv.room_selected.connect(self._on_room_selected)
        self._conv.create_room.connect(self._on_create_room)
        self._conv.join_room.connect(self._on_join_room)
        root.addWidget(self._conv)

        self._chat = ChatPanel(self._theme)
        self._chat.send_message.connect(self._on_send_message)
        root.addWidget(self._chat)

    def _apply_theme(self):
        self.setStyleSheet(make_qss(self._theme))
        self._theme_current = self._theme

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

        if mtype == T.WELCOME:
            pass

        elif mtype == T.SYSTEM:
            self.statusBar().showMessage(payload.get("message", ""), 3000)

        elif mtype == T.ERROR:
            self.statusBar().showMessage("⚠  " + payload.get("message", ""), 5000)

        elif mtype == T.ROOM_CREATED:
            rid  = payload["room_id"]
            name = payload["name"]
            locked = payload.get("locked", False)
            pending = self._rooms.pop("__pending__", {})
            pw  = pending.get("_pending_pw", "")
            key = derive_key(rid, pw) if pw else None
            self._rooms[rid] = {"name": name, "members": [self._username],
                                "locked": locked, "key": key}
            self._conv.upsert_room(rid, name, self._username, 1, locked)
            self._conv.set_active(rid)
            self._chat.open_room(rid, name, [self._username], locked)

        elif mtype == T.ROOM_JOINED:
            rid     = payload["room_id"]
            name    = payload["name"]
            members = payload.get("members", [])
            locked  = payload.get("locked", False)
            key     = self._rooms.get(rid, {}).get("_pending_key")
            self._rooms[rid] = {"name": name, "members": members,
                                "locked": locked, "key": key}
            self._conv.upsert_room(rid, name, self._username,
                                   len(members), locked)
            self._conv.set_active(rid)
            self._chat.open_room(rid, name, members, locked)

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
            rid       = payload.get("room_id", "")
            if encrypted:
                key = self._rooms.get(rid, {}).get("key")
                if key:
                    plain = decrypt(key, text)
                    text  = plain if plain else "[decryption failed]"
                else:
                    text = "[encrypted]"
            if rid == self._chat.current_room_id:
                self._chat.add_message(sender, text, ts, outgoing=False)
            else:
                # Update preview even if not in view
                self._conv.set_preview(rid, f"{sender}: {text}", ts)

        elif mtype == T.ROOM_LIST:
            for r in payload.get("rooms", []):
                self._conv.upsert_room(
                    r["id"], r["name"], r.get("creator", ""),
                    r.get("members", 0), r.get("locked", False)
                )

    # ── User actions ──────────────────────────────────────────────────────────

    @pyqtSlot(str)
    def _on_room_selected(self, room_id: str):
        if room_id == self._chat.current_room_id:
            return
        if self._chat.current_room_id:
            self._bridge.send_frame(T.LEAVE_ROOM)
        room = self._rooms.get(room_id, {})
        self._bridge.send_frame(T.JOIN_ROOM, room_id=room_id)

    @pyqtSlot()
    def _on_create_room(self):
        dlg = RoomDialog("create", self)
        self._style_dialog(dlg)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        v   = dlg.values()
        rid_placeholder = "__pending__"
        key = derive_key(rid_placeholder, v["password"]) if v["password"] else None
        # key will be re-derived after ROOM_CREATED returns the real room_id
        self._rooms.setdefault(rid_placeholder, {})["_pending_key"] = key
        if v["password"]:
            self._rooms[rid_placeholder]["_pending_pw"] = v["password"]
        self._bridge.send_frame(T.CREATE_ROOM, name=v["name"], password=v["password"])

    @pyqtSlot()
    def _on_join_room(self):
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
        key = self._rooms.get(rid, {}).get("key")
        if key:
            enc_text  = encrypt(key, text)
            encrypted = True
        else:
            enc_text  = text
            encrypted = False
        self._bridge.send_frame(T.SEND_MSG, text=enc_text, encrypted=encrypted)
        self._chat.add_message(self._username, text, time.time(), outgoing=True)
        self._conv.set_preview(rid, f"You: {text}", time.time())

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

    def _on_room_created_fix_key(self, rid: str, pw: str):
        """Re-derive key with the real room_id received from server."""
        key = derive_key(rid, pw) if pw else None
        if rid in self._rooms:
            self._rooms[rid]["key"] = key

    def closeEvent(self, event):
        if self._bridge:
            self._bridge.close()
            self._bridge.wait(1500)
        event.accept()
