"""使用真实 Qt 事件循环验证后台更新通知的线程派发。"""

import pathlib
import threading

import pytest

pytest.importorskip("PyQt6")
from PyQt6.QtCore import QEventLoop, QTimer
from PyQt6.QtWidgets import QApplication, QLabel, QMainWindow, QWidget


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def update_window(app, tmp_path, monkeypatch):
    # 导入日志也写入测试目录；不构造身份、网络连接或完整主界面。
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
    from gui.window import MainWindow

    class UpdateWindow(MainWindow):
        def __init__(self):
            QMainWindow.__init__(self)
            self._update_found.connect(self._on_update_found)
            self._update_lbl = QLabel(self)
            self._update_bar = QWidget(self)
            self._update_bar.hide()
            self.notifications = []

        def _show_update_bar(self, ver):
            self.notifications.append((ver, threading.current_thread()))
            MainWindow._show_update_bar(self, ver)

    window = UpdateWindow()
    yield window
    window.deleteLater()
    app.processEvents()


def _wait_for_worker_events(window, worker_done, app):
    """处理跨线程排队信号，并让未派发的旧回调明确失败。"""
    loop = QEventLoop()
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    timer.start(100)
    loop.exec()
    assert worker_done.wait(2), "更新检查工作线程未完成"
    app.processEvents()


def test_background_update_notifies_on_gui_thread(update_window, app, monkeypatch):
    import updater

    worker_done = threading.Event()
    worker_threads = []
    url = "https://example.test/releases/v1.2.3/BeamChat.exe"

    def check_update():
        worker_threads.append(threading.current_thread())
        worker_done.set()
        return "1.2.3", url, None

    monkeypatch.setattr(updater, "check_update", check_update)
    update_window._check_update_bg()
    _wait_for_worker_events(update_window, worker_done, app)

    assert worker_threads[0] is not threading.main_thread()
    assert update_window.notifications == [("1.2.3", threading.main_thread())]
    assert update_window._update_available_ver == "1.2.3"
    assert update_window._update_available_url == url
    assert "v1.2.3" in update_window._update_lbl.text()
    assert not update_window._update_bar.isHidden()


@pytest.mark.parametrize("result", [
    (None, None, None),
    ("1.2.3", None, None),
    (None, None, "网络连接失败"),
])
def test_background_check_without_installable_update_keeps_banner_hidden(
    update_window, app, monkeypatch, result,
):
    import updater

    worker_done = threading.Event()

    def check_update():
        worker_done.set()
        return result

    monkeypatch.setattr(updater, "check_update", check_update)
    update_window._check_update_bg()
    _wait_for_worker_events(update_window, worker_done, app)

    assert update_window.notifications == []
    assert update_window._update_bar.isHidden()
    assert not hasattr(update_window, "_update_available_url")
