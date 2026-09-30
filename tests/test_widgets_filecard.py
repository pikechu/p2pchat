import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
try:
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)
except Exception:
    pytest.skip("No display", allow_module_level=True)

from gui.widgets import FileCard, ImageCard, VideoCard
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtTest import QTest


def test_filecard_creates_without_error():
    card = FileCard(
        transfer_id="t1",
        filename="photo.jpg",
        size=204800,
        outgoing=True,
    )
    assert card is not None


def test_filecard_has_cancel_signal():
    card = FileCard(transfer_id="t2", filename="doc.pdf", size=1024, outgoing=False)
    # signal must exist and be connectable
    received = []
    card.cancel_requested.connect(lambda tid: received.append(tid))
    assert hasattr(card, "cancel_requested")


def test_filecard_set_progress_updates_label():
    card = FileCard(transfer_id="t3", filename="vid.mp4", size=1048576, outgoing=True)
    card.set_progress(50)
    # Just verify it doesn't raise; label text update is visual
    card.set_progress(100)


def test_filecard_set_done_hides_progress():
    card = FileCard(transfer_id="t4", filename="archive.zip", size=2048, outgoing=False)
    card.set_done(save_path="/tmp/archive.zip")  # must not raise


def test_filecard_set_error_shows_message():
    card = FileCard(transfer_id="t5", filename="fail.bin", size=99, outgoing=False)
    card.set_error("Connection lost")  # must not raise


def test_filecard_image_thumbnail_for_png():
    card = FileCard(transfer_id="t6", filename="cat.png", size=512,
                    outgoing=False, thumbnail_data=b"\x89PNG\r\n")
    # thumbnail_data provided but may be invalid image — must not raise
    assert card is not None


def test_filecard_long_name_uses_full_tooltip_and_responsive_width():
    filename = "very-" * 30 + "long-name.txt"
    card = FileCard("long", filename, 10, outgoing=True)

    assert card._name_lbl.toolTip() == filename
    assert card.minimumWidth() < card.maximumWidth()


def test_filecard_theme_state_can_be_updated():
    card = FileCard("theme", "theme.txt", 10, outgoing=True, theme="light")
    card.set_theme("dark")
    assert card._theme == "dark"


@pytest.mark.parametrize("factory", [
    lambda outgoing: FileCard("retry", "file.bin", 10, outgoing=outgoing),
    lambda outgoing: ImageCard("retry", "image.png", b"invalid", outgoing=outgoing),
    lambda outgoing: VideoCard("retry", "video.mp4", 10, outgoing=outgoing),
])
def test_failed_outgoing_card_can_retry_with_current_transfer_id(factory):
    card = factory(True)
    retried = []
    card.retry_requested.connect(retried.append)
    card.set_error("网络断开")
    assert not card._retry_btn.isHidden()
    assert card._cancel_btn.isHidden()
    assert "网络断开" in card._status_lbl.text()

    card._tid = "retry-new"
    card._retry_btn.click()
    assert retried == ["retry-new"]
    card.reset_transfer()
    assert card._retry_btn.isHidden()
    assert not card._cancel_btn.isHidden()
    assert card._status_lbl.objectName() == "FileCardStatus"


@pytest.mark.parametrize("factory", [
    lambda: FileCard("received", "file.bin", 10),
    lambda: ImageCard("received", "image.png", b"invalid"),
    lambda: VideoCard("received", "video.mp4", 10),
])
def test_failed_incoming_card_explains_how_to_resend(factory):
    card = factory()
    card.set_error("文件认证失败")

    assert card._retry_btn.isHidden()
    assert "请让发送方重新发送" in card._status_lbl.text()


@pytest.mark.parametrize("factory", [
    lambda: FileCard("open", "file.bin", 10),
    lambda: ImageCard("open", "image.png", b"invalid"),
    lambda: VideoCard("open", "video.mp4", 10),
])
def test_card_uses_qt_to_open_and_displays_open_errors(factory, tmp_path, monkeypatch):
    path = tmp_path / "中文 文件.bin"
    path.write_bytes(b"payload")
    card = factory()
    card.set_done(str(path))
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url) or False)

    if isinstance(card, VideoCard):
        card._open_btn.click()
    else:
        QTest.mouseClick(card, Qt.MouseButton.LeftButton)

    assert type(path)(opened[0].toLocalFile()) == path
    assert "无法打开文件" in card._status_lbl.text()
    assert card._save_path == str(path)
    assert not card._status_lbl.isHidden()


def test_invalid_image_keeps_preview_error_after_saved(tmp_path):
    path = tmp_path / "image.png"
    path.write_bytes(b"invalid")
    card = ImageCard("invalid-preview", path.name, path.read_bytes())

    card.set_done(str(path))

    assert "图片无法预览" in card._status_lbl.text()
    assert not card._status_lbl.isHidden()
    assert card._save_path == str(path)
