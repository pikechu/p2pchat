"""File chunking, reassembly, and transfer state management."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import pathlib
import uuid
import binascii
from dataclasses import dataclass
from typing import Dict, List, Optional

from e2e_crypto import CryptoError, decrypt_envelope, encrypt_envelope

CHUNK_SIZE = 32768   # 32 KB — halved to reduce frame size and ease ping/pong timing


class FileCryptoError(Exception):
    """表示文件密文格式、认证或顺序校验失败。"""


@dataclass(frozen=True)
class EncryptedFileMetadata:
    filename: str
    size: int
    mime: str
    sha256: str
    total: int


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


def encrypted_file_context(
    *,
    transfer_id: str,
    scope_type: str,
    scope_id: str,
    sender: str,
    recipient: str = "",
    purpose: str,
    index: int | None = None,
    total: int | None = None,
    size: int | None = None,
) -> dict:
    context = {
        "transfer_id": str(transfer_id),
        "scope_type": str(scope_type),
        "scope_id": str(scope_id),
        "sender": str(sender),
        "recipient": str(recipient),
        "purpose": str(purpose),
    }
    if index is not None:
        context["index"] = int(index)
    if total is not None:
        context["total"] = int(total)
    if size is not None:
        context["size"] = int(size)
    return context


def encrypted_ciphertext_size(envelope: dict) -> int:
    try:
        return len(base64.b64decode(envelope["ciphertext"], validate=True))
    except (KeyError, TypeError, ValueError, binascii.Error) as exc:
        raise FileCryptoError("文件密文格式无效") from exc


class EncryptedFileSender:
    def __init__(
        self,
        path: pathlib.Path,
        file_key: bytes,
        *,
        transfer_id: str,
        scope_type: str,
        scope_id: str,
        sender: str,
        recipient: str = "",
        wait_for_ack: bool = False,
    ):
        self.path = path
        self.file_key = file_key
        self.transfer_id = str(transfer_id)
        self.scope_type = str(scope_type)
        self.scope_id = str(scope_id)
        self.sender = str(sender)
        self.recipient = str(recipient)
        self.wait_for_ack = bool(wait_for_ack)
        self.size = path.stat().st_size
        self.total_chunks = max(1, math.ceil(self.size / CHUNK_SIZE))
        self.ciphertext_size = self.size + self.total_chunks * 16
        self._src = path.open("rb")
        self._hasher = hashlib.sha256()
        self._sent_chunks = 0
        self._acked_chunks = 0
        self._in_flight: set[int] = set()
        self._done_reading = False
        self._zero_chunk_pending = self.size == 0
        self._metadata_envelope: dict | None = None

    @property
    def sent_chunks(self) -> int:
        return self._sent_chunks

    @property
    def sha256_hex(self) -> str:
        return self._hasher.hexdigest()

    @property
    def acked_chunks(self) -> int:
        return self._acked_chunks

    def offer_payload(self) -> dict:
        if self._metadata_envelope is None:
            digest = _file_digest(self.path)
            metadata = {
                "filename": self.path.name,
                "size": self.size,
                "mime": guess_mime(self.path.name),
                "sha256": digest,
                "total": self.total_chunks,
                "chunk_size": CHUNK_SIZE,
            }
            self._metadata_envelope = encrypt_envelope(
                self.file_key,
                json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"),
                self._context("metadata", total=self.total_chunks, size=self.size),
            )
        return {
            "encrypted_metadata": self._metadata_envelope,
            "size": self.size,
            "total": self.total_chunks,
            "ciphertext_size": self.ciphertext_size,
        }

    def next_payload(self) -> Optional[dict]:
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
        envelope = encrypt_envelope(
            self.file_key,
            chunk,
            self._context("chunk", index=index, total=self.total_chunks, size=self.size),
        )
        self._sent_chunks += 1
        self._in_flight.add(index)
        if self._sent_chunks >= self.total_chunks:
            self._done_reading = True
            self._src.close()
        return {
            "index": index,
            "total": self.total_chunks,
            "encrypted_chunk": envelope,
            "ciphertext_size": encrypted_ciphertext_size(envelope),
        }

    def done_payload(self) -> dict:
        if not self.ready_to_finish():
            raise FileCryptoError("文件尚未完成读取")
        plaintext = json.dumps(
            {
                "sha256": self.sha256_hex,
                "size": self.size,
                "total": self.total_chunks,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return {
            "encrypted_done": encrypt_envelope(
                self.file_key,
                plaintext,
                self._context("done", total=self.total_chunks, size=self.size),
            )
        }

    def ready_to_finish(self) -> bool:
        if self.wait_for_ack and self._in_flight:
            return False
        return self._done_reading and self._sent_chunks == self.total_chunks

    def acknowledge(self, index: int):
        if index in self._in_flight:
            self._in_flight.remove(index)
            self._acked_chunks += 1

    def next_payloads(self) -> List[dict]:
        if self.wait_for_ack and self._in_flight:
            return []
        payload = self.next_payload()
        return [] if payload is None else [payload]

    def _context(self, purpose: str, **kwargs) -> dict:
        return encrypted_file_context(
            transfer_id=self.transfer_id,
            scope_type=self.scope_type,
            scope_id=self.scope_id,
            sender=self.sender,
            recipient=self.recipient,
            purpose=purpose,
            **kwargs,
        )


class EncryptedFileReceiver:
    def __init__(
        self,
        downloads_dir: pathlib.Path,
        file_key: bytes,
        *,
        transfer_id: str,
        scope_type: str,
        scope_id: str,
        sender: str,
        recipient: str = "",
    ):
        self._dir = downloads_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self.file_key = file_key
        self.transfer_id = str(transfer_id)
        self.scope_type = str(scope_type)
        self.scope_id = str(scope_id)
        self.sender = str(sender)
        self.recipient = str(recipient)
        self.metadata: EncryptedFileMetadata | None = None
        self.temp_path = self._dir / f".{self.transfer_id}.part"
        self.received_chunks = 0
        self._hasher = hashlib.sha256()

    def begin(self, encrypted_metadata: dict, declared_size: int, declared_total: int) -> dict:
        try:
            plaintext = decrypt_envelope(
                self.file_key,
                encrypted_metadata,
                self._context("metadata", total=int(declared_total), size=int(declared_size)),
            )
            raw = json.loads(plaintext.decode("utf-8"))
            metadata = EncryptedFileMetadata(
                filename=pathlib.Path(str(raw["filename"])).name or "file",
                size=int(raw["size"]),
                mime=str(raw.get("mime", "application/octet-stream")),
                sha256=str(raw["sha256"]),
                total=int(raw["total"]),
            )
            if metadata.size != int(declared_size) or metadata.total != int(declared_total):
                raise ValueError
            if metadata.total != max(1, math.ceil(metadata.size / CHUNK_SIZE)):
                raise ValueError
        except (CryptoError, KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
            self.cancel()
            raise FileCryptoError("文件元数据认证失败") from exc
        self.metadata = metadata
        self.received_chunks = 0
        self._hasher = hashlib.sha256()
        self.temp_path.unlink(missing_ok=True)
        return {
            "filename": metadata.filename,
            "size": metadata.size,
            "mime": metadata.mime,
            "sha256": metadata.sha256,
            "total": metadata.total,
        }

    def add_chunk(self, index: int, total: int, encrypted_chunk: dict) -> bool:
        if self.metadata is None:
            raise FileCryptoError("文件元数据尚未认证")
        try:
            index = int(index)
            total = int(total)
            if total != self.metadata.total or index != self.received_chunks or index < 0 or index >= total:
                raise ValueError
            chunk = decrypt_envelope(
                self.file_key,
                encrypted_chunk,
                self._context("chunk", index=index, total=total, size=self.metadata.size),
            )
            is_last = index == total - 1
            if (not is_last and len(chunk) != CHUNK_SIZE) or len(chunk) > CHUNK_SIZE:
                raise ValueError
            next_size = self.temp_path.stat().st_size + len(chunk) if self.temp_path.exists() else len(chunk)
            if next_size > self.metadata.size:
                raise ValueError
            if is_last and next_size != self.metadata.size:
                raise ValueError
        except (CryptoError, TypeError, ValueError, OSError) as exc:
            self.cancel()
            raise FileCryptoError("文件分块认证失败") from exc
        with self.temp_path.open("ab") as out:
            out.write(chunk)
        self._hasher.update(chunk)
        self.received_chunks += 1
        return True

    def finish(self, encrypted_done: dict) -> pathlib.Path:
        if self.metadata is None:
            raise FileCryptoError("文件元数据尚未认证")
        try:
            plaintext = decrypt_envelope(
                self.file_key,
                encrypted_done,
                self._context("done", total=self.metadata.total, size=self.metadata.size),
            )
            done = json.loads(plaintext.decode("utf-8"))
            if int(done["size"]) != self.metadata.size or int(done["total"]) != self.metadata.total:
                raise ValueError
            sha256_hex = str(done["sha256"])
            if self.received_chunks != self.metadata.total:
                raise ValueError
            if sha256_hex != self.metadata.sha256 or self._hasher.hexdigest() != sha256_hex:
                raise ValueError
        except (CryptoError, KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
            self.cancel()
            raise FileCryptoError("文件完成帧认证失败") from exc
        out = self._dir / self.metadata.filename
        stem, suffix = out.stem, out.suffix
        counter = 1
        while out.exists():
            out = self._dir / f"{stem}_{counter}{suffix}"
            counter += 1
        self.temp_path.replace(out)
        self.metadata = None
        return out

    def cancel(self):
        self.temp_path.unlink(missing_ok=True)
        self.metadata = None

    def _context(self, purpose: str, **kwargs) -> dict:
        return encrypted_file_context(
            transfer_id=self.transfer_id,
            scope_type=self.scope_type,
            scope_id=self.scope_id,
            sender=self.sender,
            recipient=self.recipient,
            purpose=purpose,
            **kwargs,
        )


def _file_digest(path: pathlib.Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as src:
        for chunk in iter(lambda: src.read(CHUNK_SIZE), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


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
