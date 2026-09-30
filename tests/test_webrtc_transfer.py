import asyncio
import json
import logging
import pathlib
import sys
import types

import pytest

from file_transfer import CHUNK_SIZE, EncryptedFileSender, file_sha256, split_file
from protocol import T
from webrtc_transfer import WebRTCTransfer

TEST_WEBRTC_FILE_KEY = b"W" * 32


def _test_file_key(_peer: str, _session_id: str) -> bytes:
    return TEST_WEBRTC_FILE_KEY


class FakeDescription:
    def __init__(self, type_: str, sdp: str):
        self.type = type_
        self.sdp = sdp


class FakeDataChannel:
    def __init__(self, label: str):
        self.label = label
        self.sent = []
        self.handlers = {}

    def send(self, message: str):
        self.sent.append(message)

    def on(self, event: str):
        def register(handler):
            self.handlers[event] = handler
            return handler
        return register


class FakePeerConnection:
    def __init__(self):
        self.channels = []
        self.localDescription = None
        self.remote_descriptions = []
        self.ice_candidates = []
        self.handlers = {}

    def createDataChannel(self, label):
        channel = FakeDataChannel(label)
        self.channels.append(channel)
        return channel

    async def createOffer(self):
        return FakeDescription("offer", "offer-sdp")

    async def createAnswer(self):
        return FakeDescription("answer", "answer-sdp")

    async def setLocalDescription(self, description):
        self.localDescription = description

    async def setRemoteDescription(self, description):
        self.remote_descriptions.append(description)

    async def addIceCandidate(self, candidate):
        self.ice_candidates.append(candidate)

    async def close(self):
        self.closed = True

    def on(self, event: str):
        def register(handler):
            self.handlers[event] = handler
            return handler
        return register


def _run(coro):
    return asyncio.run(coro)


def _encrypted_webrtc_messages(tmp_path, transfer_id: str, filename: str, data: bytes):
    source_dir = tmp_path / f"src-{transfer_id}"
    source_dir.mkdir(parents=True, exist_ok=True)
    path = source_dir / filename
    path.write_bytes(data)
    sender = EncryptedFileSender(
        path,
        TEST_WEBRTC_FILE_KEY,
        transfer_id=transfer_id,
        scope_type="dm",
        scope_id=f"webrtc:{transfer_id}",
        sender="alice",
        recipient="me",
    )
    offer = sender.offer_payload()
    messages = [{
        "kind": "file-start",
        "transfer_id": transfer_id,
        **offer,
    }]
    while payload := sender.next_payload():
        messages.append({
            "kind": "file-chunk",
            "transfer_id": transfer_id,
            "index": payload["index"],
            "total": payload["total"],
            "encrypted_chunk": payload["encrypted_chunk"],
        })
    messages.append({
        "kind": "file-done",
        "transfer_id": transfer_id,
        **sender.done_payload(),
    })
    return messages


def test_start_offer_creates_data_channel_and_sends_offer(tmp_path):
    sent = []
    peers = []
    path = tmp_path / "file.bin"
    path.write_bytes(b"abc")

    def peer_factory():
        peer = FakePeerConnection()
        peers.append(peer)
        return peer

    transfer = WebRTCTransfer(lambda msg_type, **payload: sent.append((msg_type, payload)),
                              peer_factory=peer_factory,
                              file_key_provider=_test_file_key,
                              local_user="me")

    session_id = _run(transfer.start_offer("bob", path, session_id="s1"))

    assert session_id == "s1"
    assert peers[0].channels[0].label == "file"
    assert sent == [(
        T.WEBRTC_OFFER,
        {
            "to": "bob",
            "session_id": "s1",
            "sdp": {"type": "offer", "sdp": "offer-sdp"},
        },
    )]


def test_handle_offer_sends_answer():
    sent = []
    peers = []

    def peer_factory():
        peer = FakePeerConnection()
        peers.append(peer)
        return peer

    transfer = WebRTCTransfer(lambda msg_type, **payload: sent.append((msg_type, payload)),
                              peer_factory=peer_factory,
                              file_key_provider=_test_file_key,
                              local_user="me")

    _run(transfer.handle_offer({
        "from": "alice",
        "session_id": "s2",
        "sdp": {"type": "offer", "sdp": "offer-sdp"},
    }))

    assert peers[0].remote_descriptions[0].type == "offer"
    assert sent == [(
        T.WEBRTC_ANSWER,
        {
            "to": "alice",
            "session_id": "s2",
            "sdp": {"type": "answer", "sdp": "answer-sdp"},
        },
    )]


def test_handle_answer_and_ice_update_existing_session():
    peers = []

    def peer_factory():
        peer = FakePeerConnection()
        peers.append(peer)
        return peer

    transfer = WebRTCTransfer(lambda *_args, **_kwargs: None,
                              peer_factory=peer_factory,
                              file_key_provider=_test_file_key,
                              local_user="me")
    _run(transfer.start_offer("bob", pathlib.Path(__file__), session_id="s3"))

    _run(transfer.handle_answer({
        "from": "bob",
        "session_id": "s3",
        "sdp": {"type": "answer", "sdp": "answer-sdp"},
    }))
    _run(transfer.handle_ice({
        "from": "bob",
        "session_id": "s3",
        "candidate": {"candidate": "candidate:1", "sdpMid": "0", "sdpMLineIndex": 0},
    }))

    assert peers[0].remote_descriptions[0].type == "answer"
    assert peers[0].ice_candidates == [
        {"candidate": "candidate:1", "sdpMid": "0", "sdpMLineIndex": 0}
    ]


def test_handle_answer_for_unknown_session_raises():
    transfer = WebRTCTransfer(lambda *_args, **_kwargs: None,
                              peer_factory=FakePeerConnection,
                              file_key_provider=_test_file_key,
                              local_user="me")

    with pytest.raises(KeyError, match="unknown WebRTC session"):
        _run(transfer.handle_answer({
            "from": "bob",
            "session_id": "missing",
            "sdp": {"type": "answer", "sdp": "answer-sdp"},
        }))


def test_send_file_writes_start_chunks_and_done_frames(tmp_path):
    peers = []
    path = tmp_path / "payload.bin"
    data = b"A" * (CHUNK_SIZE + 3)
    path.write_bytes(data)

    def peer_factory():
        peer = FakePeerConnection()
        peers.append(peer)
        return peer

    transfer = WebRTCTransfer(lambda *_args, **_kwargs: None,
                              peer_factory=peer_factory,
                              file_key_provider=_test_file_key,
                              local_user="me")
    _run(transfer.start_offer("bob", path, session_id="send1"))

    _run(transfer.send_file("send1"))

    frames = [json.loads(raw) for raw in peers[0].channels[0].sent]
    relay_json = json.dumps(frames)
    assert "payload.bin" not in relay_json
    assert split_file(data)[0] not in relay_json
    assert file_sha256(data) not in relay_json
    assert frames[0]["kind"] == "file-start"
    assert frames[0]["transfer_id"] == "send1"
    assert frames[0]["size"] == len(data)
    assert "encrypted_metadata" in frames[0]
    assert frames[1]["kind"] == "file-chunk"
    assert frames[1]["index"] == 0
    assert frames[1]["total"] == 2
    assert "encrypted_chunk" in frames[1]
    assert "data" not in frames[1]
    assert frames[2]["kind"] == "file-chunk"
    assert frames[2]["index"] == 1
    assert frames[3]["kind"] == "file-done"
    assert frames[3]["transfer_id"] == "send1"
    assert "encrypted_done" in frames[3]
    assert "sha256" not in frames[3]


def test_handle_data_message_reassembles_file(tmp_path):
    data = b"hello over datachannel"
    transfer = WebRTCTransfer(lambda *_args, **_kwargs: None,
                              peer_factory=FakePeerConnection,
                              downloads_dir=tmp_path,
                              file_key_provider=_test_file_key,
                              local_user="me")

    save_path = None
    for message in _encrypted_webrtc_messages(tmp_path, "recv1", "safe.txt", data):
        save_path = transfer.handle_data_message("alice", json.dumps(message))

    assert save_path is not None
    assert save_path.parent == tmp_path
    assert save_path.name == "safe.txt"
    assert save_path.read_bytes() == data


def test_start_offer_sends_local_ice_candidates(tmp_path):
    sent = []
    peers = []
    path = tmp_path / "file.bin"
    path.write_bytes(b"abc")

    def peer_factory():
        peer = FakePeerConnection()
        peers.append(peer)
        return peer

    transfer = WebRTCTransfer(lambda msg_type, **payload: sent.append((msg_type, payload)),
                              peer_factory=peer_factory,
                              file_key_provider=_test_file_key,
                              local_user="me")
    _run(transfer.start_offer("bob", path, session_id="ice1"))

    peers[0].handlers["icecandidate"]({"candidate": "candidate:1"})

    assert sent[-1] == (
        T.WEBRTC_ICE,
        {"to": "bob", "session_id": "ice1", "candidate": {"candidate": "candidate:1"}},
    )


def test_incoming_datachannel_message_is_reassembled(tmp_path):
    sent = []
    peers = []
    data = b"from remote channel"

    def peer_factory():
        peer = FakePeerConnection()
        peers.append(peer)
        return peer

    transfer = WebRTCTransfer(lambda msg_type, **payload: sent.append((msg_type, payload)),
                              peer_factory=peer_factory,
                              downloads_dir=tmp_path,
                              file_key_provider=_test_file_key,
                              local_user="me")
    _run(transfer.handle_offer({
        "from": "alice",
        "session_id": "dc1",
        "sdp": {"type": "offer", "sdp": "offer-sdp"},
    }))
    channel = FakeDataChannel("file")
    peers[0].handlers["datachannel"](channel)

    save_path = None
    for message in _encrypted_webrtc_messages(tmp_path, "dc1", "remote.bin", data):
        save_path = channel.handlers["message"](json.dumps(message))

    assert save_path is not None
    assert save_path.name == "remote.bin"
    assert save_path.read_bytes() == data


def test_offer_channel_open_sends_file(tmp_path):
    peers = []
    path = tmp_path / "auto.bin"
    path.write_bytes(b"auto-send")

    def peer_factory():
        peer = FakePeerConnection()
        peers.append(peer)
        return peer

    transfer = WebRTCTransfer(lambda *_args, **_kwargs: None,
                              peer_factory=peer_factory,
                              file_key_provider=_test_file_key,
                              local_user="me")
    _run(transfer.start_offer("bob", path, session_id="open1"))

    peers[0].channels[0].handlers["open"]()

    frames = [json.loads(raw) for raw in peers[0].channels[0].sent]
    assert frames[0]["kind"] == "file-start"
    assert frames[-1]["kind"] == "file-done"


def test_channel_open_callback_gets_session_meta(tmp_path):
    opened = []
    peers = []
    path = tmp_path / "open.bin"
    path.write_bytes(b"open")

    def peer_factory():
        peer = FakePeerConnection()
        peers.append(peer)
        return peer

    transfer = WebRTCTransfer(lambda *_args, **_kwargs: None,
                              peer_factory=peer_factory,
                              on_channel_open=lambda meta: opened.append(meta),
                              file_key_provider=_test_file_key,
                              local_user="me")
    _run(transfer.start_offer("bob", path, session_id="open-cb"))

    peers[0].channels[0].handlers["open"]()

    assert opened == [{
        "peer": "bob",
        "session_id": "open-cb",
        "filename": "open.bin",
        "size": 4,
    }]


def test_default_peer_factory_passes_ice_configuration(monkeypatch):
    created = []
    fake_aiortc = types.ModuleType("aiortc")

    class FakeRTCIceServer:
        def __init__(self, urls, username=None, credential=None):
            self.urls = urls
            self.username = username
            self.credential = credential

    class FakeRTCConfiguration:
        def __init__(self, iceServers):
            self.iceServers = iceServers

    class FakeRTCPeerConnection:
        def __init__(self, configuration=None):
            created.append(configuration)

    fake_aiortc.RTCIceServer = FakeRTCIceServer
    fake_aiortc.RTCConfiguration = FakeRTCConfiguration
    fake_aiortc.RTCPeerConnection = FakeRTCPeerConnection
    monkeypatch.setitem(sys.modules, "aiortc", fake_aiortc)

    transfer = WebRTCTransfer(lambda *_args, **_kwargs: None,
                              ice_servers=[{"urls": ["turn:turn.example.com"], "username": "u", "credential": "p"}],
                              file_key_provider=_test_file_key,
                              local_user="me")
    peer = transfer._default_peer_factory()

    assert isinstance(peer, FakeRTCPeerConnection)
    assert created[0].iceServers[0].urls == ["turn:turn.example.com"]
    assert created[0].iceServers[0].username == "u"
    assert created[0].iceServers[0].credential == "p"


def test_receive_complete_callback_gets_saved_path(tmp_path):
    completed = []
    data = b"callback-data"
    transfer = WebRTCTransfer(lambda *_args, **_kwargs: None,
                              peer_factory=FakePeerConnection,
                              downloads_dir=tmp_path,
                              on_file_received=lambda path, meta: completed.append((path, meta)),
                              file_key_provider=_test_file_key,
                              local_user="me")

    save_path = None
    for message in _encrypted_webrtc_messages(tmp_path, "cb1", "callback.bin", data):
        save_path = transfer.handle_data_message("alice", json.dumps(message))

    assert completed == [(
        save_path,
        {
            "from_user": "alice",
            "transfer_id": "cb1",
            "filename": "callback.bin",
            "size": len(data),
            "mime": "application/octet-stream",
        },
    )]


def test_send_complete_callback_gets_source_path_and_meta(tmp_path):
    sent = []
    completed = []
    peers = []
    path = tmp_path / "sent.bin"
    path.write_bytes(b"sent-data")

    def peer_factory():
        peer = FakePeerConnection()
        peers.append(peer)
        return peer

    transfer = WebRTCTransfer(lambda msg_type, **payload: sent.append((msg_type, payload)),
                              peer_factory=peer_factory,
                              on_file_sent=lambda path, meta: completed.append((path, meta)),
                              file_key_provider=_test_file_key,
                              local_user="me")
    _run(transfer.start_offer("bob", path, session_id="sent1"))

    _run(transfer.send_file("sent1"))

    assert completed == [(
        path,
        {
            "to_user": "bob",
            "transfer_id": "sent1",
            "filename": "sent.bin",
            "size": len(b"sent-data"),
            "mime": "application/octet-stream",
        },
    )]


def test_send_file_reports_chunk_progress(tmp_path):
    progress = []
    peers = []
    path = tmp_path / "progress.bin"
    data = b"P" * (CHUNK_SIZE + 1)
    path.write_bytes(data)

    def peer_factory():
        peer = FakePeerConnection()
        peers.append(peer)
        return peer

    transfer = WebRTCTransfer(lambda *_args, **_kwargs: None,
                              peer_factory=peer_factory,
                              on_file_progress=lambda meta: progress.append(meta),
                              file_key_provider=_test_file_key,
                              local_user="me")
    _run(transfer.start_offer("bob", path, session_id="prog1"))

    _run(transfer.send_file("prog1"))

    assert [item["progress"] for item in progress] == [0, 50, 100]
    assert progress[-1] == {
        "direction": "send",
        "peer": "bob",
        "transfer_id": "prog1",
        "filename": "progress.bin",
        "size": len(data),
        "progress": 100,
    }


def test_receive_datachannel_reports_chunk_progress(tmp_path):
    progress = []
    data = b"R" * (CHUNK_SIZE + 1)
    transfer = WebRTCTransfer(lambda *_args, **_kwargs: None,
                              peer_factory=FakePeerConnection,
                              downloads_dir=tmp_path,
                              on_file_progress=lambda meta: progress.append(meta),
                              file_key_provider=_test_file_key,
                              local_user="me")

    for message in _encrypted_webrtc_messages(tmp_path, "recv-progress", "recv-progress.bin", data)[:-1]:
        transfer.handle_data_message("alice", json.dumps(message))

    assert [item["progress"] for item in progress] == [0, 50, 100]
    assert progress[-1]["direction"] == "receive"
    assert progress[-1]["peer"] == "alice"


def test_datachannel_close_reports_session_closed(tmp_path):
    closed = []
    peers = []
    path = tmp_path / "closing.bin"
    path.write_bytes(b"abc")

    def peer_factory():
        peer = FakePeerConnection()
        peers.append(peer)
        return peer

    transfer = WebRTCTransfer(lambda *_args, **_kwargs: None,
                              peer_factory=peer_factory,
                              on_session_closed=lambda meta: closed.append(meta),
                              file_key_provider=_test_file_key,
                              local_user="me")
    _run(transfer.start_offer("bob", path, session_id="close1"))

    peers[0].channels[0].handlers["close"]()

    assert closed == [{
        "peer": "bob",
        "session_id": "close1",
        "message": "DataChannel closed",
    }]
    assert transfer.get_session_peer("close1") is None
    assert peers[0].closed


def test_failed_offer_closes_peer_connection(tmp_path):
    peer = FakePeerConnection()
    path = tmp_path / "offer.bin"
    path.write_bytes(b"payload")
    transfer = WebRTCTransfer(lambda *_args, **_kwargs: False,
                              peer_factory=lambda: peer,
                              file_key_provider=_test_file_key,
                              local_user="me")

    with pytest.raises(RuntimeError, match="信令发送失败"):
        _run(transfer.start_offer("bob", path, session_id="failed-offer"))

    assert peer.closed
    assert transfer.get_session_peer("failed-offer") is None


def test_failed_datachannel_creation_closes_peer_connection(tmp_path, monkeypatch):
    peer = FakePeerConnection()
    path = tmp_path / "channel.bin"
    path.write_bytes(b"payload")

    def fail_channel(_label):
        raise RuntimeError("创建通道失败")

    monkeypatch.setattr(peer, "createDataChannel", fail_channel)
    transfer = WebRTCTransfer(lambda *_args, **_kwargs: None,
                              peer_factory=lambda: peer,
                              file_key_provider=_test_file_key, local_user="me")

    with pytest.raises(RuntimeError, match="创建通道失败"):
        _run(transfer.start_offer("bob", path, session_id="channel-failed"))

    assert peer.closed
    assert transfer.get_session_peer("channel-failed") is None


def test_channel_send_failure_reports_error_and_closes_sender(tmp_path, monkeypatch):
    import webrtc_transfer
    created = []
    closed = []

    def sender_factory(*args, **kwargs):
        sender = EncryptedFileSender(*args, **kwargs)
        created.append(sender)
        return sender

    monkeypatch.setattr(webrtc_transfer, "EncryptedFileSender", sender_factory)
    path = tmp_path / "send-failed.bin"
    path.write_bytes(b"P" * (CHUNK_SIZE + 1))
    peer = FakePeerConnection()
    transfer = WebRTCTransfer(lambda *_args, **_kwargs: None,
                              peer_factory=lambda: peer, on_session_closed=closed.append,
                              file_key_provider=_test_file_key, local_user="me")
    _run(transfer.start_offer("bob", path, session_id="send-failed"))

    def fail_send(_message):
        raise OSError("通道已断开")

    monkeypatch.setattr(peer.channels[0], "send", fail_send)
    peer.channels[0].handlers["open"]()

    assert created[0]._src.closed
    assert peer.closed
    assert transfer.get_session_peer("send-failed") is None
    assert "文件发送失败" in closed[0]["message"]


def test_close_all_removes_unfinished_incoming_temp_files(tmp_path):
    peers = []

    def factory():
        peer = FakePeerConnection()
        peers.append(peer)
        return peer

    transfer = WebRTCTransfer(lambda *_args, **_kwargs: None,
                              peer_factory=factory, downloads_dir=tmp_path,
                              file_key_provider=_test_file_key, local_user="me")
    for tid in ("unfinished1", "unfinished2"):
        _run(transfer.handle_offer({"from": "alice", "session_id": tid,
                                   "sdp": {"type": "offer", "sdp": "offer-sdp"}}))
        for message in _encrypted_webrtc_messages(tmp_path, tid, "partial.bin", b"P" * (CHUNK_SIZE + 1))[:2]:
            transfer.handle_data_message("alice", json.dumps(message))
        assert (tmp_path / f".{tid}.part").exists()

    _run(transfer.close_all())
    _run(transfer.close_all())

    assert not list(tmp_path.glob(".*.part"))
    assert not transfer._incoming_receivers
    assert not transfer._incoming_meta
    assert not transfer._sessions
    assert all(peer.closed for peer in peers)


def test_invalid_chunk_index_cleans_temp_file_and_reports_failure(tmp_path):
    closed = []
    transfer = WebRTCTransfer(lambda *_args, **_kwargs: None,
                              peer_factory=FakePeerConnection, downloads_dir=tmp_path,
                              on_session_closed=closed.append,
                              file_key_provider=_test_file_key, local_user="me")
    messages = _encrypted_webrtc_messages(tmp_path, "bad-index", "partial.bin", b"P" * (CHUNK_SIZE + 1))
    for message in messages[:2]:
        transfer.handle_data_message("alice", json.dumps(message))
    assert (tmp_path / ".bad-index.part").exists()
    messages[2]["index"] = "invalid"

    transfer.handle_data_message("alice", json.dumps(messages[2]))

    assert not (tmp_path / ".bad-index.part").exists()
    assert not transfer._incoming_receivers
    assert closed[0]["message"] == "文件分块认证失败"


def test_close_cancels_active_sender_and_closes_source(tmp_path, monkeypatch):
    import webrtc_transfer
    created = []
    original_sender = EncryptedFileSender

    def sender_factory(*args, **kwargs):
        sender = original_sender(*args, **kwargs)
        created.append(sender)
        return sender

    monkeypatch.setattr(webrtc_transfer, "EncryptedFileSender", sender_factory)
    path = tmp_path / "active.bin"
    path.write_bytes(b"P" * (CHUNK_SIZE + 1))
    peer = FakePeerConnection()
    transfer = WebRTCTransfer(lambda *_args, **_kwargs: None,
                              peer_factory=lambda: peer,
                              file_key_provider=_test_file_key, local_user="me")

    async def exercise():
        await transfer.start_offer("bob", path, session_id="active")
        peer.channels[0].bufferedAmount = 2_000_000
        task = asyncio.create_task(transfer.send_file("active"))
        await asyncio.sleep(0)
        assert created and not created[0]._src.closed
        await transfer.close("active")
        assert task.cancelled()

    _run(exercise())

    assert created[0]._src.closed
    assert peer.closed
    assert not transfer._send_tasks
    assert transfer.get_session_peer("active") is None


def test_webrtc_transfer_logs_key_session_events(tmp_path, caplog):
    peers = []
    path = tmp_path / "logged.bin"
    path.write_bytes(b"abc")

    def peer_factory():
        peer = FakePeerConnection()
        peers.append(peer)
        return peer

    transfer = WebRTCTransfer(lambda *_args, **_kwargs: None,
                              peer_factory=peer_factory,
                              file_key_provider=_test_file_key,
                              local_user="me")

    with caplog.at_level(logging.INFO, logger="webrtc_transfer"):
        _run(transfer.start_offer("bob", path, session_id="log1"))
        peers[0].channels[0].handlers["open"]()
        _run(transfer.handle_answer({
            "from": "bob",
            "session_id": "log1",
            "sdp": {"type": "answer", "sdp": "answer-sdp"},
        }))
        peers[0].channels[0].handlers["close"]()

    messages = [record.getMessage() for record in caplog.records]
    assert any("WEBRTC start_offer session=log1 peer=bob filename=logged.bin size=3" in msg
               for msg in messages)
    assert any("WEBRTC datachannel_open session=log1 peer=bob" in msg
               for msg in messages)
    assert any("WEBRTC handle_answer session=log1 peer=bob" in msg
               for msg in messages)
    assert any("WEBRTC datachannel_close session=log1 peer=bob" in msg
               for msg in messages)
