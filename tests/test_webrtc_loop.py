"""验证直连使用持久循环，并通过 Qt 将后台失败交回界面线程。"""

import asyncio
import threading
import types
import sys

import pytest

pytest.importorskip("PyQt6")
# 循环测试不使用录音设备，容器中无需安装 PortAudio。
sys.modules.setdefault("sounddevice", types.SimpleNamespace(InputStream=object, OutputStream=object))
from PyQt6.QtWidgets import QApplication, QMainWindow
from gui.window import MainWindow


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def worker_loop():
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def run():
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        loop.run_forever()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert ready.wait(2)
    try:
        yield loop, thread
    finally:
        async def cleanup():
            tasks = asyncio.all_tasks() - {asyncio.current_task()}
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        asyncio.run_coroutine_threadsafe(cleanup(), loop).result(timeout=2)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        assert not thread.is_alive()
        loop.close()


def test_webrtc_background_tasks_survive_offer_return(worker_loop, app):
    loop, thread = worker_loop
    window = MainWindow.__new__(MainWindow)
    QMainWindow.__init__(window)
    window._bridge = types.SimpleNamespace(_loop=loop)
    background_done = threading.Event()

    async def offer():
        async def ice_task():
            await asyncio.sleep(0.01)
            background_done.set()

        asyncio.create_task(ice_task())
        return threading.get_ident()

    future = MainWindow._run_webrtc_task(window, offer(), raise_errors=True)

    assert future.result(timeout=2) == thread.ident
    assert background_done.wait(2)
    assert loop.is_running()
    window.deleteLater()


def test_webrtc_async_failure_callback_runs_on_gui_thread(worker_loop, app):
    loop, _thread = worker_loop
    window = MainWindow.__new__(MainWindow)
    QMainWindow.__init__(window)
    window._bridge = types.SimpleNamespace(_loop=loop)
    window._webrtc_task_failed.connect(window._on_webrtc_task_failed)
    calls = []

    async def fail():
        raise OSError("连接失败")

    future = MainWindow._run_webrtc_task(
        window, fail(), on_error=lambda error: calls.append((threading.get_ident(), str(error))),
    )
    with pytest.raises(OSError, match="连接失败"):
        future.result(timeout=2)
    queued = threading.Event()
    loop.call_soon_threadsafe(queued.set)
    assert queued.wait(2)
    app.processEvents()

    assert calls == [(threading.get_ident(), "连接失败")]
    window.deleteLater()
