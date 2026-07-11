"""Native app-server context compaction."""

from __future__ import annotations

import asyncio
from typing import Any

from .event_router import CodexEventRouter
from .process import AppServerProtocolError
from .runtime import CodexRuntime
from .turns import CodexTurnService
from .usage import CodexUsageService


class CodexCompactService:
    """Run one native compaction and wait for its own turn to finish."""

    def __init__(
        self,
        runtime: CodexRuntime,
        events: CodexEventRouter,
        turns: CodexTurnService,
        usage: CodexUsageService,
        *,
        timeout: float = 600.0,
    ):
        if timeout <= 0:
            raise ValueError("compact timeout must be positive")
        self.runtime = runtime
        self.events = events
        self.turns = turns
        self.usage = usage
        self.timeout = timeout

    async def compact(self, thread_id: str, *, model: str = "") -> dict[str, Any]:
        clean_id = thread_id.strip()
        if not clean_id:
            raise ValueError("thread id cannot be empty")
        subscription = None
        await self.turns.begin_operation(clean_id, model=model)
        try:
            subscription = await self.events.subscribe(clean_id)
            result = await self.runtime.request(
                "thread/compact/start",
                {"threadId": clean_id},
            )
            if not isinstance(result, dict):
                raise AppServerProtocolError(
                    "thread/compact/start returned an invalid result")
            compact_turn_id: str | None = None
            saw_compaction_item = False
            async with asyncio.timeout(self.timeout):
                while True:
                    notification = await subscription.next()
                    method = notification.get("method")
                    params = notification.get("params")
                    if not isinstance(method, str) or not isinstance(params, dict):
                        continue
                    if method == "turn/started":
                        turn = params.get("turn")
                        if isinstance(turn, dict) and isinstance(turn.get("id"), str):
                            compact_turn_id = turn["id"]
                    elif method == "thread/tokenUsage/updated":
                        token_usage = params.get("tokenUsage")
                        if isinstance(token_usage, dict):
                            self.usage.update(clean_id, token_usage)
                    elif method == "item/completed":
                        item = params.get("item")
                        if isinstance(item, dict) and item.get("type") == "contextCompaction":
                            saw_compaction_item = True
                    elif method == "thread/compacted":
                        break
                    elif method == "turn/completed":
                        turn = params.get("turn")
                        if not isinstance(turn, dict):
                            continue
                        turn_id = turn.get("id")
                        if compact_turn_id is not None and turn_id != compact_turn_id:
                            continue
                        if not saw_compaction_item and compact_turn_id is None:
                            continue
                        status = turn.get("status")
                        if status != "completed":
                            raise AppServerProtocolError(
                                f"context compaction finished with status {status}")
                        break
            return {
                "ok": True,
                "session_usage": self.usage.get(clean_id, model=model),
            }
        finally:
            if subscription is not None:
                await subscription.close()
            await self.turns.end_operation(clean_id)
