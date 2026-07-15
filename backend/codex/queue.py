"""Persistent queue state for messages waiting behind a Codex turn."""

from __future__ import annotations

import json
import time
import uuid
from collections import defaultdict
from pathlib import Path
import threading
from typing import Any

from ..settings import atomic_write_text
from .usage import _thread_id


class CodexQueueService:
    def __init__(
        self,
        state_dir: Path | None = None,
        *,
        max_items: int = 10,
    ):
        self.max_items = max_items
        self._items: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._paused: set[str] = set()
        self._lock = threading.RLock()
        self._state_dir = Path(state_dir).resolve() if state_dir is not None else None
        if self._state_dir is not None:
            self._state_dir.mkdir(parents=True, exist_ok=True)
            self._load()

    def get(self, thread_id: str) -> dict[str, Any]:
        sid = _thread_id(thread_id)
        with self._lock:
            return {"items": [dict(item) for item in self._items[sid]],
                    "paused": sid in self._paused}

    def enqueue(self, thread_id: str, text: str, image_ids: str = "",
                permission: str = "", *, model: str = "",
                model_provider: str = "", effort: str = "",
                source_device_kind: str = "unknown") -> dict[str, Any]:
        sid = _thread_id(thread_id)
        text = text.strip()
        image_ids = image_ids.strip()
        if not text and not image_ids:
            raise ValueError("empty message")
        with self._lock:
            if len(self._items[sid]) >= self.max_items:
                raise OverflowError("queue_full")
            item = {"id": uuid.uuid4().hex, "text": text,
                    "image_ids": image_ids, "permission": permission,
                    "model": model.strip(), "model_provider": model_provider.strip(),
                    "effort": effort.strip(),
                    "source_device_kind": (
                        source_device_kind
                        if source_device_kind in {"mobile", "desktop"}
                        else "unknown"
                    ),
                    "enqueued_at": time.time()}
            self._items[sid].append(item)
            self._persist(sid)
            return dict(item)

    def remove(self, thread_id: str, item_id: str) -> bool:
        sid = _thread_id(thread_id)
        with self._lock:
            before = len(self._items[sid])
            self._items[sid] = [item for item in self._items[sid]
                                if item["id"] != item_id]
            changed = len(self._items[sid]) != before
            if changed:
                self._persist(sid)
            return changed

    def clear(self, thread_id: str) -> None:
        sid = _thread_id(thread_id)
        with self._lock:
            self._items.pop(sid, None)
            self._paused.discard(sid)
            self._persist(sid)

    def pause(self, thread_id: str, paused: bool) -> dict[str, Any]:
        sid = _thread_id(thread_id)
        with self._lock:
            if paused:
                self._paused.add(sid)
            else:
                self._paused.discard(sid)
            self._persist(sid)
            return self.get(sid)

    def reorder(self, thread_id: str, order: list[str]) -> dict[str, Any]:
        sid = _thread_id(thread_id)
        with self._lock:
            current = {item["id"]: item for item in self._items[sid]}
            if set(order) != set(current) or len(order) != len(current):
                raise ValueError("queue order must contain every item exactly once")
            self._items[sid] = [current[item_id] for item_id in order]
            self._persist(sid)
            return self.get(sid)

    def take_next(self, thread_id: str) -> dict[str, Any] | None:
        """Atomically remove the FIFO head unless the queue is paused."""
        sid = _thread_id(thread_id)
        with self._lock:
            if sid in self._paused or not self._items[sid]:
                return None
            item = self._items[sid].pop(0)
            self._persist(sid)
            return item

    def restore_head(self, thread_id: str, item: dict[str, Any]) -> None:
        """Put a failed-to-start item back without changing its identity."""
        sid = _thread_id(thread_id)
        if not isinstance(item.get("id"), str):
            raise ValueError("invalid queue item")
        with self._lock:
            self._items[sid].insert(0, dict(item))
            self._persist(sid)

    def _path(self, sid: str) -> Path | None:
        return self._state_dir / f"{sid}.json" if self._state_dir is not None else None

    def _persist(self, sid: str) -> None:
        path = self._path(sid)
        if path is None:
            return
        items = self._items.get(sid, [])
        paused = sid in self._paused
        if not items and not paused:
            path.unlink(missing_ok=True)
            return
        atomic_write_text(
            path,
            json.dumps(
                {"items": items, "paused": paused},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )

    def _load(self) -> None:
        assert self._state_dir is not None
        for path in self._state_dir.glob("*.json"):
            try:
                sid = _thread_id(path.stem)
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            raw_items = payload.get("items")
            if not isinstance(raw_items, list):
                continue
            items: list[dict[str, Any]] = []
            for raw in raw_items[:self.max_items]:
                item = _valid_persisted_item(raw)
                if item is not None:
                    items.append(item)
            if items:
                self._items[sid] = items
            if payload.get("paused") is True:
                self._paused.add(sid)


def _valid_persisted_item(value: Any) -> dict[str, Any] | None:
    """Fail closed on a partial/corrupt queue file while preserving old rows."""
    if not isinstance(value, dict):
        return None
    item_id = value.get("id")
    text = value.get("text")
    image_ids = value.get("image_ids")
    if not isinstance(item_id, str) or not item_id:
        return None
    if not isinstance(text, str) or not isinstance(image_ids, str):
        return None
    source = value.get("source_device_kind")
    raw_enqueued = value.get("enqueued_at")
    try:
        enqueued_at = float(raw_enqueued or 0)
    except (TypeError, ValueError):
        enqueued_at = 0.0
    return {
        "id": item_id,
        "text": text,
        "image_ids": image_ids,
        "permission": str(value.get("permission") or ""),
        "model": str(value.get("model") or ""),
        "model_provider": str(value.get("model_provider") or ""),
        "effort": str(value.get("effort") or ""),
        "source_device_kind": source
        if source in {"mobile", "desktop"} else "unknown",
        "enqueued_at": enqueued_at,
    }
