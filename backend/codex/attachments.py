"""Workspace-local attachments for Codex-native turns.

Uploads are staged on disk before an SSE connection is opened, then claimed by
exactly one Codex thread.  Keeping the bytes under the workspace gives
``codex app-server`` stable local paths and avoids the legacy process-global
base64 store.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import HTTPException, UploadFile


_MAX_BYTES = 10 * 1024 * 1024
_MAX_TEXT_BYTES = 200 * 1024
_MAX_ATTACHMENTS = 8
_ID_RE = re.compile(r"[0-9a-f]{32}")
_THREAD_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_IMAGE_EXTENSIONS = {
    "png": ("image/png", ".png"),
    "jpeg": ("image/jpeg", ".jpg"),
    "gif": ("image/gif", ".gif"),
    "webp": ("image/webp", ".webp"),
}
_TEXT_EXTENSIONS = {
    ".md", ".markdown", ".txt", ".csv", ".json", ".yaml", ".yml",
    ".toml", ".py", ".sh", ".js", ".ts", ".tsx", ".jsx", ".html",
    ".css", ".xml", ".log", ".ini", ".conf", ".cfg", ".rs", ".go",
    ".java", ".c", ".h", ".cpp", ".hpp", ".rb", ".php", ".swift",
    ".kt", ".sql",
}
_SHEET_EXTENSIONS = {".xlsx", ".xlsm", ".xltx", ".xltm"}


@dataclass(frozen=True)
class PreparedAttachments:
    inputs: list[dict[str, Any]]
    images: list[dict[str, Any]]
    docs: list[dict[str, Any]]
    client_user_message_id: str | None = None


class CodexAttachmentService:
    """Stage uploads and expose only thread-owned files to app-server."""

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).resolve()
        self.base = self.workspace / ".muselab-codex" / "attachments"
        self.base.mkdir(parents=True, exist_ok=True)
        try:
            self.base = self.base.resolve()
            self.base.relative_to(self.workspace)
        except (OSError, ValueError):
            raise ValueError("attachment directory must stay inside the workspace") from None
        children = []
        for name in ("staged", "threads"):
            raw = self.base / name
            raw.mkdir(parents=True, exist_ok=True)
            resolved = raw.resolve()
            try:
                resolved.relative_to(self.base)
            except ValueError:
                raise ValueError(
                    "attachment subdirectories must stay inside the workspace") from None
            children.append(resolved)
        self.staged, self.threads = children

    async def upload(self, file: UploadFile) -> dict[str, Any]:
        name = _safe_name(file.filename or "upload")
        body = await file.read(_MAX_BYTES + 1)
        if len(body) > _MAX_BYTES:
            raise HTTPException(413, f"file too large: max {_MAX_BYTES} bytes")
        kind, mime, extension = _classify(body, file.content_type or "", name)
        if kind == "text" and extension in _TEXT_EXTENSIONS:
            if len(body) > _MAX_TEXT_BYTES:
                raise HTTPException(
                    413,
                    f"text file too large: max {_MAX_TEXT_BYTES} bytes",
                )
            try:
                body.decode("utf-8")
            except UnicodeDecodeError:
                raise HTTPException(400, "text file is not valid UTF-8") from None

        attachment_id = secrets.token_hex(16)
        filename = f"{attachment_id}{extension}"
        data_path = self.staged / filename
        metadata = {
            "id": attachment_id,
            "name": name,
            "mime": mime,
            "kind": kind,
            "filename": filename,
            "bytes": len(body),
            "created_at": time.time(),
        }
        metadata_body = json.dumps(
            metadata, ensure_ascii=False, separators=(",", ":")).encode()
        await asyncio.to_thread(
            self._write_staged, data_path, body, attachment_id, metadata_body)
        return {
            "id": attachment_id,
            "mime": mime,
            "bytes": len(body),
            "kind": kind,
            "name": name,
            "attach_ext": extension.removeprefix(".") if kind == "image" else "",
        }

    def prepare(self, thread_id: str, attachment_ids: str) -> PreparedAttachments:
        clean_thread = _thread_id(thread_id)
        ids = _attachment_ids(attachment_ids)
        inputs: list[dict[str, Any]] = []
        images: list[dict[str, Any]] = []
        docs: list[dict[str, Any]] = []
        for attachment_id in ids:
            metadata, path = self._claim(clean_thread, attachment_id)
            if metadata["kind"] == "image":
                inputs.append({"type": "localImage", "path": str(path)})
                images.append(self._image_ui(clean_thread, metadata))
            else:
                inputs.append({
                    "type": "mention",
                    "name": metadata["name"],
                    "path": str(path),
                })
                docs.append({
                    "name": metadata["name"],
                    "kind": metadata["kind"],
                })
        client_id = secrets.token_hex(16) if docs else None
        if client_id is not None:
            message_dir = self._message_directory(clean_thread, create=True)
            _atomic_write(
                message_dir / f"{client_id}.json",
                json.dumps(
                    {"docs": docs}, ensure_ascii=False, separators=(",", ":")
                ).encode(),
            )
        return PreparedAttachments(
            inputs=inputs,
            images=images,
            docs=docs,
            client_user_message_id=client_id,
        )

    def describe_staged(self, attachment_ids: str) -> list[dict[str, Any]]:
        """Return safe UI metadata for attachments waiting in a queue.

        Queue items retain staged upload ids until the head is claimed by
        ``prepare``.  The browser needs names/types for chips and a thumbnail
        route, but must not receive local filesystem paths.  Missing data is
        represented explicitly so the UI can say "attachment expired" rather
        than pretending a generic attachment still exists.
        """
        described: list[dict[str, Any]] = []
        for attachment_id in _attachment_ids(attachment_ids):
            metadata = self._read_metadata(
                self.staged / f"{attachment_id}.json")
            available = False
            if metadata is not None:
                filename = str(metadata.get("filename") or "")
                data_path = self.staged / filename
                try:
                    available = (
                        data_path.is_file()
                        and not data_path.is_symlink()
                        and data_path.parent.resolve() == self.staged
                    )
                except OSError:
                    available = False
            described.append({
                "id": attachment_id,
                "kind": str((metadata or {}).get("kind") or "file"),
                "name": str((metadata or {}).get("name") or "file"),
                "mime": str(
                    (metadata or {}).get("mime")
                    or "application/octet-stream"
                ),
                "available": available,
            })
        return described

    def resolve_staged_image(self, attachment_id: str) -> tuple[Path, str]:
        """Resolve one queued image without claiming it for a thread."""
        ids = _attachment_ids(attachment_id)
        if len(ids) != 1:
            raise HTTPException(400, "bad queued image id")
        clean_id = ids[0]
        metadata = self._read_metadata(self.staged / f"{clean_id}.json")
        if metadata is None or metadata.get("kind") != "image":
            raise HTTPException(404, "queued image not found")
        filename = str(metadata.get("filename") or "")
        path = self.staged / filename
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(self.staged)
        except (OSError, ValueError):
            raise HTTPException(404, "queued image not found") from None
        if path.is_symlink() or not resolved.is_file():
            raise HTTPException(404, "queued image not found")
        return resolved, str(metadata.get("mime") or "image/*")

    def history_items(
        self,
        thread_id: str,
        content: list[Any],
        client_user_message_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        clean_thread = _thread_id(thread_id)
        images: list[dict[str, Any]] = []
        docs: list[dict[str, Any]] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            path_value = item.get("path")
            if item_type not in {"localImage", "mention"} or not isinstance(path_value, str):
                continue
            metadata = self._metadata_for_path(clean_thread, Path(path_value))
            if metadata is None:
                continue
            if item_type == "localImage" and metadata.get("kind") == "image":
                images.append(self._image_ui(clean_thread, metadata))
            elif item_type == "mention":
                docs.append({
                    "name": str(metadata.get("name") or "file"),
                    "kind": str(metadata.get("kind") or "file"),
                })
        sidecar = self._message_metadata(clean_thread, client_user_message_id)
        for doc in sidecar.get("docs", []) if sidecar else []:
            if isinstance(doc, dict) and doc not in docs:
                docs.append(doc)
        return images, docs

    def resolve(self, thread_id: str, filename: str) -> tuple[Path, str]:
        clean_thread = _thread_id(thread_id)
        if Path(filename).name != filename or ".." in filename:
            raise HTTPException(400, "bad attachment filename")
        directory = self._thread_directory(clean_thread)
        path = (directory / filename).resolve()
        try:
            path.relative_to(directory)
        except ValueError:
            raise HTTPException(400, "bad attachment path") from None
        if not path.is_file() or path.suffix == ".json":
            raise HTTPException(404, "attachment not found")
        metadata = self._read_metadata(directory / f"{path.stem}.json")
        if metadata is None or metadata.get("filename") != filename:
            raise HTTPException(404, "attachment not found")
        return path, str(metadata.get("mime") or "application/octet-stream")

    def delete_thread(self, thread_id: str) -> None:
        raw = self.threads / _thread_id(thread_id)
        if raw.is_symlink():
            raw.unlink(missing_ok=True)
            return
        try:
            directory = self._thread_directory(raw.name)
        except ValueError:
            return
        shutil.rmtree(directory, ignore_errors=True)

    def _claim(self, thread_id: str, attachment_id: str) -> tuple[dict[str, Any], Path]:
        directory = self._thread_directory(thread_id, create=True)
        existing_meta = self._read_metadata(directory / f"{attachment_id}.json")
        if existing_meta is not None:
            existing = directory / str(existing_meta.get("filename") or "")
            if existing.is_file():
                return existing_meta, existing.resolve()

        staged_meta = self._read_metadata(self.staged / f"{attachment_id}.json")
        if staged_meta is None:
            raise ValueError(f"attachment is missing or expired: {attachment_id}")
        source = self.staged / str(staged_meta.get("filename") or "")
        if (not source.is_file() or source.is_symlink()
                or source.parent.resolve() != self.staged):
            raise ValueError(f"attachment is invalid: {attachment_id}")
        destination = directory / source.name
        os.replace(source, destination)
        try:
            os.replace(
                self.staged / f"{attachment_id}.json",
                directory / f"{attachment_id}.json",
            )
        except BaseException:
            os.replace(destination, source)
            raise
        return staged_meta, destination.resolve()

    def _metadata_for_path(self, thread_id: str, path: Path) -> dict[str, Any] | None:
        try:
            directory = self._thread_directory(thread_id)
        except ValueError:
            return None
        try:
            resolved = path.resolve()
            resolved.relative_to(directory)
        except (OSError, ValueError):
            return None
        return self._read_metadata(directory / f"{resolved.stem}.json")

    def _thread_directory(self, thread_id: str, *, create: bool = False) -> Path:
        raw = self.threads / thread_id
        if create:
            raw.mkdir(parents=True, exist_ok=True)
        try:
            directory = raw.resolve()
            directory.relative_to(self.threads)
        except (OSError, ValueError):
            raise ValueError("attachment thread directory escaped the workspace") from None
        return directory

    def _message_directory(self, thread_id: str, *, create: bool = False) -> Path:
        thread_dir = self._thread_directory(thread_id, create=create)
        raw = thread_dir / "messages"
        if create:
            raw.mkdir(parents=True, exist_ok=True)
        try:
            directory = raw.resolve()
            directory.relative_to(thread_dir)
        except (OSError, ValueError):
            raise ValueError("attachment message directory escaped the workspace") from None
        return directory

    def _message_metadata(
        self,
        thread_id: str,
        client_user_message_id: str | None,
    ) -> dict[str, Any] | None:
        if not isinstance(client_user_message_id, str) or not _ID_RE.fullmatch(
            client_user_message_id
        ):
            return None
        try:
            directory = self._message_directory(thread_id)
        except ValueError:
            return None
        return self._read_metadata(directory / f"{client_user_message_id}.json")

    def _image_ui(self, thread_id: str, metadata: dict[str, Any]) -> dict[str, Any]:
        filename = str(metadata["filename"])
        return {
            "name": str(metadata.get("name") or "image"),
            "mime": str(metadata.get("mime") or "image/*"),
            "url": f"/api/chat/attachments/{thread_id}/{filename}",
        }

    @staticmethod
    def _read_metadata(path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def _write_staged(
        self,
        data_path: Path,
        body: bytes,
        attachment_id: str,
        metadata_body: bytes,
    ) -> None:
        _atomic_write(data_path, body)
        try:
            _atomic_write(self.staged / f"{attachment_id}.json", metadata_body)
        except BaseException:
            data_path.unlink(missing_ok=True)
            raise


def _classify(body: bytes, content_type: str, name: str) -> tuple[str, str, str]:
    image_format = _image_format(body)
    if image_format is not None:
        mime, extension = _IMAGE_EXTENSIONS[image_format]
        return "image", mime, extension
    suffix = Path(name).suffix.lower()
    if body.startswith(b"%PDF-") and (suffix == ".pdf" or content_type.lower() == "application/pdf"):
        return "pdf", "application/pdf", ".pdf"
    if suffix in _TEXT_EXTENSIONS:
        return "text", _text_mime(content_type), suffix
    if suffix in _SHEET_EXTENSIONS and body.startswith(b"PK\x03\x04"):
        return "text", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", suffix
    raise HTTPException(
        400,
        "unsupported file type; accepted: PNG/JPEG/GIF/WebP, PDF, text/code, or XLSX",
    )


def _image_format(body: bytes) -> str | None:
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if body.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if body.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(body) >= 12 and body.startswith(b"RIFF") and body[8:12] == b"WEBP":
        return "webp"
    return None


def _text_mime(content_type: str) -> str:
    clean = content_type.split(";", 1)[0].strip().lower()
    return clean if clean else "text/plain"


def _safe_name(value: str) -> str:
    name = Path(value.replace("\\", "/")).name
    name = "".join(ch for ch in name if ch >= " " and ch != "\x7f").strip()
    return name[:180] or "upload"


def _thread_id(value: str) -> str:
    clean = value.strip()
    if not _THREAD_RE.fullmatch(clean):
        raise ValueError("invalid thread id")
    return clean


def _attachment_ids(value: str) -> list[str]:
    ids = list(dict.fromkeys(part.strip() for part in value.split(",") if part.strip()))
    if len(ids) > _MAX_ATTACHMENTS:
        raise ValueError(f"at most {_MAX_ATTACHMENTS} attachments are allowed")
    if any(not _ID_RE.fullmatch(item) for item in ids):
        raise ValueError("invalid attachment id")
    return ids


def _atomic_write(path: Path, body: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        temporary.write_bytes(body)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
