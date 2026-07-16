"""Durable cross-workspace task activity ledger."""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .settings import atomic_write_text


_MAX_EVENTS = 500
_TERMINAL = {"completed", "failed", "cancelled"}
_GENERIC_SUMMARIES = {
    "", "Muse is working", "Task completed", "Task failed", "Task cancelled",
    "Interrupted by service restart",
}


class ActivityService:
    """Keep a small flat-file ledger of work that deserves user attention."""

    def __init__(self, root: Path, threads: Any):
        self.path = root / ".muselab-codex" / "activity.json"
        self.threads = threads
        self._lock = threading.RLock()
        self._events = self._load()
        self._tasks: set[asyncio.Task] = set()
        changed = self._collapse_threads()
        for item in self._events:
            if item.get("state") in {"running", "waiting_approval", "paused"}:
                item.update({
                    "state": "failed",
                    "status_detail": "Interrupted by service restart",
                    "finished_at": time.time(),
                    "needs_attention": True,
                    "read": False,
                })
                changed = True
        if changed:
            self._save()

    def _collapse_threads(self) -> bool:
        """Keep one current activity card per native conversation."""
        latest: dict[str, dict[str, Any]] = {}
        anonymous: list[dict[str, Any]] = []
        for item in self._events:
            thread_id = str(item.get("thread_id") or "")
            if thread_id:
                previous = latest.pop(thread_id, None)
                if previous is not None:
                    item["turn_count"] = max(
                        int(item.get("turn_count") or 1),
                        int(previous.get("turn_count") or 1) + 1,
                    )
                latest[thread_id] = item
            else:
                anonymous.append(item)
        collapsed = anonymous + list(latest.values())
        changed = len(collapsed) != len(self._events)
        self._events = collapsed[-_MAX_EVENTS:]
        return changed

    def _load(self) -> list[dict[str, Any]]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)][-_MAX_EVENTS:]
        except (OSError, ValueError):
            pass
        return []

    def _save(self) -> None:
        atomic_write_text(
            self.path,
            json.dumps(self._events[-_MAX_EVENTS:], ensure_ascii=False, indent=2),
        )

    async def _thread_meta(self, thread_id: str) -> tuple[str, str, str, str]:
        name = ""
        preview = ""
        cwd = ""
        try:
            thread = await self.threads.read(thread_id, include_turns=False)
            name = str(thread.get("name") or "").strip()
            preview = str(thread.get("preview") or "").strip()
            cwd = str(thread.get("cwd") or "").strip()
        except Exception:
            pass
        workspaces = {entry.path: entry.name for entry in self.threads.list_workspaces()}
        workspace_name = workspaces.get(cwd) or Path(cwd).name or "Workspace"
        return name or preview or "Muse task", preview, cwd, workspace_name

    def _latest_open(self, thread_id: str) -> dict[str, Any] | None:
        for item in reversed(self._events):
            if item.get("thread_id") == thread_id and item.get("state") not in _TERMINAL:
                return item
        return None

    async def start(self, thread_id: str, *, summary: str = "") -> dict[str, Any]:
        now = time.time()
        with self._lock:
            previous = next((item for item in reversed(self._events)
                             if item.get("thread_id") == thread_id), None)
            if previous is not None:
                previous.update({
                    "state": "running",
                    "summary": summary or previous.get("summary", ""),
                    "task_summary": summary or previous.get("task_summary", ""),
                    "status_detail": "",
                    "started_at": now,
                    "finished_at": None,
                    "needs_attention": False,
                    "read": True,
                    "turn_count": int(previous.get("turn_count") or 1) + 1,
                })
                self._save()
                result = dict(previous)
                event_id = str(previous.get("id") or "")
            else:
                item = {
                    "id": uuid.uuid4().hex,
                    "thread_id": thread_id,
                    "workspace": "",
                    "workspace_name": "Workspace",
                    "session_name": "Muse task",
                    "kind": "turn",
                    "state": "running",
                    "summary": summary or "Muse is working",
                    "task_summary": summary or "",
                    "status_detail": "",
                    "started_at": now,
                    "finished_at": None,
                    "needs_attention": False,
                    "read": True,
                    "turn_count": 1,
                }
                self._events.append(item)
                self._events = self._events[-_MAX_EVENTS:]
                self._save()
                result = dict(item)
                event_id = item["id"]
        task = asyncio.create_task(self._enrich(event_id, thread_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return result

    async def _enrich(self, event_id: str, thread_id: str) -> None:
        name, preview, workspace, workspace_name = await self._thread_meta(thread_id)
        with self._lock:
            for item in self._events:
                if item.get("id") == event_id:
                    item.update({
                        "workspace": workspace,
                        "workspace_name": workspace_name,
                        "session_name": name,
                    })
                    current_task = str(item.get("task_summary") or "").strip()
                    legacy_summary = str(item.get("summary") or "").strip()
                    if not current_task:
                        if legacy_summary not in _GENERIC_SUMMARIES:
                            current_task = legacy_summary
                        elif preview:
                            current_task = preview[:500]
                    item["task_summary"] = current_task or name
                    self._save()
                    return

    async def refresh_metadata(self) -> None:
        """Backfill old generic ledger rows from their native thread preview."""
        with self._lock:
            rows = [
                (str(item.get("id") or ""), str(item.get("thread_id") or ""))
                for item in self._events[-150:]
                if not item.get("task_summary")
                or item.get("session_name") in {None, "", "Muse task"}
            ]
        # Keep app-server control-plane pressure bounded during startup.
        for event_id, thread_id in rows:
            if event_id and thread_id:
                try:
                    await self._enrich(event_id, thread_id)
                except Exception:
                    pass

    async def drain(self) -> None:
        """Wait for pending metadata lookups during orderly shutdown/tests."""
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    async def set_state(
        self, thread_id: str, state: str, *, summary: str = "",
    ) -> dict[str, Any] | None:
        if state not in {"running", "waiting_approval", "paused"}:
            raise ValueError("invalid active activity state")
        with self._lock:
            item = self._latest_open(thread_id)
        if item is None:
            await self.start(thread_id, summary=summary)
        with self._lock:
            item = self._latest_open(thread_id)
            if item is None:
                return None
            item["state"] = state
            if summary:
                item["status_detail"] = summary[:500]
            item["needs_attention"] = state in {"waiting_approval", "paused"}
            item["read"] = not item["needs_attention"]
            self._save()
            return dict(item)

    async def finish(self, thread_id: str, status: str) -> dict[str, Any] | None:
        state = "completed" if status == "completed" else (
            "cancelled" if status in {"cancelled", "interrupted"} else "failed")
        with self._lock:
            item = self._latest_open(thread_id)
        if item is None:
            await self.start(thread_id)
        with self._lock:
            item = self._latest_open(thread_id)
            if item is None:
                return None
            item.update({
                "state": state,
                "finished_at": time.time(),
                "needs_attention": state != "cancelled",
                "read": state == "cancelled",
                "status_detail": {
                    "completed": "Task completed",
                    "failed": "Task failed",
                    "cancelled": "Task cancelled",
                }[state],
            })
            self._save()
            return dict(item)

    def list(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in reversed(self._events[-max(1, min(limit, 500)):])]

    def latest_thread(self, thread_id: str) -> dict[str, Any] | None:
        with self._lock:
            for item in reversed(self._events):
                if item.get("thread_id") == thread_id:
                    return dict(item)
        return None

    def summary(self) -> dict[str, Any]:
        with self._lock:
            events = [dict(item) for item in self._events]
        running_states = {"running", "waiting_approval", "paused"}
        running = sum(item.get("state") in running_states for item in events)
        unread = sum(bool(item.get("needs_attention")) and not item.get("read") for item in events)
        attention = sum(
            item.get("state") in {"failed", "waiting_approval", "paused"}
            and not item.get("read") for item in events)
        workspaces: dict[str, dict[str, Any]] = {}
        for item in events:
            path = str(item.get("workspace") or "")
            row = workspaces.setdefault(path, {
                "path": path,
                "name": item.get("workspace_name") or Path(path).name or "Workspace",
                "running": 0,
                "unread": 0,
                "attention": 0,
            })
            if item.get("state") in running_states:
                row["running"] += 1
            if item.get("needs_attention") and not item.get("read"):
                row["unread"] += 1
            if item.get("state") in {"failed", "waiting_approval", "paused"} and not item.get("read"):
                row["attention"] += 1
        return {
            "running": running,
            "unread": unread,
            "attention": attention,
            "workspaces": list(workspaces.values()),
        }

    def ack(self, event_id: str | None = None) -> int:
        changed = 0
        with self._lock:
            for item in self._events:
                if event_id is not None and item.get("id") != event_id:
                    continue
                if item.get("needs_attention") and not item.get("read"):
                    item["read"] = True
                    changed += 1
            if changed:
                self._save()
        return changed

    def ack_thread(self, thread_id: str) -> int:
        changed = 0
        with self._lock:
            for item in self._events:
                if item.get("thread_id") != thread_id:
                    continue
                if item.get("needs_attention") and not item.get("read"):
                    item["read"] = True
                    changed += 1
            if changed:
                self._save()
        return changed
