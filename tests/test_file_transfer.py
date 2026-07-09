import sys, os, hashlib, tempfile, pathlib
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import base64
import pytest
from file_transfer import (
    CHUNK_SIZE, split_file, reassemble_chunks,
    DirectFileSender, file_sha256, FileTransferManager, RoomFileSender,
)


# ── chunk utilities ──────────────────────────────────────────────────────────

def test_split_file_returns_base64_chunks():
    data = b"A" * (CHUNK_SIZE + 1)
    chunks = split_file(data)
    assert len(chunks) == 2
    # each chunk is a base64 string
    base64.b64decode(chunks[0])   # must not raise


def test_split_file_single_chunk_for_small_data():
    data = b"hello"
    chunks = split_file(data)
    assert len(chunks) == 1
    assert base64.b64decode(chunks[0]) == b"hello"


def test_reassemble_chunks_round_trips():
    data = bytes(range(256)) * 300   # 76 800 bytes, crosses chunk boundary
    chunks = split_file(data)
    result = reassemble_chunks(chunks)
    assert result == data


def test_file_sha256():
    data = b"integrity check"
    digest = file_sha256(data)
    expected = hashlib.sha256(data).hexdigest()
    assert digest == expected


# ── FileTransferManager ───────────────────────────────────────────────────────

def test_manager_register_outgoing():
    mgr = FileTransferManager(downloads_dir=pathlib.Path(tempfile.mkdtemp()))
    tid = mgr.register_outgoing("bob", "photo.jpg", b"JPEG_DATA")
    assert tid in mgr.outgoing
    assert mgr.outgoing[tid]["to"] == "bob"
    assert mgr.outgoing[tid]["filename"] == "photo.jpg"
    assert len(mgr.outgoing[tid]["chunks"]) >= 1


def test_manager_begin_incoming():
    mgr = FileTransferManager(downloads_dir=pathlib.Path(tempfile.mkdtemp()))
    mgr.begin_incoming("t1", "alice", "file.txt", 10, "text/plain")
    assert "t1" in mgr.incoming
    assert mgr.incoming["t1"]["from"] == "alice"


def test_manager_add_chunk_and_finish():
    data = b"chunk content here"
    chunks = split_file(data)
    mgr = FileTransferManager(downloads_dir=pathlib.Path(tempfile.mkdtemp()))
    mgr.begin_incoming("t2", "alice", "out.bin", len(data), "application/octet-stream")
    for i, chunk_b64 in enumerate(chunks):
        mgr.add_chunk("t2", i, len(chunks), chunk_b64)

    sha = file_sha256(data)
    path = mgr.finish_incoming("t2", sha)
    assert path is not None
    assert path.read_bytes() == data


def test_manager_finish_incoming_rejects_bad_sha():
    data = b"tampered"
    chunks = split_file(data)
    mgr = FileTransferManager(downloads_dir=pathlib.Path(tempfile.mkdtemp()))
    mgr.begin_incoming("t3", "eve", "bad.bin", len(data), "application/octet-stream")
    for i, c in enumerate(chunks):
        mgr.add_chunk("t3", i, len(chunks), c)

    path = mgr.finish_incoming("t3", "deadbeef" * 8)
    assert path is None   # checksum mismatch


def test_manager_finish_incoming_removes_temp_file_on_bad_sha():
    data = b"tampered"
    chunks = split_file(data)
    tmpdir = pathlib.Path(tempfile.mkdtemp())
    mgr = FileTransferManager(downloads_dir=tmpdir)
    mgr.begin_incoming("t_bad_sha", "eve", "bad.bin", len(data), "application/octet-stream")
    temp_path = mgr.incoming["t_bad_sha"]["temp_path"]
    for i, c in enumerate(chunks):
        mgr.add_chunk("t_bad_sha", i, len(chunks), c)

    path = mgr.finish_incoming("t_bad_sha", "deadbeef" * 8)
    assert path is None
    assert temp_path.exists() is False


def test_begin_incoming_sanitizes_traversal_filename():
    tmpdir = pathlib.Path(tempfile.mkdtemp())
    mgr = FileTransferManager(downloads_dir=tmpdir)
    # Adversarial filename with path traversal
    mgr.begin_incoming("t_evil", "eve", "../../../evil.txt", 5, "text/plain")
    # Stored filename must not contain path separators
    stored = mgr.incoming["t_evil"]["filename"]
    assert ".." not in stored
    assert "/" not in stored
    assert "\\" not in stored


def test_add_chunk_ignores_out_of_range_index():
    mgr = FileTransferManager(downloads_dir=pathlib.Path(tempfile.mkdtemp()))
    mgr.begin_incoming("t4", "alice", "file.bin", 4, "application/octet-stream")

    before_chunks = mgr.incoming["t4"]["received_chunks"]
    temp_path = mgr.incoming["t4"]["temp_path"]
    accepted = mgr.add_chunk("t4", 999, 1, "AAAA")

    assert accepted is False
    assert mgr.incoming["t4"]["received_chunks"] == before_chunks
    assert temp_path.exists() is False


def test_add_chunk_ignores_mismatched_total():
    mgr = FileTransferManager(downloads_dir=pathlib.Path(tempfile.mkdtemp()))
    mgr.begin_incoming("t5", "alice", "file.bin", CHUNK_SIZE + 1, "application/octet-stream")

    before_chunks = mgr.incoming["t5"]["received_chunks"]
    temp_path = mgr.incoming["t5"]["temp_path"]
    accepted = mgr.add_chunk("t5", 0, 999, split_file(b"abc")[0])

    assert accepted is False
    assert mgr.incoming["t5"]["received_chunks"] == before_chunks
    assert temp_path.exists() is False


def test_add_chunk_ignores_out_of_order_chunk():
    data = b"A" * (CHUNK_SIZE + 10)
    chunks = split_file(data)
    mgr = FileTransferManager(downloads_dir=pathlib.Path(tempfile.mkdtemp()))
    mgr.begin_incoming("t6", "alice", "file.bin", len(data), "application/octet-stream")

    accepted = mgr.add_chunk("t6", 1, len(chunks), chunks[1])
    assert accepted is False
    assert mgr.incoming["t6"]["received_chunks"] == 0

    accepted = mgr.add_chunk("t6", 0, len(chunks), chunks[0])
    assert accepted is True
    assert mgr.incoming["t6"]["received_chunks"] == 1


def test_add_chunk_returns_true_when_chunk_is_written():
    mgr = FileTransferManager(downloads_dir=pathlib.Path(tempfile.mkdtemp()))
    mgr.begin_incoming("t_written", "alice", "file.bin", 3, "application/octet-stream")

    accepted = mgr.add_chunk("t_written", 0, 1, split_file(b"abc")[0])

    assert accepted is True
    assert mgr.incoming["t_written"]["received_chunks"] == 1


def test_iter_file_chunks_streams_without_loading_whole_file():
    tmpdir = pathlib.Path(tempfile.mkdtemp())
    path = tmpdir / "stream.bin"
    data = bytes(range(256)) * 300
    path.write_bytes(data)

    chunks = list(FileTransferManager.iter_file_chunks(path))
    total = len(chunks)

    assert total >= 2
    assert chunks[0][0] == 0
    assert chunks[-1][0] == total - 1
    assert all(chunk_total == total for _, chunk_total, _ in chunks)
    rebuilt = b"".join(base64.b64decode(payload) for _, _, payload in chunks)
    assert rebuilt == data


def test_room_file_sender_limits_in_flight_and_tracks_ack_progress():
    tmpdir = pathlib.Path(tempfile.mkdtemp())
    path = tmpdir / "send.bin"
    data = b"A" * (CHUNK_SIZE * 3 + 10)
    path.write_bytes(data)

    sender = RoomFileSender(path, max_in_flight=2)
    batch1 = sender.next_payloads()
    assert [index for index, _, _ in batch1] == [0, 1]
    assert sender.acked_chunks == 0
    assert sender.sent_chunks == 2

    assert sender.next_payloads() == []

    sender.acknowledge(0)
    batch2 = sender.next_payloads()
    assert [index for index, _, _ in batch2] == [2]

    sender.acknowledge(1)
    sender.acknowledge(2)
    batch3 = sender.next_payloads()
    assert [index for index, _, _ in batch3] == [3]

    sender.acknowledge(3)
    assert sender.ready_to_finish() is True
    assert sender.sha256_hex == hashlib.sha256(data).hexdigest()


def test_room_file_sender_handles_zero_byte_file():
    tmpdir = pathlib.Path(tempfile.mkdtemp())
    path = tmpdir / "empty.bin"
    path.write_bytes(b"")

    sender = RoomFileSender(path, max_in_flight=2)
    batch = sender.next_payloads()

    assert batch == [(0, 1, "")]
    sender.acknowledge(0)
    assert sender.ready_to_finish() is True
    assert sender.sha256_hex == hashlib.sha256(b"").hexdigest()


def test_direct_file_sender_streams_incrementally():
    tmpdir = pathlib.Path(tempfile.mkdtemp())
    path = tmpdir / "direct.bin"
    data = bytes(range(256)) * 300
    path.write_bytes(data)

    sender = DirectFileSender(path)
    payloads = []
    while payload := sender.next_payload():
        payloads.append(payload)

    assert len(payloads) >= 2
    assert payloads[0][0] == 0
    assert payloads[-1][0] == len(payloads) - 1
    rebuilt = b"".join(base64.b64decode(chunk) for _, _, chunk in payloads)
    assert rebuilt == data
    assert sender.ready_to_finish() is True
    assert sender.sha256_hex == hashlib.sha256(data).hexdigest()
