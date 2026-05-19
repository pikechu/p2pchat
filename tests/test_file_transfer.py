import sys, os, hashlib, tempfile, pathlib
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import base64
import pytest
from file_transfer import (
    CHUNK_SIZE, split_file, reassemble_chunks,
    file_sha256, FileTransferManager,
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
