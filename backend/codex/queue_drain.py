"""Safe handoff from a completed Codex turn to the next queued message."""

from __future__ import annotations

import asyncio
from weakref import WeakValueDictionary
from .attachments import CodexAttachmentService
from .process import (
    AppServerResponseError,
    AppServerTimeoutError,
)
from .queue import CodexQueueService
from .turns import CodexTurnService, TurnAlreadyActive
from .usage import _thread_id


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
        self._locks: WeakValueDictionary[str, asyncio.Lock] = (
            WeakValueDictionary())

    def _lock(self, thread_id: str) -> asyncio.Lock:
        lock = self._locks.get(thread_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[thread_id] = lock
        return lock

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
        clean_id = _thread_id(thread_id)
        async with self._lock(clean_id):
            try:
                await self.turns.begin_operation(
                    clean_id, ensure_loaded=False)
            except TurnAlreadyActive:
                return False
            try:
                if not await self._reconcile_starting(clean_id):
                    return False
                item = self.queue.begin_next(clean_id)
                if item is None:
                    return False
                item_id = str(item.get("id") or "")
                try:
                    prepared = self.attachments.prepare(
                        clean_id,
                        str(item.get("image_ids") or ""),
                        queue_item_id=item_id,
                        client_user_message_id=str(
                            item.get("client_user_message_id") or ""),
                    )
                except (ValueError, OSError):
                    self._rollback_and_pause(clean_id, item_id)
                    await self._mark_paused(clean_id)
                    return False
                from ..turn_notifications import clear_turn_origin, record_turn_origin
                record_turn_origin(
                    clean_id,
                    str(item.get("source_device_kind") or "unknown"))
                try:
                    await self.turns.start(
                        clean_id,
                        str(item.get("text") or ""),
                        model=str(item.get("model") or ""),
                        model_provider=str(item.get("model_provider") or ""),
                        permission=str(item.get("permission") or "default"),
                        effort=str(item.get("effort") or ""),
                        service_tier=item.get("service_tier"),
                        inputs=prepared.inputs,
                        user_images=prepared.images,
                        user_docs=prepared.docs,
                        client_user_message_id=(
                            prepared.client_user_message_id
                            or str(item.get("client_user_message_id") or "")
                        ),
                        _reserved_operation=True,
                    )
                except asyncio.CancelledError:
                    clear_turn_origin(clean_id)
                    self._pause_uncertain(clean_id)
                    raise
                except AppServerTimeoutError as exc:
                    clear_turn_origin(clean_id)
                    if exc.outcome_unknown:
                        self._pause_uncertain(clean_id)
                    else:
                        self._rollback_and_pause(clean_id, item_id)
                    await self._mark_paused(clean_id)
                    return False
                except (TurnAlreadyActive, AppServerResponseError, ValueError):
                    clear_turn_origin(clean_id)
                    self._rollback_and_pause(clean_id, item_id)
                    await self._mark_paused(clean_id)
                    return False
                except Exception:
                    # Process/protocol failures after entering turn/start do
                    # not prove rejection. Keep the starting row for stable-id
                    # transcript reconciliation rather than duplicate it.
                    clear_turn_origin(clean_id)
                    self._pause_uncertain(clean_id)
                    await self._mark_paused(clean_id)
                    return False
                try:
                    self.attachments.ack_queue(
                        clean_id, item_id,
                        str(item.get("image_ids") or ""),
                    )
                    if not self.queue.ack_started(clean_id, item_id):
                        raise RuntimeError("starting queue row disappeared")
                except Exception:
                    # Native success already committed. Leave the durable row
                    # for canonical reconciliation; never roll it back.
                    self._pause_uncertain(clean_id)
                    await self._mark_paused(clean_id)
                    return False
                return True
            finally:
                await self.turns.end_operation(clean_id)

    def _rollback_and_pause(self, thread_id: str, item_id: str) -> None:
        self.queue.rollback_start(thread_id, item_id)
        self.queue.pause(thread_id, True)

    def _pause_uncertain(self, thread_id: str) -> None:
        try:
            self.queue.pause(thread_id, True)
        except OSError:
            # The starting row alone is a replay fence even if the secondary
            # paused bit cannot be flushed.
            pass

    async def _reconcile_starting(self, thread_id: str) -> bool:
        starting = self.queue.starting_items(thread_id)
        if not starting:
            return True
        try:
            thread = await self.turns.threads.read(
                thread_id, include_turns=True, timeout=5.0)
        except Exception:
            self._pause_uncertain(thread_id)
            await self._mark_paused(thread_id)
            return False
        for item in starting:
            client_id = str(item.get("client_user_message_id") or "")
            found, terminal = _client_turn_state(thread, client_id)
            item_id = str(item.get("id") or "")
            if found:
                self.attachments.ack_queue(
                    thread_id, item_id,
                    str(item.get("image_ids") or ""),
                )
                if not self.queue.ack_started(thread_id, item_id):
                    raise RuntimeError("starting queue row disappeared")
                if not terminal:
                    self.queue.pause(thread_id, True)
                    return False
            else:
                if not self.queue.rollback_start(thread_id, item_id):
                    raise RuntimeError("starting queue row disappeared")
        return True


def _client_turn_state(thread: object, client_id: str) -> tuple[bool, bool]:
    if not client_id or not isinstance(thread, dict):
        return False, False
    turns = thread.get("turns")
    if not isinstance(turns, list):
        return False, False
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        items = turn.get("items")
        if not isinstance(items, list):
            continue
        if any(
            isinstance(item, dict)
            and item.get("type") == "userMessage"
            and item.get("clientId") == client_id
            for item in items
        ):
            return True, str(turn.get("status") or "") in {
                "completed", "failed", "interrupted", "cancelled",
            }
    return False, False
