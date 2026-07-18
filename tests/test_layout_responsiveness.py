import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from PyQt6.QtWidgets import QApplication, QSplitter

    app = QApplication.instance() or QApplication(sys.argv)
except Exception:
    pytest.skip("No display", allow_module_level=True)

# 测试只覆盖布局；CI 容器不需要加载系统 PortAudio。
if "sounddevice" not in sys.modules:
    sys.modules["sounddevice"] = types.SimpleNamespace(
        InputStream=type("InputStream", (), {}),
        OutputStream=type("OutputStream", (), {}),
    )

from gui.theme import make_qss
from gui.widgets import FileCard, ImageCard, MessageRow, VideoCard
from gui.window import ChatPanel, MessagesArea


def test_chat_content_uses_splitter_for_responsive_info_panel():
    panel = ChatPanel()

    assert isinstance(panel._middle_splitter, QSplitter)
    assert panel._middle_splitter.widget(0) is panel._msgs_stack
    assert panel._middle_splitter.widget(1) is panel._info_panel
    assert panel._info_panel.minimumWidth() < panel._info_panel.maximumWidth()


def test_text_and_file_messages_share_message_row():
    messages = MessagesArea(own_name="me")
    messages.add_message("me", "hello", 1, outgoing=True)
    card = FileCard("transfer", "notes.txt", 128, outgoing=True)
    messages.add_file_card(card)
    image = ImageCard("image", "photo.png", b"", outgoing=False)
    image._sender = "alice"
    messages.add_file_card(image)
    video = VideoCard("video", "clip.mp4", 1024, outgoing=False)
    video._sender = "alice"
    messages.add_file_card(video)

    rows = messages._container.findChildren(MessageRow)
    assert len(rows) == 4
    assert rows[0].content.objectName() == "BubbleOut"
    assert rows[1].content is card
    assert rows[2].content is image
    assert rows[3].content is video


def test_message_row_replaces_transfer_card_in_place():
    messages = MessagesArea(own_name="me")
    card = FileCard("transfer", "photo.png", 128, outgoing=False)
    card._sender = "alice"
    messages.add_file_card(card)
    row = messages._container.findChild(MessageRow)
    image = ImageCard("transfer", "photo.png", b"", outgoing=False)

    row.replace_content(image)

    assert row.content is image
    assert image.parentWidget() is row
    assert card.parentWidget() is None


def test_layout_qss_does_not_duplicate_header_or_bubble_padding():
    qss = make_qss("light")

    chat_header_rule = qss.split("#ChatHeader", 1)[1].split("}", 1)[0]
    bubble_rule = qss.split("#BubbleIn", 1)[1].split("}", 1)[0]
    assert "padding: 0;" in chat_header_rule
    assert "padding:" not in bubble_rule


def test_file_card_can_shrink_and_expand():
    card = FileCard("responsive", "a-long-file-name.txt", 128)

    assert card.minimumWidth() <= 160
    assert card.maximumWidth() >= 400
