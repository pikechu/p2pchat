import hashlib
import pathlib
import sys

import updater


class _Response:
    def __init__(self, data: bytes, *, content_length: bool = True):
        self._data = data
        self._offset = 0
        self.headers = {"Content-Length": str(len(data))} if content_length else {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self._data) - self._offset
        result = self._data[self._offset:self._offset + size]
        self._offset += len(result)
        return result


def test_update_downloader_verifies_sha256_before_success(tmp_path, monkeypatch):
    payload = b"signed release payload"
    checksum = hashlib.sha256(payload).hexdigest().encode("ascii") + b"  BeamChat.exe"
    responses = iter([_Response(checksum), _Response(payload)])
    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr(sys, "executable", str(tmp_path / "BeamChat.exe"))

    finished = []
    failed = []
    downloader = updater.UpdateDownloader(
        "https://example.test/BeamChat.exe",
        checksum_url="https://example.test/BeamChat.exe.sha256",
    )
    downloader.finished.connect(finished.append)
    downloader.failed.connect(failed.append)
    downloader.run()

    assert failed == []
    assert pathlib.Path(finished[0]).read_bytes() == payload


def test_update_downloader_removes_tampered_payload(tmp_path, monkeypatch):
    checksum = hashlib.sha256(b"expected").hexdigest().encode("ascii")
    responses = iter([_Response(checksum), _Response(b"tampered")])
    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr(sys, "executable", str(tmp_path / "BeamChat.exe"))

    failed = []
    downloader = updater.UpdateDownloader(
        "https://example.test/BeamChat.exe",
        checksum_url="https://example.test/BeamChat.exe.sha256",
    )
    downloader.failed.connect(failed.append)
    downloader.run()

    assert "SHA-256" in failed[0]
    assert not (tmp_path / "_BeamChat_update.exe").exists()
