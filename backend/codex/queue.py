"""Process-local queue state for messages waiting behind a Codex turn."""

from __future__ import annotations

import time
import uuid
from collections import defaultdict
from typing import Any

from .usage import _thread_id


class CodexQueueService:
    def __init__(self, *, max_items: int = 10):
        self.max_items = max_items
        self._items: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._paused: set[str] = set()

    def get(self, thread_id: str) -> dict[str, Any]:
        sid = _thread_id(thread_id)
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
        return dict(item)

    def remove(self, thread_id: str, item_id: str) -> bool:
        sid = _thread_id(thread_id)
        before = len(self._items[sid])
        self._items[sid] = [item for item in self._items[sid]
                            if item["id"] != item_id]
        return len(self._items[sid]) != before

    def clear(self, thread_id: str) -> None:
        sid = _thread_id(thread_id)
        self._items.pop(sid, None)
        self._paused.discard(sid)

    def pause(self, thread_id: str, paused: bool) -> dict[str, Any]:
        sid = _thread_id(thread_id)
        if paused:
            self._paused.add(sid)
        else:
            self._paused.discard(sid)
        return self.get(sid)

    def reorder(self, thread_id: str, order: list[str]) -> dict[str, Any]:
        sid = _thread_id(thread_id)
        current = {item["id"]: item for item in self._items[sid]}
        if set(order) != set(current) or len(order) != len(current):
            raise ValueError("queue order must contain every item exactly once")
        self._items[sid] = [current[item_id] for item_id in order]
        return self.get(sid)

    def take_next(self, thread_id: str) -> dict[str, Any] | None:
        """Atomically remove the FIFO head unless the queue is paused."""
        sid = _thread_id(thread_id)
        if sid in self._paused or not self._items[sid]:
            return None
        return self._items[sid].pop(0)

    def restore_head(self, thread_id: str, item: dict[str, Any]) -> None:
        """Put a failed-to-start item back without changing its identity."""
        sid = _thread_id(thread_id)
        if not isinstance(item.get("id"), str):
            raise ValueError("invalid queue item")
        self._items[sid].insert(0, dict(item))
