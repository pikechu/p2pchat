"""Floating call window and incoming-call dialog."""
from PyQt6.QtCore import Qt, QPoint, pyqtSignal, QTimer
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QDialog,
)
from PyQt6.QtGui import QMouseEvent


def _fmt_duration(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


class CallWidget(QWidget):
    """Frameless, draggable, always-on-top call HUD."""

    hangup_requested = pyqtSignal()
    mute_toggled     = pyqtSignal()

    def __init__(self, peer: str, theme: str = "light", parent=None):
        super().__init__(parent,
                         Qt.WindowType.Window |
                         Qt.WindowType.FramelessWindowHint |
                         Qt.WindowType.WindowStaysOnTopHint)
        self.setObjectName("CallWidget")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self._drag_pos: QPoint | None = None
        self._muted = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 14)
        lay.setSpacing(8)

        # ── Drag bar / title ─────────────────────────────────────
        top = QHBoxLayout()
        top.setSpacing(8)
        drag = QLabel("📞")
        drag.setObjectName("CallDragBar")
        top.addWidget(drag)
        self._peer_lbl = QLabel(peer)
        self._peer_lbl.setObjectName("CallPeerName")
        top.addWidget(self._peer_lbl, 1)
        lay.addLayout(top)

        # ── Status row ────────────────────────────────────────────
        status = QHBoxLayout()
        status.setSpacing(8)
        self._mode_lbl = QLabel("中继 ●")
        self._mode_lbl.setObjectName("CallModeBadge")
        status.addWidget(self._mode_lbl)
        self._dur_lbl = QLabel("00:00")
        self._dur_lbl.setObjectName("CallDuration")
        status.addWidget(self._dur_lbl)
        status.addStretch()
        lay.addLayout(status)

        # ── Buttons ───────────────────────────────────────────────
        btns = QHBoxLayout()
        btns.setSpacing(12)
        btns.addStretch()
        self._mute_btn = QPushButton("🎤")
        self._mute_btn.setObjectName("CallMuteBtn")
        self._mute_btn.setFixedSize(40, 40)
        self._mute_btn.clicked.connect(self._on_mute)
        btns.addWidget(self._mute_btn)
        hangup_btn = QPushButton("📵")
        hangup_btn.setObjectName("CallHangupBtn")
        hangup_btn.setFixedSize(40, 40)
        hangup_btn.clicked.connect(self.hangup_requested)
        btns.addWidget(hangup_btn)
        btns.addStretch()
        lay.addLayout(btns)

        self.setFixedWidth(220)
        self.adjustSize()

    # ── Public update methods ─────────────────────────────────────────────────

    def set_duration(self, seconds: int) -> None:
        self._dur_lbl.setText(_fmt_duration(seconds))

    def set_mode(self, mode: str) -> None:
        if mode == "direct":
            self._mode_lbl.setText("直连 ●")
            self._mode_lbl.setObjectName("CallModeDirect")
        else:
            self._mode_lbl.setText("中继 ●")
            self._mode_lbl.setObjectName("CallModeRelay")
        # Force QSS re-polish after objectName change
        self._mode_lbl.style().unpolish(self._mode_lbl)
        self._mode_lbl.style().polish(self._mode_lbl)

    def set_muted(self, muted: bool) -> None:
        self._muted = muted
        if muted:
            self._mute_btn.setText("🔇")
            self._mute_btn.setObjectName("CallMutedBtn")
        else:
            self._mute_btn.setText("🎤")
            self._mute_btn.setObjectName("CallMuteBtn")
        self._mute_btn.style().unpolish(self._mute_btn)
        self._mute_btn.style().polish(self._mute_btn)

    # ── Drag ──────────────────────────────────────────────────────────────────

    def mousePressEvent(self, ev: QMouseEvent) -> None:
        if ev.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = ev.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, ev: QMouseEvent) -> None:
        if self._drag_pos and ev.buttons() & Qt.MouseButton.LeftButton:
            self.move(ev.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, ev: QMouseEvent) -> None:
        self._drag_pos = None

    def _on_mute(self) -> None:
        self.mute_toggled.emit()


class IncomingCallDialog(QDialog):
    """Modal dialog shown when a CALL_OFFER arrives."""

    accepted_signal = pyqtSignal()
    rejected_signal = pyqtSignal()

    def __init__(self, peer: str, theme: str = "light", parent=None):
        super().__init__(parent,
                         Qt.WindowType.Dialog |
                         Qt.WindowType.FramelessWindowHint |
                         Qt.WindowType.WindowStaysOnTopHint)
        self.setObjectName("Dialog")
        self._peer = peer

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(14)

        lay.addWidget(QLabel(f"📞  {peer} 正在呼叫你"))

        btns = QHBoxLayout()
        btns.setSpacing(12)
        accept_btn = QPushButton("接听")
        accept_btn.setObjectName("BtnPrimary")
        accept_btn.setMinimumWidth(80)
        accept_btn.clicked.connect(self._on_accept)
        reject_btn = QPushButton("拒绝")
        reject_btn.setObjectName("BtnGhost")
        reject_btn.setMinimumWidth(80)
        reject_btn.clicked.connect(self._on_reject)
        btns.addStretch()
        btns.addWidget(accept_btn)
        btns.addWidget(reject_btn)
        btns.addStretch()
        lay.addLayout(btns)

        # Auto-reject after 15 s
        self._auto_timer = QTimer(self)
        self._auto_timer.setSingleShot(True)
        self._auto_timer.setInterval(15000)
        self._auto_timer.timeout.connect(self._on_reject)
        self._auto_timer.start()

    def _on_accept(self) -> None:
        self._auto_timer.stop()
        self.accepted_signal.emit()
        self.accept()

    def _on_reject(self) -> None:
        self._auto_timer.stop()
        self.rejected_signal.emit()
        self.reject()
