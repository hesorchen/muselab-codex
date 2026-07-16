"""Best-effort Web Push delivery for completed Codex turns."""

from __future__ import annotations

import asyncio
import sys
from typing import Any, Awaitable, Callable

from . import push


_turn_origins: dict[str, str] = {}
_pending_notifications: list[dict[str, Any]] = []
_notification_flush_task: asyncio.Task | None = None


def record_turn_origin(thread_id: str, device_kind: str) -> None:
    """Remember which device started the active turn for completion routing."""
    _turn_origins[thread_id] = (
        device_kind if device_kind in {"mobile", "desktop"} else "unknown")


def clear_turn_origin(thread_id: str) -> None:
    _turn_origins.pop(thread_id, None)


def completed_turn_callback(
    threads: Any,
    after_turn: Callable[[str, str], Awaitable[None]] | None = None,
    *,
    activity: Any | None = None,
) -> Callable[[str, str], Awaitable[None]]:
    """Compose queue draining with one presence-gated completion push."""

    async def callback(thread_id: str, status: str) -> None:
        # Claim this turn's origin before queue draining can start and record
        # the successor turn under the same thread id.
        origin = _turn_origins.pop(thread_id, "unknown")
        item = activity.latest_thread(thread_id) if activity is not None else None
        tasks = [_notify_completed_turn(
            threads, thread_id, origin=origin, status=status,
            activity=activity, item=item)]
        if after_turn is not None and status == "completed":
            tasks.append(after_turn(thread_id, status))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                sys.stderr.write(
                    f"[push] turn-done follow-up failed sid={thread_id}: {result}\n")

    return callback


async def _notify_completed_turn(
    threads: Any, thread_id: str, *, origin: str | None = None,
    status: str = "completed", activity: Any | None = None,
    item: dict[str, Any] | None = None,
) -> None:
    # Fan out to every subscribed device. The service worker performs the
    # correct per-device foreground suppression; a process-global presence
    # gate cannot distinguish a visible Mac from a backgrounded phone.
    _ = _turn_origins.pop(thread_id, "unknown") if origin is None else origin

    thread_name = ""
    try:
        thread = await threads.read(thread_id, include_turns=False)
        candidate = thread.get("name") if isinstance(thread, dict) else None
        if isinstance(candidate, str) and candidate.strip():
            thread_name = candidate.strip()
    except Exception:
        # A notification must not fail merely because thread lookup did.
        pass

    workspace_name = str((item or {}).get("workspace_name") or "Workspace")
    session_name = str((item or {}).get("session_name") or thread_name or "Muse")
    summary = activity.summary() if activity is not None else {"unread": 1}
    _queue_notification({
        "thread_id": thread_id,
        "workspace_name": workspace_name,
        "session_name": session_name,
        "status": status,
        "badge_count": int(summary.get("unread") or 0),
    })


def _queue_notification(item: dict[str, Any]) -> None:
    global _notification_flush_task
    _pending_notifications.append(item)
    if _notification_flush_task is None or _notification_flush_task.done():
        _notification_flush_task = asyncio.create_task(_flush_notifications())


def queue_attention_notification(item: dict[str, Any], badge_count: int) -> None:
    _queue_notification({
        "thread_id": item.get("thread_id") or "",
        "workspace_name": item.get("workspace_name") or "Workspace",
        "session_name": item.get("session_name") or "Muse task",
        "status": item.get("state") or "waiting_approval",
        "badge_count": badge_count,
    })


async def _flush_notifications() -> None:
    await asyncio.sleep(3)
    pending = list(_pending_notifications)
    _pending_notifications.clear()
    by_thread: dict[str, dict[str, Any]] = {}
    for item in pending:
        key = str(item.get("thread_id") or item.get("session_name") or len(by_thread))
        by_thread.pop(key, None)
        by_thread[key] = item
    batch = list(by_thread.values())
    if not batch:
        return
    latest = batch[-1]
    failed = [item for item in batch if item.get("status") != "completed"]
    if len(batch) == 1:
        status = latest.get("status")
        prefix = (
            "任务已完成" if status == "completed"
            else "任务需要处理" if status in {"waiting_approval", "paused"}
            else "任务失败")
        title = prefix + " · " + latest["workspace_name"]
        body = str(latest.get("task_summary") or latest["session_name"])
        url = f"/?session={latest['thread_id']}&activity=1"
        tag = f"turn-{latest['thread_id']}"
    else:
        title = f"{len(batch)} 个任务已更新"
        body = f"{len(failed)} 个需要处理" if failed else "点击查看跨工作区任务结果"
        url = "/?activity=1"
        tag = "activity-batch"
    await asyncio.to_thread(
        push.send_to_all,
        title=title,
        body=body,
        url=url,
        tag=tag,
        context=f"activity batch={len(batch)}",
        mobile_only=False,
        badge_count=int(latest.get("badge_count") or 0),
    )
