import asyncio
import json
import logging
import pathlib
import sys
import types

import pytest

from file_transfer import CHUNK_SIZE, file_sha256, split_file
from protocol import T
from webrtc_transfer import WebRTCTransfer


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
                              peer_factory=peer_factory)

    session_id = _run(transfer.start_offer("bob", path, session_id="s1"))

    assert session_id == "s1"
    assert peers[0].channels[0].label == "file"
    assert sent == [(
        T.WEBRTC_OFFER,
        {
            "to": "bob",
            "session_id": "s1",
            "sdp": {"type": "offer", "sdp": "offer-sdp"},
            "filename": "file.bin",
            "size": 3,
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
                              peer_factory=peer_factory)

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
                              peer_factory=peer_factory)
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
                              peer_factory=FakePeerConnection)

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
                              peer_factory=peer_factory)
    _run(transfer.start_offer("bob", path, session_id="send1"))

    _run(transfer.send_file("send1"))

    frames = [json.loads(raw) for raw in peers[0].channels[0].sent]
    assert frames[0] == {
        "kind": "file-start",
        "transfer_id": "send1",
        "filename": "payload.bin",
        "size": len(data),
        "mime": "application/octet-stream",
    }
    assert frames[1]["kind"] == "file-chunk"
    assert frames[1]["index"] == 0
    assert frames[1]["total"] == 2
    assert frames[1]["data"] == split_file(data)[0]
    assert frames[2]["kind"] == "file-chunk"
    assert frames[2]["index"] == 1
    assert frames[3] == {
        "kind": "file-done",
        "transfer_id": "send1",
        "sha256": file_sha256(data),
    }


def test_handle_data_message_reassembles_file(tmp_path):
    data = b"hello over datachannel"
    chunks = split_file(data)
    transfer = WebRTCTransfer(lambda *_args, **_kwargs: None,
                              peer_factory=FakePeerConnection,
                              downloads_dir=tmp_path)

    assert transfer.handle_data_message("alice", json.dumps({
        "kind": "file-start",
        "transfer_id": "recv1",
        "filename": "../safe.txt",
        "size": len(data),
        "mime": "text/plain",
    })) is None
    assert transfer.handle_data_message("alice", json.dumps({
        "kind": "file-chunk",
        "transfer_id": "recv1",
        "index": 0,
        "total": len(chunks),
        "data": chunks[0],
    })) is None
    save_path = transfer.handle_data_message("alice", json.dumps({
        "kind": "file-done",
        "transfer_id": "recv1",
        "sha256": file_sha256(data),
    }))

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
                              peer_factory=peer_factory)
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
    chunks = split_file(data)

    def peer_factory():
        peer = FakePeerConnection()
        peers.append(peer)
        return peer

    transfer = WebRTCTransfer(lambda msg_type, **payload: sent.append((msg_type, payload)),
                              peer_factory=peer_factory,
                              downloads_dir=tmp_path)
    _run(transfer.handle_offer({
        "from": "alice",
        "session_id": "dc1",
        "sdp": {"type": "offer", "sdp": "offer-sdp"},
    }))
    channel = FakeDataChannel("file")
    peers[0].handlers["datachannel"](channel)

    assert channel.handlers["message"](json.dumps({
        "kind": "file-start",
        "transfer_id": "dc1",
        "filename": "remote.bin",
        "size": len(data),
        "mime": "application/octet-stream",
    })) is None
    assert channel.handlers["message"](json.dumps({
        "kind": "file-chunk",
        "transfer_id": "dc1",
        "index": 0,
        "total": len(chunks),
        "data": chunks[0],
    })) is None
    save_path = channel.handlers["message"](json.dumps({
        "kind": "file-done",
        "transfer_id": "dc1",
        "sha256": file_sha256(data),
    }))

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
                              peer_factory=peer_factory)
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
                              on_channel_open=lambda meta: opened.append(meta))
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
                              ice_servers=[{"urls": ["turn:turn.example.com"], "username": "u", "credential": "p"}])
    peer = transfer._default_peer_factory()

    assert isinstance(peer, FakeRTCPeerConnection)
    assert created[0].iceServers[0].urls == ["turn:turn.example.com"]
    assert created[0].iceServers[0].username == "u"
    assert created[0].iceServers[0].credential == "p"


def test_receive_complete_callback_gets_saved_path(tmp_path):
    completed = []
    data = b"callback-data"
    chunks = split_file(data)
    transfer = WebRTCTransfer(lambda *_args, **_kwargs: None,
                              peer_factory=FakePeerConnection,
                              downloads_dir=tmp_path,
                              on_file_received=lambda path, meta: completed.append((path, meta)))

    save_path = None
    for message in [
        {
            "kind": "file-start",
            "transfer_id": "cb1",
            "filename": "callback.bin",
            "size": len(data),
            "mime": "application/octet-stream",
        },
        {
            "kind": "file-chunk",
            "transfer_id": "cb1",
            "index": 0,
            "total": len(chunks),
            "data": chunks[0],
        },
        {
            "kind": "file-done",
            "transfer_id": "cb1",
            "sha256": file_sha256(data),
        },
    ]:
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
                              on_file_sent=lambda path, meta: completed.append((path, meta)))
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
                              on_file_progress=lambda meta: progress.append(meta))
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
    chunks = split_file(data)
    transfer = WebRTCTransfer(lambda *_args, **_kwargs: None,
                              peer_factory=FakePeerConnection,
                              downloads_dir=tmp_path,
                              on_file_progress=lambda meta: progress.append(meta))

    transfer.handle_data_message("alice", json.dumps({
        "kind": "file-start",
        "transfer_id": "recv-progress",
        "filename": "recv-progress.bin",
        "size": len(data),
        "mime": "application/octet-stream",
    }))
    for index, chunk in enumerate(chunks):
        transfer.handle_data_message("alice", json.dumps({
            "kind": "file-chunk",
            "transfer_id": "recv-progress",
            "index": index,
            "total": len(chunks),
            "data": chunk,
        }))

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
                              on_session_closed=lambda meta: closed.append(meta))
    _run(transfer.start_offer("bob", path, session_id="close1"))

    peers[0].channels[0].handlers["close"]()

    assert closed == [{
        "peer": "bob",
        "session_id": "close1",
        "message": "DataChannel closed",
    }]


def test_webrtc_transfer_logs_key_session_events(tmp_path, caplog):
    peers = []
    path = tmp_path / "logged.bin"
    path.write_bytes(b"abc")

    def peer_factory():
        peer = FakePeerConnection()
        peers.append(peer)
        return peer

    transfer = WebRTCTransfer(lambda *_args, **_kwargs: None,
                              peer_factory=peer_factory)

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
