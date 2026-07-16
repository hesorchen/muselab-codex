"""Safe handoff from a completed Codex turn to the next queued message."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from .attachments import CodexAttachmentService
from .queue import CodexQueueService
from .turns import CodexTurnService, TurnAlreadyActive


class CodexQueueDrainService:
    """Start at most one FIFO item when a thread is idle.

    The caller invokes ``drain`` after a successful terminal turn or an
    explicit resume. A failed start is restored at the queue head and pauses
    the queue, so the service never silently skips user input or spins.
    """

    def __init__(self, queue: CodexQueueService, turns: CodexTurnService,
                 attachments: CodexAttachmentService, activity=None):
        self.queue = queue
        self.turns = turns
        self.attachments = attachments
        self.activity = activity
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def _mark_paused(self, thread_id: str) -> None:
        if self.activity is None:
            return
        item = await self.activity.set_state(
            thread_id, "paused", summary="Queue paused: task could not start")
        if item:
            from ..turn_notifications import queue_attention_notification
            queue_attention_notification(
                item, self.activity.summary()["unread"])

    async def drain(self, thread_id: str) -> bool:
        async with self._locks[thread_id]:
            if self.turns.busy(thread_id):
                return False
            item = self.queue.take_next(thread_id)
            if item is None:
                return False
            try:
                from ..turn_notifications import clear_turn_origin, record_turn_origin
                prepared = self.attachments.prepare(
                    thread_id, str(item.get("image_ids") or ""))
                record_turn_origin(
                    thread_id, str(item.get("source_device_kind") or "unknown"))
                await self.turns.start(
                    thread_id,
                    str(item.get("text") or ""),
                    model=str(item.get("model") or ""),
                    model_provider=str(item.get("model_provider") or ""),
                    permission=str(item.get("permission") or "default"),
                    effort=str(item.get("effort") or ""),
                    service_tier=item.get("service_tier"),
                    inputs=prepared.inputs,
                    user_images=prepared.images,
                    user_docs=prepared.docs,
                    client_user_message_id=prepared.client_user_message_id,
                )
            except (TurnAlreadyActive, ValueError):
                clear_turn_origin(thread_id)
                self.queue.restore_head(thread_id, item)
                self.queue.pause(thread_id, True)
                await self._mark_paused(thread_id)
                return False
            except Exception:
                clear_turn_origin(thread_id)
                self.queue.restore_head(thread_id, item)
                self.queue.pause(thread_id, True)
                await self._mark_paused(thread_id)
                return False
            return True
