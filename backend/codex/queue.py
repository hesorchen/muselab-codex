"""Persistent queue state for messages waiting behind a Codex turn."""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from collections import defaultdict
from pathlib import Path
import threading
from typing import Any

from ..settings import atomic_write_text
from .threads import normalize_service_tier
from .usage import _thread_id


_SCHEMA_VERSION = 2
_TOMBSTONE_LIMIT = 128
_TOMBSTONE_TTL = 7 * 24 * 60 * 60


class CodexQueueService:
    def __init__(
        self,
        state_dir: Path | None = None,
        *,
        max_items: int = 10,
    ):
        self.max_items = max_items
        self._items: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._tombstones: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._paused: set[str] = set()
        self._unreadable_threads: set[str] = set()
        self._lock = threading.RLock()
        self._state_dir = Path(state_dir).resolve() if state_dir is not None else None
        if self._state_dir is not None:
            self._state_dir.mkdir(parents=True, exist_ok=True)
            self._load()

    def get(self, thread_id: str) -> dict[str, Any]:
        sid = _thread_id(thread_id)
        with self._lock:
            return {"items": [dict(item) for item in self._items.get(sid, [])],
                    "paused": sid in self._paused}

    def enqueue(self, thread_id: str, text: str, image_ids: str = "",
                permission: str = "", *, model: str = "",
                model_provider: str = "", effort: str = "",
                service_tier: str | None = None,
                source_device_kind: str = "unknown",
                item_id: str | None = None,
                client_user_message_id: str | None = None) -> dict[str, Any]:
        sid = _thread_id(thread_id)
        text = text.strip()
        image_ids = image_ids.strip()
        if not text and not image_ids:
            raise ValueError("empty message")
        with self._lock:
            current = [dict(item) for item in self._items.get(sid, [])]
            if len(current) >= self.max_items:
                raise OverflowError("queue_full")
            queue_id = item_id or uuid.uuid4().hex
            client_id = client_user_message_id or uuid.uuid4().hex
            if not queue_id or not client_id:
                raise ValueError("invalid queue identity")
            if any(item.get("id") == queue_id for item in current):
                raise ValueError("duplicate queue item")
            item = {"id": queue_id, "text": text,
                    "image_ids": image_ids, "permission": permission,
                    "model": model.strip(), "model_provider": model_provider.strip(),
                    "effort": effort.strip(),
                    "service_tier": normalize_service_tier(service_tier),
                    "source_device_kind": (
                        source_device_kind
                        if source_device_kind in {"mobile", "desktop"}
                        else "unknown"
                    ),
                    "enqueued_at": time.time(),
                    "state": "queued",
                    "client_user_message_id": client_id,
                    "start_attempts": 0}
            current.append(item)
            self._commit(sid, current, *self._other_state(sid))
            return dict(item)

    def remove(self, thread_id: str, item_id: str) -> bool:
        return self.remove_item(thread_id, item_id) is not None

    def remove_item(self, thread_id: str, item_id: str) -> dict[str, Any] | None:
        sid = _thread_id(thread_id)
        with self._lock:
            current = [dict(item) for item in self._items.get(sid, [])]
            for index, item in enumerate(current):
                if item["id"] == item_id:
                    if item.get("state") == "starting":
                        return None
                    removed = current.pop(index)
                    tombstones, paused = self._other_state(sid)
                    self._commit(sid, current, tombstones, paused)
                    return dict(removed)
            return None

    def clear(
        self,
        thread_id: str,
        *,
        include_starting: bool = False,
    ) -> list[dict[str, Any]]:
        sid = _thread_id(thread_id)
        with self._lock:
            current = [dict(item) for item in self._items.get(sid, [])]
            removed = [item for item in current
                       if include_starting
                       or item.get("state") != "starting"]
            retained = [] if include_starting else [
                item for item in current if item.get("state") == "starting"]
            tombstones, _paused = self._other_state(sid)
            # Tombstones are protocol idempotency records, not visible queue
            # rows.  Clearing UI work must not erase them or a crash-left
            # acknowledged message can be replayed.
            self._commit(sid, retained, tombstones, False)
            return removed

    def pause(self, thread_id: str, paused: bool) -> dict[str, Any]:
        sid = _thread_id(thread_id)
        with self._lock:
            items = [dict(item) for item in self._items.get(sid, [])]
            tombstones, _old_paused = self._other_state(sid)
            self._commit(sid, items, tombstones, paused)
            return self.get(sid)

    def reorder(self, thread_id: str, order: list[str]) -> dict[str, Any]:
        sid = _thread_id(thread_id)
        with self._lock:
            items = [dict(item) for item in self._items.get(sid, [])]
            if any(item.get("state") == "starting" for item in items):
                raise ValueError("cannot reorder while a queue item is starting")
            current = {item["id"]: item for item in items}
            if set(order) != set(current) or len(order) != len(current):
                raise ValueError("queue order must contain every item exactly once")
            tombstones, paused = self._other_state(sid)
            self._commit(
                sid, [current[item_id] for item_id in order],
                tombstones, paused)
            return self.get(sid)

    def take_next(self, thread_id: str) -> dict[str, Any] | None:
        """Legacy destructive pop retained for API/tests outside v2 drain."""
        sid = _thread_id(thread_id)
        with self._lock:
            items = [dict(item) for item in self._items.get(sid, [])]
            if sid in self._paused or not items:
                return None
            index = next((i for i, item in enumerate(items)
                          if item.get("state") == "queued"), None)
            if index is None:
                return None
            item = items.pop(index)
            tombstones, paused = self._other_state(sid)
            self._commit(sid, items, tombstones, paused)
            return dict(item)

    def begin_next(self, thread_id: str) -> dict[str, Any] | None:
        """Durably move the FIFO head from queued to starting."""
        sid = _thread_id(thread_id)
        with self._lock:
            if sid in self._paused:
                return None
            items = [dict(item) for item in self._items.get(sid, [])]
            if any(item.get("state") == "starting" for item in items):
                return None
            index = next((index for index, item in enumerate(items)
                          if item.get("state") == "queued"), None)
            if index is None:
                return None
            item = dict(items[index])
            item["state"] = "starting"
            item["started_at"] = time.time()
            item["start_attempts"] = int(item.get("start_attempts") or 0) + 1
            items[index] = item
            tombstones, paused = self._other_state(sid)
            self._commit(sid, items, tombstones, paused)
            return dict(item)

    def ack_started(self, thread_id: str, item_id: str) -> bool:
        """Persist acknowledgement before forgetting a started queue row."""
        sid = _thread_id(thread_id)
        with self._lock:
            items = [dict(item) for item in self._items.get(sid, [])]
            for index, item in enumerate(items):
                if item.get("id") != item_id or item.get("state") != "starting":
                    continue
                removed = items.pop(index)
                tombstones, paused = self._other_state(sid)
                tombstones.append({
                    "id": item_id,
                    "client_user_message_id": removed.get(
                        "client_user_message_id", ""),
                    "acked_at": time.time(),
                })
                self._commit(sid, items, tombstones, paused)
                return True
            return False

    def rollback_start(self, thread_id: str, item_id: str) -> bool:
        """Return an unacknowledged starting row to the FIFO head."""
        sid = _thread_id(thread_id)
        with self._lock:
            items = [dict(item) for item in self._items.get(sid, [])]
            for index, item in enumerate(items):
                if item.get("id") != item_id or item.get("state") != "starting":
                    continue
                restored = items.pop(index)
                restored["state"] = "queued"
                restored.pop("started_at", None)
                items.insert(0, restored)
                tombstones, paused = self._other_state(sid)
                self._commit(sid, items, tombstones, paused)
                return True
            return False

    def restore_head(self, thread_id: str, item: dict[str, Any]) -> None:
        """Put a failed-to-start item back without changing its identity."""
        sid = _thread_id(thread_id)
        if not isinstance(item.get("id"), str):
            raise ValueError("invalid queue item")
        with self._lock:
            restored = dict(item)
            restored["state"] = "queued"
            restored.pop("started_at", None)
            # Validate the full shape before it can enter either memory or a
            # JSON snapshot.  Legacy callers may pass arbitrary dictionaries.
            restored = _valid_persisted_item(restored)
            if restored is None:
                raise ValueError("invalid queue item")
            items = [dict(existing)
                     for existing in self._items.get(sid, [])]
            if not any(existing.get("id") == restored.get("id")
                       for existing in items):
                items.insert(0, restored)
            tombstones, paused = self._other_state(sid)
            self._commit(sid, items, tombstones, paused)

    def thread_ids(self) -> list[str]:
        with self._lock:
            return sorted(sid for sid, items in self._items.items() if items)

    def attachment_references(self) -> dict[str, tuple[str, str]]:
        """Return staged attachment ownership implied by durable queue rows."""
        references: dict[str, tuple[str, str]] = {}
        with self._lock:
            for sid, items in self._items.items():
                for item in items:
                    owner = (sid, str(item.get("id") or ""))
                    for attachment_id in str(item.get("image_ids") or "").split(","):
                        clean = attachment_id.strip()
                        if clean:
                            references[clean] = owner
        return references

    def starting_items(self, thread_id: str) -> list[dict[str, Any]]:
        sid = _thread_id(thread_id)
        with self._lock:
            return [dict(item) for item in self._items.get(sid, [])
                    if item.get("state") == "starting"]

    def unreadable_thread_ids(self) -> set[str]:
        """Queue files whose attachment ownership cannot be trusted."""
        with self._lock:
            return set(self._unreadable_threads)

    def reconcile_starting(self) -> int:
        """Retained for callers that used the old eager recovery hook.

        A ``starting`` row is an uncertainty record: the native turn may have
        been accepted before the process died.  Only QueueDrain may resolve it
        after checking the canonical transcript by stable client id.
        """
        return 0

    def _path(self, sid: str) -> Path | None:
        return self._state_dir / f"{sid}.json" if self._state_dir is not None else None

    def _other_state(self, sid: str) -> tuple[list[dict[str, Any]], bool]:
        return (
            [dict(item) for item in self._tombstones.get(sid, [])],
            sid in self._paused,
        )

    def _commit(
        self,
        sid: str,
        items: list[dict[str, Any]],
        tombstones: list[dict[str, Any]],
        paused: bool,
    ) -> None:
        """Persist a candidate snapshot before publishing it in memory."""
        clean_items = [dict(item) for item in items]
        clean_tombstones = _trimmed_tombstones(tombstones)
        self._persist_snapshot(
            sid, clean_items, clean_tombstones, bool(paused))
        if clean_items:
            self._items[sid] = clean_items
        else:
            self._items.pop(sid, None)
        if clean_tombstones:
            self._tombstones[sid] = clean_tombstones
        else:
            self._tombstones.pop(sid, None)
        if paused:
            self._paused.add(sid)
        else:
            self._paused.discard(sid)

    def _persist_snapshot(
        self,
        sid: str,
        items: list[dict[str, Any]],
        tombstones: list[dict[str, Any]],
        paused: bool,
    ) -> None:
        path = self._path(sid)
        if path is None:
            return
        if not items and not paused and not tombstones:
            path.unlink(missing_ok=True)
            # Persist deletion itself; otherwise a power loss can resurrect
            # the previous queue snapshot after memory already reports empty.
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
            return
        atomic_write_text(
            path,
            json.dumps(
                {"version": _SCHEMA_VERSION, "items": items,
                 "paused": paused, "tombstones": tombstones},
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
                try:
                    self._unreadable_threads.add(_thread_id(path.stem))
                except ValueError:
                    pass
                continue
            if not isinstance(payload, dict):
                self._unreadable_threads.add(sid)
                continue
            legacy = payload.get("version") != _SCHEMA_VERSION
            rewrite = legacy
            if legacy:
                backup = path.with_suffix(path.suffix + ".v1.bak")
                if not backup.exists():
                    try:
                        shutil.copy2(path, backup)
                    except OSError:
                        continue
            raw_items = payload.get("items")
            if not isinstance(raw_items, list):
                self._unreadable_threads.add(sid)
                continue
            items: list[dict[str, Any]] = []
            for raw in raw_items[:self.max_items]:
                item = _valid_persisted_item(raw)
                if item is not None:
                    items.append(item)
                    if (not isinstance(raw, dict)
                            or raw.get("client_user_message_id")
                            != item["client_user_message_id"]):
                        # v2 files written by an interrupted migration may be
                        # missing the idempotency key.  The generated key is
                        # useful only if immediately made durable.
                        rewrite = True
                else:
                    rewrite = True
            if len(raw_items) > self.max_items:
                rewrite = True
            if items:
                self._items[sid] = items
            raw_tombstones = payload.get("tombstones")
            if isinstance(raw_tombstones, list):
                parsed_tombstones = [
                    tombstone for raw in raw_tombstones
                    if (tombstone := _valid_tombstone(raw)) is not None
                ]
                trimmed = _trimmed_tombstones(parsed_tombstones)
                if trimmed != parsed_tombstones:
                    rewrite = True
                if trimmed:
                    self._tombstones[sid] = trimmed
            if payload.get("paused") is True:
                self._paused.add(sid)
            if rewrite:
                self._persist_snapshot(
                    sid,
                    [dict(item) for item in self._items.get(sid, [])],
                    [dict(item) for item in self._tombstones.get(sid, [])],
                    sid in self._paused,
                )


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
    try:
        start_attempts = max(0, int(value.get("start_attempts") or 0))
        started_at = float(value.get("started_at") or 0)
    except (TypeError, ValueError):
        start_attempts = 0
        started_at = 0.0
    try:
        service_tier = normalize_service_tier(value.get("service_tier"))
    except (AttributeError, ValueError):
        service_tier = None
    return {
        "id": item_id,
        "text": text,
        "image_ids": image_ids,
        "permission": str(value.get("permission") or ""),
        "model": str(value.get("model") or ""),
        "model_provider": str(value.get("model_provider") or ""),
        "effort": str(value.get("effort") or ""),
        "service_tier": service_tier,
        "source_device_kind": source
        if source in {"mobile", "desktop"} else "unknown",
        "enqueued_at": enqueued_at,
        "state": value.get("state")
        if value.get("state") in {"queued", "starting"} else "queued",
        "client_user_message_id": str(
            value.get("client_user_message_id") or uuid.uuid4().hex),
        "start_attempts": start_attempts,
        **({"started_at": started_at}
           if value.get("state") == "starting" else {}),
    }


def _valid_tombstone(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    item_id = value.get("id")
    client_id = value.get("client_user_message_id")
    try:
        acked_at = float(value.get("acked_at") or 0)
    except (TypeError, ValueError):
        return None
    if not isinstance(item_id, str) or not item_id:
        return None
    if not isinstance(client_id, str) or not client_id:
        return None
    return {"id": item_id, "client_user_message_id": client_id,
            "acked_at": acked_at}


def _trimmed_tombstones(
    tombstones: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    cutoff = time.time() - _TOMBSTONE_TTL
    return [
        dict(item) for item in tombstones
        if float(item.get("acked_at") or 0) >= cutoff
    ][-_TOMBSTONE_LIMIT:]
