"""File chunking, reassembly, and transfer state management."""

import base64
import hashlib
import math
import pathlib
import uuid
from typing import Dict, List, Optional

CHUNK_SIZE = 32768   # 32 KB — halved to reduce frame size and ease ping/pong timing


def split_file(data: bytes) -> List[str]:
    """Split bytes into base64-encoded chunks of CHUNK_SIZE."""
    chunks = []
    for i in range(0, max(len(data), 1), CHUNK_SIZE):
        chunks.append(base64.b64encode(data[i:i + CHUNK_SIZE]).decode())
    return chunks


def reassemble_chunks(chunks_b64: List[str]) -> bytes:
    """Decode and concatenate base64 chunks back to original bytes."""
    return b"".join(base64.b64decode(c) for c in chunks_b64)


def file_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class RoomFileSender:
    def __init__(self, path: pathlib.Path, max_in_flight: int = 16):
        self.path = path
        self.max_in_flight = max_in_flight
        self.size = path.stat().st_size
        self.total_chunks = max(1, math.ceil(self.size / CHUNK_SIZE))
        self._src = path.open("rb")
        self._hasher = hashlib.sha256()
        self._sent_chunks = 0
        self._acked_chunks = 0
        self._in_flight: set[int] = set()
        self._done_reading = False
        self._zero_chunk_pending = self.size == 0

    @property
    def sent_chunks(self) -> int:
        return self._sent_chunks

    @property
    def acked_chunks(self) -> int:
        return self._acked_chunks

    @property
    def sha256_hex(self) -> str:
        return self._hasher.hexdigest()

    def next_payloads(self) -> List[tuple[int, int, str]]:
        payloads = []
        while not self._done_reading and len(self._in_flight) < self.max_in_flight:
            if self._zero_chunk_pending:
                chunk = b""
                self._zero_chunk_pending = False
            else:
                chunk = self._src.read(CHUNK_SIZE)
            if not chunk and self.size > 0:
                self._done_reading = True
                self._src.close()
                break
            index = self._sent_chunks
            self._hasher.update(chunk)
            payloads.append((
                index,
                self.total_chunks,
                base64.b64encode(chunk).decode("ascii"),
            ))
            self._sent_chunks += 1
            self._in_flight.add(index)
            if self._sent_chunks >= self.total_chunks:
                self._done_reading = True
                self._src.close()
        return payloads

    def acknowledge(self, index: int):
        if index in self._in_flight:
            self._in_flight.remove(index)
            self._acked_chunks += 1

    def ready_to_finish(self) -> bool:
        return self._done_reading and not self._in_flight and self._sent_chunks == self.total_chunks


class DirectFileSender:
    def __init__(self, path: pathlib.Path):
        self.path = path
        self.size = path.stat().st_size
        self.total_chunks = max(1, math.ceil(self.size / CHUNK_SIZE))
        self._src = path.open("rb")
        self._hasher = hashlib.sha256()
        self._sent_chunks = 0
        self._done_reading = False
        self._zero_chunk_pending = self.size == 0

    @property
    def sent_chunks(self) -> int:
        return self._sent_chunks

    @property
    def sha256_hex(self) -> str:
        return self._hasher.hexdigest()

    def next_payload(self) -> Optional[tuple[int, int, str]]:
        if self._done_reading:
            return None
        if self._zero_chunk_pending:
            chunk = b""
            self._zero_chunk_pending = False
        else:
            chunk = self._src.read(CHUNK_SIZE)
        if not chunk and self.size > 0:
            self._done_reading = True
            self._src.close()
            return None
        index = self._sent_chunks
        self._hasher.update(chunk)
        self._sent_chunks += 1
        if self._sent_chunks >= self.total_chunks:
            self._done_reading = True
            self._src.close()
        return index, self.total_chunks, base64.b64encode(chunk).decode("ascii")

    def ready_to_finish(self) -> bool:
        return self._done_reading and self._sent_chunks == self.total_chunks


class FileTransferManager:
    def __init__(self, downloads_dir: pathlib.Path):
        self._dir = downloads_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        # transfer_id → {to, filename, chunks, size, mime}
        self.outgoing: Dict[str, dict] = {}
        # transfer_id → {from, filename, size, mime, received: List[str|None]}
        self.incoming: Dict[str, dict] = {}

    def register_outgoing(self, to: str, filename: str, data: bytes) -> str:
        tid = uuid.uuid4().hex[:12]
        self.outgoing[tid] = {
            "to":       to,
            "filename": filename,
            "data":     data,
            "chunks":   split_file(data),
            "mime":     guess_mime(filename),
            "size":     len(data),
        }
        return tid

    def register_outgoing_path(self, to: str, path: pathlib.Path) -> str:
        tid = uuid.uuid4().hex[:12]
        self.outgoing[tid] = {
            "to":       to,
            "filename": path.name,
            "path":     path,
            "sender":   DirectFileSender(path),
            "mime":     guess_mime(path.name),
            "size":     path.stat().st_size,
        }
        return tid

    def begin_incoming(self, transfer_id: str, from_user: str,
                       filename: str, size: int, mime: str):
        # Sanitize filename to basename only, preventing path traversal
        filename = pathlib.Path(filename).name or "file"
        total_chunks = max(1, (size + CHUNK_SIZE - 1) // CHUNK_SIZE)
        temp_path = self._dir / f".{transfer_id}.part"
        self.incoming[transfer_id] = {
            "from":     from_user,
            "filename": filename,
            "size":     size,
            "mime":     mime,
            "total_chunks": total_chunks,
            "received_chunks": 0,
            "temp_path": temp_path,
            "hasher": hashlib.sha256(),
        }
        temp_path.unlink(missing_ok=True)

    def add_chunk(self, transfer_id: str, index: int, total: int, data_b64: str) -> bool:
        rec = self.incoming.get(transfer_id)
        if rec is None:
            return False
        if total != rec["total_chunks"]:
            return False
        if index < 0 or index >= total or index != rec["received_chunks"]:
            return False
        try:
            chunk = base64.b64decode(data_b64)
        except Exception:
            return False
        with rec["temp_path"].open("ab") as out:
            out.write(chunk)
        rec["hasher"].update(chunk)
        rec["received_chunks"] += 1
        return True

    def finish_incoming(self, transfer_id: str, sha256_hex: str) -> Optional[pathlib.Path]:
        rec = self.incoming.pop(transfer_id, None)
        if rec is None:
            return None
        temp_path = rec["temp_path"]
        if rec["received_chunks"] != rec["total_chunks"]:
            temp_path.unlink(missing_ok=True)
            return None
        if rec["hasher"].hexdigest() != sha256_hex:
            temp_path.unlink(missing_ok=True)
            return None
        out = self._dir / rec["filename"]
        # Avoid clobbering existing files
        stem, suffix = out.stem, out.suffix
        counter = 1
        while out.exists():
            out = self._dir / f"{stem}_{counter}{suffix}"
            counter += 1
        temp_path.replace(out)
        return out

    def cancel(self, transfer_id: str):
        self.outgoing.pop(transfer_id, None)
        if rec := self.incoming.pop(transfer_id, None):
            rec["temp_path"].unlink(missing_ok=True)

    @staticmethod
    def iter_file_chunks(path: pathlib.Path):
        size = path.stat().st_size
        total = max(1, math.ceil(size / CHUNK_SIZE))
        with path.open("rb") as src:
            for index in range(total):
                chunk = src.read(CHUNK_SIZE)
                yield index, total, base64.b64encode(chunk).decode("ascii")


def guess_mime(filename: str) -> str:
    ext = pathlib.Path(filename).suffix.lower()
    return {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp",
        ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
        ".pdf": "application/pdf", ".zip": "application/zip",
    }.get(ext, "application/octet-stream")
