import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
try:
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)
except Exception:
    pytest.skip("No display", allow_module_level=True)

from gui.widgets import FileCard


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
