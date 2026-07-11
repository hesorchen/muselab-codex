"""Codex-native scheduled prompts and headless turn history."""

from __future__ import annotations

import asyncio
import calendar
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..settings import atomic_write_text
from .threads import CodexThreadService
from .turns import CodexTurnService


class CodexScheduler:
    """Persistent schedule metadata; all model work stays in app-server."""

    def __init__(self, workspace: Path, threads: CodexThreadService,
                 turns: CodexTurnService):
        self.workspace = Path(workspace).resolve()
        self.threads = threads
        self.turns = turns
        self.path = self.workspace / ".muselab-codex" / "scheduler.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._state: dict[str, Any] = {"tasks": {}, "history": [], "unread_count": 0}
        self._lock = asyncio.Lock()
        self._runner: asyncio.Task | None = None
        self._runs: set[asyncio.Task] = set()

    async def start(self) -> None:
        async with self._lock:
            self._load()
            self._roll_forward()
            self._save()
        if self._runner is None or self._runner.done():
            self._runner = asyncio.create_task(self._loop(), name="codex-scheduler")

    async def close(self) -> None:
        if self._runner is not None:
            self._runner.cancel()
            await asyncio.gather(self._runner, return_exceptions=True)
        tasks = tuple(self._runs)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def list_tasks(self) -> dict[str, Any]:
        async with self._lock:
            return {"tasks": list(self._state["tasks"].values()),
                    "unread_count": self._state["unread_count"]}

    async def create(self, data: dict[str, Any]) -> dict[str, Any]:
        schedule = _clean_schedule(data.get("schedule"))
        next_run = _next_run(schedule)
        if next_run is None:
            raise ValueError("schedule does not produce a future run")
        mode = data.get("session_mode") or "fresh"
        if mode not in {"fresh", "reuse"}:
            raise ValueError("session_mode must be fresh or reuse")
        task = {
            "id": uuid.uuid4().hex,
            "name": _required(data.get("name"), "name", 80),
            "prompt": _required(data.get("prompt"), "prompt", 20000),
            "model": str(data.get("model") or ""),
            "session_mode": mode,
            "session_id": "",
            "schedule": schedule,
            "enabled": True,
            "last_run": None,
            "next_run": next_run,
            "created_at": time.time(),
        }
        async with self._lock:
            self._state["tasks"][task["id"]] = task
            self._save()
        return dict(task)

    async def update(self, task_id: str, changes: dict[str, Any]) -> dict[str, Any] | None:
        async with self._lock:
            task = self._state["tasks"].get(task_id)
            if task is None:
                return None
            for key, limit in (("name", 80), ("prompt", 20000)):
                if key in changes and changes[key] is not None:
                    task[key] = _required(changes[key], key, limit)
            if "model" in changes and changes["model"] is not None:
                task["model"] = str(changes["model"])
            if "enabled" in changes and changes["enabled"] is not None:
                task["enabled"] = bool(changes["enabled"])
            if "session_mode" in changes and changes["session_mode"] is not None:
                mode = str(changes["session_mode"])
                if mode not in {"fresh", "reuse"}:
                    raise ValueError("session_mode must be fresh or reuse")
                task["session_mode"] = mode
            if "schedule" in changes and changes["schedule"] is not None:
                task["schedule"] = _clean_schedule(changes["schedule"])
                task["next_run"] = _next_run(task["schedule"])
            self._save()
            return dict(task)

    async def delete(self, task_id: str) -> bool:
        async with self._lock:
            deleted = self._state["tasks"].pop(task_id, None) is not None
            if deleted:
                self._save()
            return deleted

    async def history(self, task_id: str = "", limit: int = 50) -> dict[str, Any]:
        async with self._lock:
            rows = self._state["history"]
            if task_id:
                rows = [row for row in rows if row.get("task_id") == task_id]
            return {"history": list(reversed(rows[-limit:])),
                    "unread_count": self._state["unread_count"]}

    async def ack(self) -> int:
        async with self._lock:
            self._state["unread_count"] = 0
            self._save()
            return 0

    async def clear_history(self) -> int:
        async with self._lock:
            count = len(self._state["history"])
            self._state["history"] = []
            self._save()
            return count

    async def delete_history(self, ts: float, task_id: str = "") -> bool:
        async with self._lock:
            before = len(self._state["history"])
            self._state["history"] = [row for row in self._state["history"]
                                      if not (row.get("ts") == ts
                                              and (not task_id or row.get("task_id") == task_id))]
            deleted = len(self._state["history"]) != before
            if deleted:
                self._save()
            return deleted

    async def run_now(self, task_id: str) -> bool:
        async with self._lock:
            task = self._state["tasks"].get(task_id)
            if task is None:
                return False
            snapshot = dict(task)
        self._track(asyncio.create_task(self._run(snapshot)))
        return True

    def _track(self, task: asyncio.Task) -> None:
        self._runs.add(task)
        task.add_done_callback(self._runs.discard)

    async def _run(self, task: dict[str, Any]) -> None:
        started = time.time()
        sid = str(task.get("session_id") or "")
        error: str | None = None
        reply = ""
        try:
            if task.get("session_mode") == "fresh" or not sid:
                thread = await self.threads.start(
                    name=f"[Scheduled] {task['name']}", model=task.get("model") or None)
                sid = thread["id"]
                if task.get("session_mode") == "reuse":
                    async with self._lock:
                        stored = self._state["tasks"].get(task["id"])
                        if stored is not None:
                            stored["session_id"] = sid
                            self._save()
            stream = await self.turns.start(
                sid, task["prompt"], model=task.get("model") or "",
                permission="default")
            deadline = time.monotonic() + 300
            while not stream.done and time.monotonic() < deadline:
                await asyncio.sleep(0.1)
            if not stream.done:
                await self.turns.interrupt(sid)
                error = "headless turn timed out waiting for completion"
            elif stream.status != "completed":
                error = f"turn status: {stream.status}"
            reply = "".join(event["data"].get("text", "") for event in stream.events
                            if event.get("event") == "text")
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        entry = {"task_id": task["id"], "task_name": task["name"],
                 "session_id": sid, "ts": started, "ok": error is None,
                 "error": error, "reply_preview": reply[:240] if not error else None}
        async with self._lock:
            stored = self._state["tasks"].get(task["id"])
            if stored is not None:
                stored["last_run"] = time.time()
                stored["session_id"] = sid
            self._state["history"].append(entry)
            self._state["history"] = self._state["history"][-200:]
            self._state["unread_count"] += 1
            self._save()

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            now = time.time()
            due: list[dict[str, Any]] = []
            async with self._lock:
                for task in self._state["tasks"].values():
                    if task.get("enabled") and task.get("next_run", 0) <= now:
                        due.append(dict(task))
                        task["next_run"] = _next_run(task["schedule"], now)
                        if task["schedule"]["kind"] == "once":
                            task["enabled"] = False
                if due:
                    self._save()
            for task in due:
                self._track(asyncio.create_task(self._run(task)))

    def _roll_forward(self) -> None:
        now = time.time()
        for task in self._state["tasks"].values():
            if task.get("next_run", 0) <= now:
                task["next_run"] = _next_run(task.get("schedule") or {}, now)

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("tasks"), dict):
                self._state = {"tasks": raw["tasks"],
                               "history": raw.get("history", []),
                               "unread_count": int(raw.get("unread_count", 0))}
        except (OSError, ValueError, TypeError):
            pass

    def _save(self) -> None:
        atomic_write_text(self.path, json.dumps(self._state, ensure_ascii=False))


def _required(value: Any, name: str, limit: int) -> str:
    clean = str(value or "").strip()
    if not clean or len(clean) > limit:
        raise ValueError(f"invalid {name}")
    return clean


def _clean_schedule(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("invalid schedule")
    kind = value.get("kind")
    if kind not in {"daily", "weekly", "monthly", "once"}:
        raise ValueError("invalid schedule kind")
    hour, minute = int(value.get("hour", -1)), int(value.get("minute", -1))
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("invalid schedule time")
    result = {"kind": kind, "hour": hour, "minute": minute}
    for key in ("weekdays", "day", "year", "month", "tz", "tz_offset_minutes", "times"):
        if key in value and value[key] is not None:
            result[key] = value[key]
    if "times" in result:
        slots = result["times"]
        if not isinstance(slots, list) or not slots:
            raise ValueError("invalid schedule times")
        normalized = []
        for slot in slots:
            if not isinstance(slot, dict):
                raise ValueError("invalid schedule times")
            slot_hour, slot_minute = int(slot.get("hour", -1)), int(slot.get("minute", -1))
            if not 0 <= slot_hour <= 23 or not 0 <= slot_minute <= 59:
                raise ValueError("invalid schedule times")
            normalized.append({"hour": slot_hour, "minute": slot_minute})
        result["times"] = normalized
    return result


def _next_run(schedule: dict[str, Any], now: float | None = None) -> float | None:
    try:
        tz = ZoneInfo(str(schedule.get("tz"))) if schedule.get("tz") else timezone(
            timedelta(minutes=int(schedule.get("tz_offset_minutes", 0))))
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        tz = timezone.utc
    base = datetime.fromtimestamp(now or time.time(), tz)
    hour, minute = schedule["hour"], schedule["minute"]
    kind = schedule["kind"]
    if kind == "once":
        candidate = datetime(int(schedule.get("year", 0)), int(schedule.get("month", 0)),
                             int(schedule.get("day", 0)), hour, minute, tzinfo=tz)
        return candidate.timestamp() if candidate > base else None
    slots = [(hour, minute)]
    if kind == "daily" and schedule.get("times"):
        slots = [(slot["hour"], slot["minute"]) for slot in schedule["times"]]
    for delta in range(0, 370):
        day = base + timedelta(days=delta)
        if kind == "monthly":
            requested = int(schedule.get("day", 1))
            if day.day != min(requested, calendar.monthrange(day.year, day.month)[1]):
                continue
        if kind == "weekly" and day.weekday() not in set(schedule.get("weekdays") or []):
            continue
        for slot_hour, slot_minute in sorted(slots):
            candidate = day.replace(hour=slot_hour, minute=slot_minute,
                                    second=0, microsecond=0)
            if candidate > base:
                return candidate.timestamp()
    return None
