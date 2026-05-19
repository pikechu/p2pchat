"""File chunking, reassembly, and transfer state management."""

import base64
import hashlib
import pathlib
import uuid
from typing import Dict, List, Optional

CHUNK_SIZE = 65536   # 64 KB


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
            "mime":     _guess_mime(filename),
            "size":     len(data),
        }
        return tid

    def begin_incoming(self, transfer_id: str, from_user: str,
                       filename: str, size: int, mime: str):
        # Sanitize filename to basename only, preventing path traversal
        filename = pathlib.Path(filename).name or "file"
        total_chunks = max(1, (size + CHUNK_SIZE - 1) // CHUNK_SIZE)
        self.incoming[transfer_id] = {
            "from":     from_user,
            "filename": filename,
            "size":     size,
            "mime":     mime,
            "received": [None] * total_chunks,
        }

    def add_chunk(self, transfer_id: str, index: int, total: int, data_b64: str):
        rec = self.incoming.get(transfer_id)
        if rec is None:
            return
        # Grow list if needed (guard against off-by-one in total)
        while len(rec["received"]) <= index:
            rec["received"].append(None)
        rec["received"][index] = data_b64

    def finish_incoming(self, transfer_id: str, sha256_hex: str) -> Optional[pathlib.Path]:
        rec = self.incoming.pop(transfer_id, None)
        if rec is None:
            return None
        data = reassemble_chunks([c for c in rec["received"] if c is not None])
        if file_sha256(data) != sha256_hex:
            return None
        out = self._dir / rec["filename"]
        # Avoid clobbering existing files
        stem, suffix = out.stem, out.suffix
        counter = 1
        while out.exists():
            out = self._dir / f"{stem}_{counter}{suffix}"
            counter += 1
        out.write_bytes(data)
        return out

    def cancel(self, transfer_id: str):
        self.outgoing.pop(transfer_id, None)
        self.incoming.pop(transfer_id, None)


def _guess_mime(filename: str) -> str:
    ext = pathlib.Path(filename).suffix.lower()
    return {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp",
        ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
        ".pdf": "application/pdf", ".zip": "application/zip",
    }.get(ext, "application/octet-stream")
