"""Reusable visual components matching the Beam design system."""

from datetime import datetime

from PyQt6.QtCore import Qt, QSize, QRectF, pyqtSignal
from PyQt6.QtGui import (
    QPainter, QPainterPath, QLinearGradient, QColor,
    QBrush, QPen, QFont,
)
from PyQt6.QtWidgets import (
    QWidget, QLabel, QHBoxLayout, QVBoxLayout, QSizePolicy,
    QFrame,
)

from .theme import TOKENS, avatar_stops


# ── Avatar ────────────────────────────────────────────────────────────────────

class Avatar(QWidget):
    """Circular avatar: gradient background + initials (IBM Plex Mono)."""

    def __init__(self, name: str = "", size: int = 36, parent=None):
        super().__init__(parent)
        self._name = name
        self._sz   = size
        self.setFixedSize(size, size)

    def set_name(self, name: str):
        self._name = name
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        stops = avatar_stops(self._name or "?")
        grad  = QLinearGradient(0, 0, self._sz, self._sz)
        grad.setColorAt(0, QColor(stops[0]))
        grad.setColorAt(1, QColor(stops[1]))

        path = QPainterPath()
        path.addEllipse(QRectF(0, 0, self._sz, self._sz))
        p.fillPath(path, QBrush(grad))

        initials = (self._name or "?")[:2].upper()
        font = QFont("IBM Plex Mono", max(7, self._sz // 3))
        font.setWeight(QFont.Weight.DemiBold)
        p.setFont(font)
        p.setPen(QColor("white"))
        p.drawText(QRectF(0, 0, self._sz, self._sz),
                   Qt.AlignmentFlag.AlignCenter, initials)


# ── Status dot ────────────────────────────────────────────────────────────────

class StatusDot(QWidget):
    """7×7 px coloured dot: ok / warn / offline."""

    _COLORS = {
        "ok":      "#16a34a",
        "warn":    "#d97706",
        "offline": "#9ca3af",
        "dark_ok":      "#22c55e",
        "dark_warn":    "#f59e0b",
        "dark_offline": "#9ca3af",
    }

    def __init__(self, state: str = "ok", theme: str = "light", parent=None):
        super().__init__(parent)
        self._state = state
        self._theme = theme
        self.setFixedSize(9, 9)

    def set_state(self, state: str):
        self._state = state
        self.update()

    def paintEvent(self, _):
        key = f"{'dark_' if self._theme == 'dark' else ''}{self._state}"
        color = QColor(self._COLORS.get(key, self._COLORS["offline"]))
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QBrush(color))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(0, 0, 9, 9)


# ── Message bubble ────────────────────────────────────────────────────────────

class BubbleWidget(QFrame):
    """Single message bubble — in (received) or out (sent)."""

    def __init__(self, sender: str, text: str, ts: float,
                 outgoing: bool = False, show_sender: bool = True,
                 theme: str = "light", parent=None):
        super().__init__(parent)
        self._outgoing = outgoing
        self._theme    = theme
        self.setObjectName("BubbleOut" if outgoing else "BubbleIn")
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Maximum)
        self.setMaximumWidth(520)

        vlay = QVBoxLayout(self)
        vlay.setContentsMargins(0, 0, 0, 0)
        vlay.setSpacing(2)

        if show_sender and not outgoing and sender:
            name_lbl = QLabel(sender)
            name_lbl.setObjectName("BubbleSender")
            vlay.addWidget(name_lbl)

        msg_lbl = QLabel(text)
        msg_lbl.setObjectName("BubbleText")
        msg_lbl.setWordWrap(True)
        msg_lbl.setTextFormat(Qt.TextFormat.PlainText)
        vlay.addWidget(msg_lbl)

        time_str = datetime.fromtimestamp(ts).strftime("%H:%M")
        time_lbl = QLabel(time_str)
        time_lbl.setObjectName("BubbleTime")
        align = Qt.AlignmentFlag.AlignRight if outgoing else Qt.AlignmentFlag.AlignLeft
        time_lbl.setAlignment(align)
        vlay.addWidget(time_lbl)


# ── System / day messages ─────────────────────────────────────────────────────

class SysMsgWidget(QLabel):
    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setObjectName("SysMsg")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)


class DayMarkWidget(QLabel):
    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setObjectName("DayMark")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)


# ── Conversation row ──────────────────────────────────────────────────────────

class ConvRowWidget(QWidget):
    clicked = pyqtSignal(str)   # emits room_id

    def __init__(self, room_id: str, name: str, creator: str,
                 members: int = 0, locked: bool = False,
                 unread: int = 0, theme: str = "light", parent=None):
        super().__init__(parent)
        self._room_id = room_id
        self._theme   = theme
        self.setObjectName("ConvRow")
        self.setProperty("active", False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(64)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(10)

        self._avatar = Avatar(name, 40)
        lay.addWidget(self._avatar)

        mid = QVBoxLayout()
        mid.setSpacing(2)
        mid.setContentsMargins(0, 0, 0, 0)

        top_row = QHBoxLayout()
        top_row.setSpacing(4)
        name_str = ("🔒 " if locked else "") + name
        self._name_lbl = QLabel(name_str)
        self._name_lbl.setObjectName("ConvRowName")
        top_row.addWidget(self._name_lbl)
        top_row.addStretch()

        t = TOKENS[theme]
        self._time_lbl = QLabel("")
        self._time_lbl.setObjectName("ConvRowTime")
        top_row.addWidget(self._time_lbl)
        mid.addLayout(top_row)

        bot_row = QHBoxLayout()
        bot_row.setSpacing(4)
        preview = f"{members} member{'s' if members != 1 else ''}"
        self._prev_lbl = QLabel(preview)
        self._prev_lbl.setObjectName("ConvRowPreview")
        bot_row.addWidget(self._prev_lbl)
        bot_row.addStretch()

        if unread:
            badge = QLabel(str(unread))
            badge.setObjectName("UnreadBadge")
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            bot_row.addWidget(badge)
        mid.addLayout(bot_row)

        lay.addLayout(mid)

    def set_active(self, active: bool):
        self.setProperty("active", active)
        self.style().unpolish(self)
        self.style().polish(self)

    def set_preview(self, text: str, ts: float = 0):
        self._prev_lbl.setText(text[:50])
        if ts:
            self._time_lbl.setText(datetime.fromtimestamp(ts).strftime("%H:%M"))

    def mousePressEvent(self, _):
        self.clicked.emit(self._room_id)
