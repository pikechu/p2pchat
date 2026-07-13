"""Reusable visual components matching the Beam design system."""

from datetime import datetime

from PyQt6.QtCore import Qt, QSize, QRectF, pyqtSignal, QTimer, QEvent
from PyQt6.QtGui import (
    QPainter, QPainterPath, QLinearGradient, QColor,
    QBrush, QPen, QFont, QAction, QPixmap,
)
from PyQt6.QtWidgets import (
    QWidget, QLabel, QHBoxLayout, QVBoxLayout, QSizePolicy,
    QFrame, QPushButton, QGridLayout, QScrollArea, QMenu,
)

from .popups import popup_above_global_pos
from .theme import TOKENS, avatar_stops


# ── Avatar ────────────────────────────────────────────────────────────────────

class Avatar(QWidget):
    """Circular avatar: gradient background + initials, or custom image."""

    clicked = pyqtSignal()

    def __init__(self, name: str = "", size: int = 36, parent=None):
        super().__init__(parent)
        self._name   = name
        self._sz     = size
        self._pixmap: QPixmap | None = None
        self.setFixedSize(size, size)

    def set_name(self, name: str):
        self._name = name
        self.update()

    def set_pixmap(self, pixmap: QPixmap | None):
        self._pixmap = pixmap
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        path = QPainterPath()
        path.addEllipse(QRectF(0, 0, self._sz, self._sz))
        p.setClipPath(path)

        if self._pixmap and not self._pixmap.isNull():
            scaled = self._pixmap.scaled(
                self._sz, self._sz,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = (self._sz - scaled.width()) // 2
            y = (self._sz - scaled.height()) // 2
            p.drawPixmap(x, y, scaled)
        else:
            stops = avatar_stops(self._name or "?")
            grad  = QLinearGradient(0, 0, self._sz, self._sz)
            grad.setColorAt(0, QColor(stops[0]))
            grad.setColorAt(1, QColor(stops[1]))
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
    """9×9 px coloured dot: ok (green) / warn (amber) / offline (gray)."""

    _COLORS = {
        "ok":           "#16a34a",
        "warn":         "#d97706",
        "offline":      "#9ca3af",
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


# ── Quote bar (reply preview inside bubble) ───────────────────────────────────

class QuoteBar(QFrame):
    """Quoted message shown at the top of a reply bubble."""

    def __init__(self, sender: str, text: str, theme: str = "light", parent=None):
        super().__init__(parent)
        self.setObjectName("QuoteBar")
        self._theme = theme

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 4, 8, 4)
        lay.setSpacing(1)

        name_lbl = QLabel(sender)
        name_lbl.setObjectName("QuoteSender")
        lay.addWidget(name_lbl)

        preview = text[:200] + ("…" if len(text) > 200 else "")
        text_lbl = QLabel(preview)
        text_lbl.setObjectName("QuoteText")
        text_lbl.setWordWrap(True)
        text_lbl.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        lay.addWidget(text_lbl)


# ── Message bubble ────────────────────────────────────────────────────────────

class BubbleWidget(QFrame):
    """Single message bubble with optional quote, status ticks, right-click reply."""

    reply_requested = pyqtSignal(str, str, int)   # sender, text, seq

    def __init__(self, sender: str, text: str, ts: float,
                 outgoing: bool = False, show_sender: bool = True,
                 theme: str = "light", seq: int = 0,
                 quote: dict | None = None, parent=None):
        super().__init__(parent)
        self._sender   = sender
        self._text     = text
        self._quote    = quote
        self._outgoing = outgoing
        self._theme    = theme
        self._seq      = seq
        self.setObjectName("BubbleOut" if outgoing else "BubbleIn")
        sp = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        sp.setHeightForWidth(True)
        self.setSizePolicy(sp)
        self.setMaximumWidth(720)  # tightened to natural width by _update_max_width()

        vlay = QVBoxLayout(self)
        vlay.setContentsMargins(0, 0, 0, 0)
        vlay.setSpacing(2)

        # Sender name (incoming group messages)
        if show_sender and not outgoing and sender:
            name_lbl = QLabel(sender)
            name_lbl.setObjectName("BubbleSender")
            vlay.addWidget(name_lbl)

        # Quoted reply bar
        if quote:
            quote_bar = QuoteBar(quote.get("sender", ""), quote.get("text", ""), theme)
            vlay.addWidget(quote_bar)

        # Message text
        msg_lbl = QLabel(text)
        msg_lbl.setObjectName("BubbleText")
        msg_lbl.setWordWrap(True)
        msg_lbl.setTextFormat(Qt.TextFormat.PlainText)
        msg_lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse |
            Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        msg_lbl.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        vlay.addWidget(msg_lbl)
        self._msg_lbl = msg_lbl

        # Time + delivery tick row
        bottom = QHBoxLayout()
        bottom.setSpacing(3)
        bottom.setContentsMargins(0, 0, 0, 0)
        time_str = datetime.fromtimestamp(ts).strftime("%H:%M")
        time_lbl = QLabel(time_str)
        time_lbl.setObjectName("BubbleTime")
        bottom.addWidget(time_lbl)

        if outgoing:
            self._tick = QLabel("✓")
            self._tick.setObjectName("TickSent")
            bottom.addWidget(self._tick)

        align = Qt.AlignmentFlag.AlignRight if outgoing else Qt.AlignmentFlag.AlignLeft
        bottom_w = QWidget()
        bottom_w.setLayout(bottom)
        bottom_lay = QHBoxLayout()
        bottom_lay.setContentsMargins(0, 0, 0, 0)
        if outgoing:
            bottom_lay.addStretch()
        bottom_lay.addWidget(bottom_w)
        if not outgoing:
            bottom_lay.addStretch()
        vlay.addLayout(bottom_lay)

        # Right-click context menu
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

        self._update_max_width()

    def _update_max_width(self):
        fm = self._msg_lbl.fontMetrics()
        lines = self._text.split('\n') if self._text else ['']
        text_w = max((fm.horizontalAdvance(line) for line in lines), default=0)
        # For incoming messages, also account for sender name width
        if not self._outgoing and self._sender:
            text_w = max(text_w, fm.horizontalAdvance(self._sender))
        # Quote bar: needs its own space. QuoteBar has 8+8px internal margins
        # plus a 3px left border, so add 19px on top of the bubble's 24px padding.
        if self._quote:
            q_sender = self._quote.get("sender", "") or ""
            q_text   = (self._quote.get("text",   "") or "")[:200]
            q_line   = q_text.split('\n')[0] if q_text else ""
            q_w = max(
                fm.horizontalAdvance(q_sender) if q_sender else 0,
                fm.horizontalAdvance(q_line)   if q_line   else 0,
            )
            text_w = max(text_w, q_w + 19)
        # 24px bubble padding (QSS padding: 8px 12px) + 10px rendering safety margin
        natural_w = max(text_w + 34, 60)
        self.setMaximumWidth(min(natural_w, 720))

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange) \
                and hasattr(self, '_msg_lbl'):
            self._update_max_width()
            self.updateGeometry()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if hasattr(self, '_msg_lbl'):
            self._update_max_width()
            self.updateGeometry()

    def set_status(self, status: str):
        """Update delivery tick. status: 'sent' | 'delivered' | 'read'."""
        if not hasattr(self, '_tick'):
            return
        t = TOKENS[self._theme]
        if status == "delivered":
            self._tick.setText("✓✓")
            self._tick.setObjectName("TickDelivered")
        elif status == "read":
            self._tick.setText("✓✓")
            self._tick.setObjectName("TickRead")
            self._tick.setStyleSheet(f"color: {t['accent']};")
        self._tick.style().unpolish(self._tick)
        self._tick.style().polish(self._tick)

    def hasHeightForWidth(self) -> bool:
        return True

    def sizeHint(self) -> QSize:
        # Use maximumWidth (= natural text width capped at 720) as the preferred
        # width.  QLabel.sizeHint() caches the last layout width and will return
        # 720 after the bubble was first rendered wide; bypassing it here gives
        # correct QQ-style narrow bubbles for short text.
        w = self.maximumWidth()
        h = self.heightForWidth(w)
        return QSize(w, h if h >= 0 else super().sizeHint().height())

    def heightForWidth(self, width: int) -> int:
        m = self.contentsMargins()
        # Clamp to our max width: the parent layout may pass the full container
        # width, but we render at most 720 px, so compute lines at that width.
        inner_w = min(width, self.maximumWidth()) - m.left() - m.right()
        h = self.layout().heightForWidth(max(0, inner_w))
        return (h + m.top() + m.bottom()) if h >= 0 else -1

    def _show_context_menu(self, pos):
        menu = QMenu(self)
        copy_act  = menu.addAction("⎘  Copy")
        reply_act = menu.addAction("↩  Reply")
        chosen = popup_above_global_pos(menu, self.mapToGlobal(pos))
        if chosen == copy_act:
            from PyQt6.QtWidgets import QApplication
            QApplication.clipboard().setText(self._text)
        elif chosen == reply_act:
            self.reply_requested.emit(self._sender, self._text, self._seq)


# ── Typing indicator ──────────────────────────────────────────────────────────

class TypingWidget(QLabel):
    """Shows 'Name is typing…' — shown/hidden by MainWindow."""

    def __init__(self, theme: str = "light", parent=None):
        super().__init__(parent)
        self._theme = theme
        self.setObjectName("TypingIndicator")
        self.hide()
        # Auto-hide after 8 seconds in case USER_TYPING=false never arrives
        self._auto_hide = QTimer(self)
        self._auto_hide.setSingleShot(True)
        self._auto_hide.setInterval(8000)
        self._auto_hide.timeout.connect(self.hide)

    def show_typing(self, username: str):
        self.setText(f"  {username} is typing…")
        self.show()
        self._auto_hide.start()

    def hide_typing(self):
        self._auto_hide.stop()
        self.hide()


# ── Emoji panel ───────────────────────────────────────────────────────────────

EMOJI_ROWS = [
    # Faces
    "😀 😂 😍 🥰 😎 😭 😤 🤔 😱 😅 🤣 😊 😇 🥳 😴 🤗 😬 🙄 😏 😒",
    # Hands / hearts
    "👍 👎 👌 ✌️ 🤞 🙏 👏 🤝 💪 ☝️ ❤️ 🧡 💛 💚 💙 💜 🖤 💔 💕 💯",
    # Symbols / objects
    "🔥 ✨ 💥 🎉 🚀 🌟 ⭐ 🎯 🏆 🎊 🎵 🎨 💎 🔑 ⚡ 🌈 ☀️ ❄️ 🌊 🍀",
    # Animals / nature
    "🐶 🐱 🐸 🦊 🐼 🦁 🐺 🐷 🐮 🐙 🦋 🌸 🌺 🌻 🍁 🌿 🍄 🌵 🦄 🐲",
    # Food / misc
    "🍕 🍔 🍣 🍜 🍺 🍻 🥂 ☕ 🍵 🧃 🍎 🍓 🍊 🍋 🥑 🌮 🍩 🎂 🍦 🍫",
]

class EmojiPanel(QWidget):
    """Collapsible emoji picker — emits emoji_selected(str) on click."""

    emoji_selected = pyqtSignal(str)

    def __init__(self, theme: str = "light", parent=None):
        super().__init__(parent)
        self._theme = theme
        self.setObjectName("EmojiPanel")
        self.setFixedHeight(196)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(2)

        scroll = QScrollArea()
        scroll.setObjectName("EmojiScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        inner = QWidget()
        inner.setObjectName("EmojiInner")
        grid = QVBoxLayout(inner)
        grid.setContentsMargins(4, 4, 4, 4)
        grid.setSpacing(2)

        for row_str in EMOJI_ROWS:
            row_w = QWidget()
            row_lay = QHBoxLayout(row_w)
            row_lay.setContentsMargins(0, 0, 0, 0)
            row_lay.setSpacing(2)
            for emoji in row_str.split():
                btn = QPushButton(emoji)
                btn.setObjectName("EmojiBtn")
                btn.setFixedSize(32, 32)
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                btn.clicked.connect(lambda checked, e=emoji: self.emoji_selected.emit(e))
                row_lay.addWidget(btn)
            row_lay.addStretch()
            grid.addWidget(row_w)

        grid.addStretch()
        scroll.setWidget(inner)
        outer.addWidget(scroll)


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
    clicked       = pyqtSignal(str)   # emits room_id on left-click
    right_clicked = pyqtSignal(str)   # emits room_id on right-click

    def __init__(self, room_id: str, name: str, creator: str,
                 members: int = 0, locked: bool = False,
                 unread: int = 0, theme: str = "light",
                 conn_state: str = "offline", parent=None):
        super().__init__(parent)
        self._room_id    = room_id
        self._theme      = theme
        self._conn_state = conn_state
        self.setObjectName("ConvRow")
        self.setProperty("active", False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(64)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(10)

        # Avatar with status dot overlay
        av_container = QWidget()
        av_container.setFixedSize(42, 42)
        self._avatar = Avatar(name, 40, av_container)
        self._avatar.move(0, 0)
        self._dot = StatusDot(conn_state, theme, av_container)
        self._dot.move(30, 30)   # bottom-right corner of 42×42
        lay.addWidget(av_container)

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

        self._unread_badge = QLabel("")
        self._unread_badge.setObjectName("UnreadBadge")
        self._unread_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        bot_row.addWidget(self._unread_badge)
        self.set_unread(unread)
        mid.addLayout(bot_row)

        lay.addLayout(mid)

    def set_active(self, active: bool):
        self.setProperty("active", active)
        self.style().unpolish(self)
        self.style().polish(self)

    def set_conn_state(self, state: str):
        """state: 'ok' | 'warn' | 'offline'"""
        self._dot.set_state(state)

    def set_members(self, count: int):
        if not self._prev_lbl.text() or self._prev_lbl.text().endswith("member") or \
                self._prev_lbl.text().endswith("members"):
            self._prev_lbl.setText(f"{count} member{'s' if count != 1 else ''}")

    def set_room_name(self, name: str):
        locked = self._name_lbl.text().startswith("🔒")
        self._name_lbl.setText(("🔒 " if locked else "") + name)
        self._avatar.set_name(name)

    def set_preview(self, text: str, ts: float = 0):
        self._prev_lbl.setText(text[:50])
        if ts:
            self._time_lbl.setText(datetime.fromtimestamp(ts).strftime("%H:%M"))

    def set_unread(self, unread: int):
        self._unread_badge.setText(str(unread) if unread else "")
        self._unread_badge.setVisible(unread > 0)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            self.right_clicked.emit(self._room_id)
        else:
            self.clicked.emit(self._room_id)


# ── File / image transfer card ────────────────────────────────────────────────

import os as _os

class FileCard(QFrame):
    """Shows a file transfer in progress or completed, inside a bubble row."""

    cancel_requested = pyqtSignal(str)   # transfer_id

    def __init__(self, transfer_id: str, filename: str, size: int,
                 outgoing: bool = False, thumbnail_data: bytes | None = None,
                 theme: str = "light", parent=None):
        super().__init__(parent)
        self._tid       = transfer_id
        self._filename  = filename
        self._size      = size
        self._outgoing  = outgoing
        self._theme     = theme
        self._save_path: str | None = None
        self.setObjectName("FileCard")
        self.setFixedWidth(280)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(4)

        # Thumbnail for images
        self._thumb_lbl: QLabel | None = None
        if thumbnail_data:
            from PyQt6.QtGui import QPixmap
            pix = QPixmap()
            if pix.loadFromData(thumbnail_data):
                pix = pix.scaledToWidth(260,
                    Qt.TransformationMode.SmoothTransformation)
                self._thumb_lbl = QLabel()
                self._thumb_lbl.setPixmap(pix)
                self._thumb_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                lay.addWidget(self._thumb_lbl)

        # Filename + size row
        name_row = QHBoxLayout()
        name_row.setSpacing(6)
        icon = QLabel(_file_icon(filename))
        icon.setFixedWidth(22)
        name_row.addWidget(icon)

        info = QVBoxLayout()
        info.setSpacing(0)
        self._name_lbl = QLabel(filename)
        self._name_lbl.setObjectName("FileCardName")
        self._size_lbl = QLabel(_fmt_size(size))
        self._size_lbl.setObjectName("FileCardSize")
        info.addWidget(self._name_lbl)
        info.addWidget(self._size_lbl)
        name_row.addLayout(info)
        name_row.addStretch()

        self._cancel_btn = QPushButton("✕")
        self._cancel_btn.setObjectName("FileCardCancel")
        self._cancel_btn.setFixedSize(22, 22)
        self._cancel_btn.clicked.connect(lambda: self.cancel_requested.emit(self._tid))
        name_row.addWidget(self._cancel_btn)
        lay.addLayout(name_row)

        # Progress bar
        from PyQt6.QtWidgets import QProgressBar
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setObjectName("FileCardProgress")
        self._progress.setFixedHeight(4)
        self._progress.setTextVisible(False)
        lay.addWidget(self._progress)

        # Status label
        self._status_lbl = QLabel("Waiting…" if not outgoing else "Sending…")
        self._status_lbl.setObjectName("FileCardStatus")
        lay.addWidget(self._status_lbl)

    def set_progress(self, pct: int):
        self._progress.setValue(pct)
        self._status_lbl.setText(
            f"{'Sending' if self._outgoing else 'Receiving'} {pct}%"
        )

    def set_done(self, save_path: str | None = None):
        self._save_path = save_path
        self._progress.setValue(100)
        self._cancel_btn.hide()
        if save_path:
            self._status_lbl.setText(f"已保存 → 点击打开")
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self._status_lbl.setText("Sent ✓")

    def mousePressEvent(self, event):
        if self._save_path and _os.path.exists(self._save_path):
            from PyQt6.QtGui import QDesktopServices
            from PyQt6.QtCore import QUrl
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._save_path))
        super().mousePressEvent(event)

    def set_error(self, message: str):
        self._progress.hide()
        self._cancel_btn.hide()
        self._status_lbl.setText(f"Failed: {message}")
        self._status_lbl.setObjectName("FileCardError")
        self._status_lbl.style().unpolish(self._status_lbl)
        self._status_lbl.style().polish(self._status_lbl)

    def show_thumbnail(self, data: bytes):
        """Insert an image thumbnail at the top of the card (for images sent by self)."""
        from PyQt6.QtGui import QPixmap
        pix = QPixmap()
        if pix.loadFromData(data):
            pix = pix.scaledToWidth(260, Qt.TransformationMode.SmoothTransformation)
            thumb = QLabel()
            thumb.setPixmap(pix)
            thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.layout().insertWidget(0, thumb)


def _file_icon(filename: str) -> str:
    ext = _os.path.splitext(filename)[1].lower()
    if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        return "🖼"
    if ext in (".mp4", ".webm", ".mov", ".avi"):
        return "🎬"
    if ext in (".mp3", ".wav", ".ogg", ".flac"):
        return "🎵"
    if ext in (".pdf",):
        return "📄"
    if ext in (".zip", ".tar", ".gz", ".7z"):
        return "🗜"
    return "📎"


def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


class ImageCard(QFrame):
    """Inline image display for completed image transfers. Click to open."""

    def __init__(self, transfer_id: str, filename: str, image_data: bytes,
                 outgoing: bool = False, parent=None):
        super().__init__(parent)
        self._tid      = transfer_id
        self._outgoing = outgoing
        self._save_path: str | None = None
        self.setObjectName("FileCard")
        self.setFixedWidth(280)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)

        from PyQt6.QtGui import QPixmap
        pix = QPixmap()
        if pix.loadFromData(image_data):
            pix = pix.scaledToWidth(268, Qt.TransformationMode.SmoothTransformation)
            img_lbl = QLabel()
            img_lbl.setPixmap(pix)
            img_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lay.addWidget(img_lbl)

        cap_row = QHBoxLayout()
        cap_row.setSpacing(4)
        cap_row.addWidget(QLabel("🖼"))
        name_lbl = QLabel(filename)
        name_lbl.setObjectName("FileCardName")
        cap_row.addWidget(name_lbl, 1)
        lay.addLayout(cap_row)

    def set_progress(self, pct: int):
        pass  # image is shown immediately; no progress indicator needed

    def set_error(self, message: str):
        pass

    def set_done(self, save_path: str | None = None):
        self._save_path = save_path

    def mousePressEvent(self, event):
        if self._save_path and _os.path.exists(self._save_path):
            from PyQt6.QtGui import QDesktopServices
            from PyQt6.QtCore import QUrl
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._save_path))
        super().mousePressEvent(event)


class VideoCard(QFrame):
    """Video file card: shows progress + cancel while transferring, open button when done."""

    cancel_requested = pyqtSignal(str)   # transfer_id

    def __init__(self, transfer_id: str, filename: str, size: int,
                 outgoing: bool = False, parent=None):
        super().__init__(parent)
        self._tid      = transfer_id
        self._outgoing = outgoing
        self._save_path: str | None = None
        self.setObjectName("FileCard")
        self.setFixedWidth(280)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(4)

        row = QHBoxLayout()
        row.setSpacing(8)
        icon = QLabel("🎬")
        icon.setFixedWidth(28)
        row.addWidget(icon)

        info = QVBoxLayout()
        info.setSpacing(0)
        name_lbl = QLabel(filename)
        name_lbl.setObjectName("FileCardName")
        size_lbl = QLabel(_fmt_size(size))
        size_lbl.setObjectName("FileCardSize")
        info.addWidget(name_lbl)
        info.addWidget(size_lbl)
        row.addLayout(info, 1)

        self._cancel_btn = QPushButton("✕")
        self._cancel_btn.setObjectName("FileCardCancel")
        self._cancel_btn.setFixedSize(22, 22)
        self._cancel_btn.clicked.connect(lambda: self.cancel_requested.emit(self._tid))
        row.addWidget(self._cancel_btn)

        self._open_btn = QPushButton("▶ 打开")
        self._open_btn.setObjectName("BtnGhost")
        self._open_btn.setEnabled(False)
        self._open_btn.hide()
        self._open_btn.clicked.connect(self._open)
        row.addWidget(self._open_btn)
        lay.addLayout(row)

        from PyQt6.QtWidgets import QProgressBar
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setObjectName("FileCardProgress")
        self._progress.setFixedHeight(4)
        self._progress.setTextVisible(False)
        lay.addWidget(self._progress)

        self._status_lbl = QLabel("Waiting…" if not outgoing else "Sending…")
        self._status_lbl.setObjectName("FileCardStatus")
        lay.addWidget(self._status_lbl)

    def set_progress(self, pct: int):
        self._progress.setValue(pct)
        self._status_lbl.setText(
            f"{'Sending' if self._outgoing else 'Receiving'} {pct}%"
        )

    def set_error(self, message: str):
        self._progress.hide()
        self._cancel_btn.hide()
        self._status_lbl.setText(f"Failed: {message}")
        self._status_lbl.setObjectName("FileCardError")
        self._status_lbl.style().unpolish(self._status_lbl)
        self._status_lbl.style().polish(self._status_lbl)

    def set_done(self, save_path: str | None = None):
        self._save_path = save_path
        self._progress.hide()
        self._cancel_btn.hide()
        self._status_lbl.hide()
        self._open_btn.show()
        if save_path:
            self._open_btn.setEnabled(True)

    def _open(self):
        if self._save_path and _os.path.exists(self._save_path):
            from PyQt6.QtGui import QDesktopServices
            from PyQt6.QtCore import QUrl
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._save_path))
