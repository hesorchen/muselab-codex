"""Best-effort Web Push delivery for completed Codex turns."""

from __future__ import annotations

import asyncio
import sys
from typing import Any, Awaitable, Callable

from . import presence, push


def completed_turn_callback(
    threads: Any,
    after_turn: Callable[[str, str], Awaitable[None]] | None = None,
) -> Callable[[str, str], Awaitable[None]]:
    """Compose queue draining with one presence-gated completion push."""

    async def callback(thread_id: str, status: str) -> None:
        tasks = [_notify_completed_turn(threads, thread_id)]
        if after_turn is not None:
            tasks.append(after_turn(thread_id, status))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                sys.stderr.write(
                    f"[push] turn-done follow-up failed sid={thread_id}: {result}\n")

    return callback


async def _notify_completed_turn(threads: Any, thread_id: str) -> None:
    if presence.recently_active():
        age = presence.last_seen_age()
        age_text = f"{age:.0f}s" if age is not None else "?"
        sys.stderr.write(
            f"[push] turn-done skipped (presence age={age_text}) "
            f"sid={thread_id}\n")
        return

    thread_name = ""
    try:
        thread = await threads.read(thread_id, include_turns=False)
        candidate = thread.get("name") if isinstance(thread, dict) else None
        if isinstance(candidate, str) and candidate.strip():
            thread_name = candidate.strip()
    except Exception:
        # A notification must not fail merely because thread lookup did.
        pass

    scheduled_prefix = "[Scheduled]"
    if thread_name.startswith(scheduled_prefix):
        title = "定时任务已完成"
        body = thread_name[len(scheduled_prefix):].strip() or "点按查看任务结果"
    else:
        title = "Muse 已回复"
        body = "点按查看完整回复"

    await asyncio.to_thread(
        push.send_to_all,
        title=title,
        body=body,
        url=f"/?session={thread_id}",
        tag=f"turn-{thread_id}",
        context=f"turn-done {thread_id[:8]}",
        mobile_only=True,
    )
