"""Design tokens from the Beam design system."""

from PyQt6.QtGui import QFont, QFontDatabase

# ── Colour palettes ───────────────────────────────────────────────────────────

TOKENS: dict[str, dict[str, str]] = {
    "light": {
        "bg":             "#fcfdfe",
        "bg_sidebar":     "#f5f7fa",
        "bg_rail":        "#eef1f5",
        "bg_chat":        "#ffffff",
        "bg_hover":       "#edf0f5",
        "bg_active":      "#dbeeff",
        "bg_input":       "#ffffff",
        "bubble_in":      "#f1f3f6",
        "bubble_out":     "#d9eefb",
        "fg":             "#0a0e14",
        "fg2":            "#3b4350",
        "fg3":            "#6b7585",
        "fg4":            "#9aa3b2",
        "accent":         "#0088cc",
        "accent_soft":    "#dbeeff",
        "ok":             "#16a34a",
        "warn":           "#d97706",
        "error":          "#dc2626",
        "offline":        "#9ca3af",
        "line":           "#e5e9f0",
        "line_strong":    "#d0d6e0",
        "titlebar":       "#f0f2f5",
        "scrollbar":      "#d0d6e0",
    },
    "dark": {
        "bg":             "#0f1318",
        "bg_sidebar":     "#141920",
        "bg_rail":        "#0c1015",
        "bg_chat":        "#111720",
        "bg_hover":       "#1a2030",
        "bg_active":      "#0d3a52",
        "bg_input":       "#1a1f28",
        "bubble_in":      "#1e2530",
        "bubble_out":     "#0d3a52",
        "fg":             "#e8edf5",
        "fg2":            "#b0bac8",
        "fg3":            "#788090",
        "fg4":            "#485060",
        "accent":         "#36a8e0",
        "accent_soft":    "#0d3a52",
        "ok":             "#22c55e",
        "warn":           "#f59e0b",
        "error":          "#ef4444",
        "offline":        "#9ca3af",
        "line":           "#1e2530",
        "line_strong":    "#2a3548",
        "titlebar":       "#0c1015",
        "scrollbar":      "#2a3548",
    },
}

# Deterministic avatar gradient colours (cycled by name hash)
AVATAR_STOPS = [
    ("#0088cc", "#5fc4ee"),
    ("#7c5cff", "#b48bff"),
    ("#16a34a", "#6ee7b7"),
    ("#d97706", "#fbbf24"),
    ("#e11d48", "#fb7185"),
    ("#0e7490", "#67e8f9"),
    ("#475569", "#94a3b8"),
    ("#65a30d", "#bef264"),
]


def avatar_stops(name: str) -> tuple[str, str]:
    idx = sum(ord(c) for c in name) % len(AVATAR_STOPS)
    return AVATAR_STOPS[idx]


# ── QSS template ──────────────────────────────────────────────────────────────

def make_qss(theme: str = "light") -> str:
    t = TOKENS[theme]
    return f"""
    * {{ outline: none; }}

    QMainWindow {{ background: {t['bg']}; }}
    #AppRoot     {{ background: {t['bg']}; }}
    QStackedWidget {{ background: transparent; }}

    QStatusBar {{
        background: {t['titlebar']}; color: {t['fg3']};
        font-size: 11px; border-top: 1px solid {t['line']};
        padding: 0px; margin: 0px;
    }}
    QStatusBar::item {{ border: none; }}

    QListWidget {{
        background: {t['bg_input']}; border: 1px solid {t['line']};
        border-radius: 6px; outline: none;
    }}
    QListWidget::item {{ padding: 8px 12px; color: {t['fg']}; border-radius: 4px; }}
    QListWidget::item:selected {{ background: {t['bg_active']}; color: {t['fg']}; }}
    QListWidget::item:hover {{ background: {t['bg_hover']}; }}

    QWidget {{
        font-family: "IBM Plex Sans", "Segoe UI", system-ui, sans-serif;
        font-size: 13px;
        color: {t['fg']};
        background: transparent;
        border: none;
    }}

    /* ── Rail ──────────────────────────────────────────── */
    #Rail {{
        background: {t['bg_rail']};
        border-right: 1px solid {t['line']};
    }}
    #RailBtn {{
        background: transparent;
        color: {t['fg3']};
        border-radius: 8px;
        font-size: 18px;
        padding: 0;
    }}
    #RailBtn:hover {{ background: {t['bg_hover']}; color: {t['fg']}; }}
    #RailBtn[active=true] {{ background: {t['accent_soft']}; color: {t['accent']}; }}
    #RailAvatar {{ border-radius: 18px; }}

    /* ── Conversation panel ─────────────────────────────── */
    #ConvPanel {{ background: {t['bg_sidebar']}; border-right: 1px solid {t['line']}; }}
    #ConvHeader {{ background: {t['bg_sidebar']}; border-bottom: 1px solid {t['line']}; padding: 14px 16px 10px; }}
    #ConvTitle {{ font-size: 16px; font-weight: 600; color: {t['fg']}; }}
    #NewRoomBtn {{
        background: {t['accent']};
        color: white;
        border-radius: 6px;
        font-size: 18px;
        font-weight: 600;
        padding: 0;
    }}
    #NewRoomBtn:hover {{ background: {t['accent']}; }}
    #SearchBox {{
        background: {t['bg_input']};
        border: 1px solid {t['line']};
        border-radius: 6px;
        padding: 6px 10px 6px 28px;
        font-size: 13px;
        color: {t['fg']};
    }}
    #SearchBox:focus {{ border-color: {t['accent']}; }}
    #ConvScroll {{ background: {t['bg_sidebar']}; border: none; }}
    #ConvList {{ background: {t['bg_sidebar']}; }}

    /* Conv row */
    #ConvRow {{ background: transparent; border-bottom: 1px solid {t['line']}; }}
    #ConvRow:hover {{ background: {t['bg_hover']}; }}
    #ConvRow[active=true] {{ background: {t['bg_active']}; }}
    #ConvRowName {{ font-size: 13.5px; font-weight: 600; color: {t['fg']}; }}
    #ConvRowPreview {{ font-size: 12px; color: {t['fg3']}; }}
    #ConvRowTime {{ font-family: "IBM Plex Mono", monospace; font-size: 10px; color: {t['fg4']}; }}
    #UnreadBadge {{
        background: {t['accent']}; color: white;
        font-family: "IBM Plex Mono", monospace; font-size: 10px; font-weight: 600;
        border-radius: 9px; padding: 0 5px; min-height: 18px; min-width: 18px;
    }}

    /* ── Chat panel ─────────────────────────────────────── */
    #ChatPanel {{ background: {t['bg_chat']}; }}
    #ChatHeader {{
        background: {t['bg_chat']};
        border-bottom: 1px solid {t['line']};
        padding: 0 16px;
        min-height: 52px; max-height: 52px;
    }}
    #ChatName {{ font-size: 14.5px; font-weight: 600; color: {t['fg']}; }}
    #ChatSub  {{ font-family: "IBM Plex Mono", monospace; font-size: 11px; color: {t['fg3']}; }}
    #HeaderBtn {{
        background: transparent; color: {t['fg3']};
        border-radius: 6px; font-size: 18px; padding: 0;
    }}
    #HeaderBtn:hover {{ background: {t['bg_hover']}; color: {t['fg']}; }}

    /* Messages scroll */
    #MsgsScroll {{ background: {t['bg_chat']}; border: none; }}
    #MsgsContainer {{ background: {t['bg_chat']}; }}
    QScrollBar:vertical {{
        width: 6px; background: transparent; margin: 0;
    }}
    QScrollBar::handle:vertical {{
        background: {t['scrollbar']}; border-radius: 3px; min-height: 20px;
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}

    /* Bubbles */
    #BubbleIn  {{
        background: {t['bubble_in']}; border-radius: 14px;
        border-bottom-left-radius: 4px; padding: 8px 12px;
    }}
    #BubbleOut {{
        background: {t['bubble_out']}; border-radius: 14px;
        border-bottom-right-radius: 4px; padding: 8px 12px;
    }}
    #BubbleSender {{ font-size: 12px; font-weight: 600; color: {t['accent']}; margin-bottom: 2px; }}
    #BubbleText   {{ font-size: 13.5px; color: {t['fg']}; line-height: 1.45; }}
    #BubbleTime   {{ font-family: "IBM Plex Mono", monospace; font-size: 10px; color: {t['fg4']}; margin-top: 2px; }}
    #SysMsg {{
        font-family: "IBM Plex Mono", monospace; font-size: 11px; color: {t['fg4']};
        background: {t['bg_hover']}; border-radius: 10px; padding: 3px 10px;
    }}
    #DayMark {{
        font-family: "IBM Plex Mono", monospace; font-size: 10.5px; color: {t['fg4']};
        background: {t['bg_hover']}; border-radius: 10px; padding: 3px 10px;
    }}

    /* Composer */
    #ComposerBar {{ background: {t['bg_chat']}; border-top: 1px solid {t['line']}; padding: 10px 16px 12px; }}
    #ComposerInner {{
        background: {t['bg_input']}; border: 1px solid {t['line']};
        border-radius: 10px; min-height: 40px;
    }}
    #ComposerInput {{
        background: transparent; font-size: 13.5px; color: {t['fg']};
        border: none; padding: 4px 8px;
    }}
    #SendBtn {{
        background: {t['accent']}; color: white;
        border-radius: 16px; font-size: 16px; font-weight: 600;
        min-width: 32px; max-width: 32px; min-height: 32px; max-height: 32px;
    }}
    #ComposerIconBtn {{
        background: transparent; color: {t['fg3']};
        border-radius: 6px; font-size: 16px;
        min-width: 30px; max-width: 30px; min-height: 30px; max-height: 30px;
    }}
    #ComposerIconBtn:hover {{ background: {t['bg_hover']}; color: {t['fg']}; }}

    /* ── Empty / placeholder ────────────────────────────── */
    #EmptyPanel {{ background: {t['bg_chat']}; }}
    #EmptyTitle {{ font-size: 16px; font-weight: 600; color: {t['fg2']}; }}
    #EmptyDesc  {{ font-size: 13px; color: {t['fg3']}; }}

    /* ── Dialogs ────────────────────────────────────────── */
    #Dialog {{ background: {t['bg']}; border: 1px solid {t['line_strong']}; border-radius: 10px; }}

    /* ── QMessageBox (overrides the transparent QWidget rule) ── */
    QMessageBox {{
        background: {t['bg']};
    }}
    QMessageBox QLabel {{
        background: {t['bg']};
        color: {t['fg']};
        font-size: 13px;
    }}
    QMessageBox QPushButton {{
        background: {t['bg_input']};
        color: {t['fg']};
        border: 1px solid {t['line']};
        border-radius: 6px;
        padding: 5px 18px;
        font-size: 13px;
    }}
    QMessageBox QPushButton:hover {{
        background: {t['bg_hover']};
    }}
    #DialogTitle {{ font-size: 15px; font-weight: 600; color: {t['fg']}; }}
    #FormLabel {{ font-size: 12px; font-weight: 500; color: {t['fg3']}; }}
    #CloseRemember {{ font-size: 12px; color: {t['fg3']}; }}
    #CloseRemember::indicator {{
        width: 14px; height: 14px;
        border: 1px solid {t['line']}; border-radius: 3px;
        background: {t['bg_input']};
    }}
    #CloseRemember::indicator:checked {{
        background: {t['accent']}; border-color: {t['accent']};
        image: none;
    }}
    #FormInput {{
        background: {t['bg_input']}; border: 1px solid {t['line']};
        border-radius: 6px; padding: 8px 10px; font-size: 13.5px; color: {t['fg']};
    }}
    #FormInput:focus {{ border-color: {t['accent']}; }}
    QComboBox#FormInput::drop-down {{ border: none; width: 24px; }}
    QComboBox#FormInput::down-arrow {{
        image: none; width: 0; height: 0;
        border-left: 5px solid transparent;
        border-right: 5px solid transparent;
        border-top: 6px solid {t['fg3']};
    }}
    QComboBox#FormInput QAbstractItemView {{
        background: {t['bg_input']};
        border: 1px solid {t['line']};
        border-radius: 6px;
        outline: none;
        selection-background-color: {t['bg_hover']};
        selection-color: {t['fg']};
        color: {t['fg']};
        padding: 2px;
    }}
    QComboBox#FormInput QAbstractItemView::item {{
        padding: 6px 10px;
        border-radius: 4px;
    }}
    #BtnPrimary {{
        background: {t['accent']}; color: white;
        border-radius: 6px; padding: 8px 16px; font-size: 13px; font-weight: 500;
    }}
    #BtnPrimary:hover {{ background: {t['accent']}; }}
    #BtnGhost {{
        background: transparent; color: {t['fg2']};
        border: 1px solid {t['line']}; border-radius: 6px;
        padding: 8px 16px; font-size: 13px;
    }}
    #BtnGhost:hover {{ background: {t['bg_hover']}; }}

    /* ── Quote bar (inside bubble) ─────────────────────── */
    #QuoteBar {{
        background: {t['bg_hover']};
        border-left: 3px solid {t['accent']};
        border-radius: 4px;
        margin-bottom: 4px;
    }}
    #QuoteSender {{ font-size: 11.5px; font-weight: 600; color: {t['accent']}; }}
    #QuoteText   {{ font-size: 12px; color: {t['fg3']}; }}

    /* ── Status ticks ───────────────────────────────────── */
    #TickSent      {{ font-size: 10px; color: {t['fg4']}; }}
    #TickDelivered {{ font-size: 10px; color: {t['fg3']}; }}
    #TickRead      {{ font-size: 10px; color: {t['accent']}; }}

    /* ── Typing indicator ───────────────────────────────── */
    #TypingIndicator {{
        font-family: "IBM Plex Mono", monospace;
        font-size: 11px; color: {t['fg4']};
        padding: 2px 20px 4px;
    }}

    /* ── Reply bar (above composer input) ───────────────── */
    #ReplyBar {{
        background: {t['accent_soft']};
        border-left: 3px solid {t['accent']};
        border-radius: 4px;
    }}
    #ReplyName    {{ font-size: 11.5px; font-weight: 600; color: {t['accent']}; }}
    #ReplyPreview {{ font-size: 12px; color: {t['fg3']}; }}
    #ReplyCancel  {{
        background: transparent; color: {t['fg3']};
        border-radius: 4px; font-size: 16px;
        max-width: 22px; max-height: 22px; padding: 0;
    }}
    #ReplyCancel:hover {{ background: {t['bg_hover']}; color: {t['fg']}; }}

    /* ── Emoji panel ────────────────────────────────────── */
    #EmojiPanel {{ background: {t['bg_chat']}; border-top: 1px solid {t['line']}; }}
    #EmojiScroll {{ background: transparent; border: none; }}
    #EmojiInner  {{ background: transparent; }}
    #EmojiBtn {{
        background: transparent; font-size: 18px;
        border: none; border-radius: 6px; padding: 0;
    }}
    #EmojiBtn:hover {{ background: {t['bg_hover']}; }}

    /* ── Settings ───────────────────────────────────────── */
    #SettingsPanel {{ background: {t['bg_chat']}; }}
    #SettingsVersion {{
        font-family: "IBM Plex Mono", monospace;
        font-size: 11px; color: {t['fg4']};
    }}
    #SettingsGroup {{
        background: {t['bg_sidebar']}; border: 1px solid {t['line']};
        border-radius: 8px; padding: 4px 0;
    }}
    #SettingsRow {{ background: transparent; border-bottom: 1px solid {t['line']}; padding: 12px 16px; }}
    #SettingsRow:last-child {{ border-bottom: none; }}
    #SettingsLabel {{ font-size: 13px; color: {t['fg']}; }}
    #SettingsValue {{ font-family: "IBM Plex Mono", monospace; font-size: 11.5px; color: {t['fg3']}; }}
    #SettingsSectionLabel {{
        font-family: "IBM Plex Mono", monospace; font-size: 10.5px;
        color: {t['fg4']}; letter-spacing: 0.08em;
    }}
    #StatusChip {{
        font-family: "IBM Plex Mono", monospace; font-size: 10.5px; font-weight: 500;
        border-radius: 12px; padding: 3px 8px;
    }}
    #StatusChipOk    {{ background: #dcfce7; color: {t['ok']}; }}
    #StatusChipWarn  {{ background: #fef3c7; color: {t['warn']}; }}
    #StatusChipAccent{{ background: {t['accent_soft']}; color: {t['accent']}; }}

    /* ── File card ─────────────────────────────────────────── */
    #FileCard {{
        background: {t['bg_hover']};
        border: 1px solid {t['line']};
        border-radius: 10px;
    }}
    #FileCardName  {{ font-size: 13px; font-weight: 600; color: {t['fg']}; }}
    #FileCardSize  {{ font-family: "IBM Plex Mono", monospace; font-size: 11px; color: {t['fg3']}; }}
    #FileCardStatus {{ font-family: "IBM Plex Mono", monospace; font-size: 11px; color: {t['fg3']}; }}
    #FileCardError  {{ font-size: 11px; color: {t['error']}; }}
    #FileCardCancel {{
        background: transparent; color: {t['fg3']};
        border-radius: 4px; font-size: 12px; padding: 0;
    }}
    #FileCardCancel:hover {{ background: {t['bg_hover']}; color: {t['error']}; }}
    QProgressBar#FileCardProgress {{
        background: {t['line']}; border-radius: 2px; border: none;
    }}
    QProgressBar#FileCardProgress::chunk {{
        background: {t['accent']}; border-radius: 2px;
    }}
    QProgressBar#UpdateProgress {{
        background: {t['line']}; border-radius: 3px; border: none;
    }}
    QProgressBar#UpdateProgress::chunk {{
        background: {t['accent']}; border-radius: 3px;
    }}

    /* ── Room info panel ──────────────────────────────────── */
    #RoomInfoPanel {{
        background: {t['bg_sidebar']};
        border-left: 1px solid {t['line']};
    }}
    #InfoPanelTitle {{ font-size: 13px; font-weight: 600; color: {t['fg']}; }}
    #InfoRoomName   {{ font-size: 14px; font-weight: 600; color: {t['fg']}; }}
    #InfoMeta       {{ font-size: 12px; color: {t['fg3']}; }}
    #InfoSep        {{ color: {t['line']}; }}
    #InfoCloseBtn   {{
        background: transparent; color: {t['fg3']}; border: none;
        border-radius: 4px; font-size: 16px; padding: 0;
    }}
    #InfoCloseBtn:hover {{ background: {t['bg_hover']}; color: {t['fg']}; }}
    #InfoEditBtn {{
        background: {t['bg_hover']}; color: {t['fg2']};
        border: 1px solid {t['line']}; border-radius: 4px;
        font-size: 12px; padding: 2px 6px;
    }}
    #InfoEditBtn:hover {{ background: {t['accent_soft']}; color: {t['accent']}; }}

    /* ── Room search dialog ────────────────────────────────── */
    #SearchResultRow {{
        background: {t['bg_chat']};
        border-bottom: 1px solid {t['line']};
        border-radius: 0px;
    }}
    #SearchResultRow:hover {{ background: {t['bg_hover']}; }}
    #DialogCloseBtn {{
        background: transparent; color: {t['fg3']}; border: none;
        border-radius: 4px; font-size: 18px; padding: 0;
    }}
    #DialogCloseBtn:hover {{ background: {t['bg_hover']}; color: {t['fg']}; }}
    #PrimaryBtn {{
        background: {t['accent']}; color: white;
        border: none; border-radius: 6px;
        font-size: 12px; font-weight: 500;
    }}
    #PrimaryBtn:hover {{ background: {t['accent']}; opacity: 0.9; }}

    /* ── Update banner ──────────────────────────────────────── */
    #UpdateBar   {{ background: {t['accent']}; }}
    #UpdateBarLabel {{ color: white; font-size: 12px; }}
    #UpdateBarBtn {{
        background: white; color: {t['accent']};
        border: none; border-radius: 4px;
        font-size: 12px; font-weight: 600; padding: 2px 10px;
    }}
    #UpdateBarBtn:hover {{ background: #f0f0f0; }}
    """
