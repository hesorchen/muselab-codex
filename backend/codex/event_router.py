"""Single-reader fan-out for app-server notifications."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from contextlib import suppress
from typing import Any

from .process import AppServerError, AppServerProcessError, CodexAppServer
from .runtime import CodexRuntime


_CLOSED = object()
_RESYNC = object()
_DEFAULT_MAX_EVENTS = 1024
_DEFAULT_MAX_BYTES = 4 * 1024 * 1024


class EventSubscriptionResyncRequired(AppServerError):
    """A consumer fell behind its bounded app-server notification buffer."""


def _notification_size(notification: dict[str, Any]) -> int:
    try:
        return len(json.dumps(
            notification, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8"))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_BYTES + 1


class EventSubscription:
    """A thread-scoped or connection-scoped view over app-server events."""

    def __init__(
        self,
        owner: "CodexEventRouter",
        thread_id: str | None,
        generation: int,
        *,
        max_events: int,
        max_bytes: int,
    ):
        self._owner = owner
        self.thread_id = thread_id
        self.generation = generation
        self._queue: asyncio.Queue = asyncio.Queue()
        self._pending_bytes = 0
        self._max_events = max_events
        self._max_bytes = max_bytes
        self._closed = False
        self._accepting = True

    async def next(self) -> dict[str, Any]:
        item = await self._queue.get()
        if isinstance(item, tuple) and len(item) == 2:
            notification, size = item
            self._pending_bytes = max(0, self._pending_bytes - int(size))
            return notification
        if item is _RESYNC:
            raise EventSubscriptionResyncRequired(
                "Codex event subscriber fell behind; resync required")
        if item is _CLOSED:
            self._queue.put_nowait(_CLOSED)
            raise AppServerProcessError("Codex event subscription closed")
        raise AppServerProcessError("Codex event subscription is invalid")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._accepting = False
        self._owner._discard(self)
        self._queue.put_nowait(_CLOSED)

    def _publish(self, notification: dict[str, Any]) -> None:
        if self._closed or not self._accepting:
            return
        size = _notification_size(notification)
        if (self._queue.qsize() >= self._max_events
                or self._pending_bytes + size > self._max_bytes):
            self._accepting = False
            self._owner._discard(self)
            while not self._queue.empty():
                with suppress(asyncio.QueueEmpty):
                    self._queue.get_nowait()
            self._pending_bytes = 0
            self._queue.put_nowait(_RESYNC)
            return
        self._pending_bytes += size
        self._queue.put_nowait((notification, size))

    def _close_from_owner(self) -> None:
        if not self._closed:
            self._closed = True
            self._accepting = False
            self._queue.put_nowait(_CLOSED)


class CodexEventRouter:
    """Own the only reader of ``next_notification`` and fan out by thread.

    Reading the app-server queue from individual HTTP streams is unsafe: two
    concurrent sessions would consume each other's notifications. This router
    keeps the transport single-reader while allowing one subscription per
    active turn.
    """

    def __init__(
        self,
        runtime: CodexRuntime,
        *,
        subscription_max_events: int = _DEFAULT_MAX_EVENTS,
        subscription_max_bytes: int = _DEFAULT_MAX_BYTES,
    ):
        if subscription_max_events < 1 or subscription_max_bytes < 1:
            raise ValueError("event subscription limits must be positive")
        self.runtime = runtime
        self.subscription_max_events = subscription_max_events
        self.subscription_max_bytes = subscription_max_bytes
        self._server: CodexAppServer | None = None
        self._generation = 0
        self._pump_task: asyncio.Task | None = None
        self._subscriptions: dict[str | None, set[EventSubscription]] = defaultdict(set)
        self._lock = asyncio.Lock()
        self._closing = False

    async def start(self) -> None:
        async with self._lock:
            if self._closing:
                raise AppServerProcessError("Codex event router is closing")
            server = await self.runtime.ensure_ready()
            generation = int(getattr(self.runtime, "generation", 0))
            if (self._server is server and self._pump_task is not None
                    and self._generation == generation
                    and not self._pump_task.done()):
                return
            await self._stop_pump_locked()
            self._close_subscriptions()
            self._server = server
            self._generation = generation
            self._pump_task = asyncio.create_task(
                self._pump(server, generation), name="codex-app-server-events")

    async def subscribe(self, thread_id: str) -> EventSubscription:
        clean_id = thread_id.strip()
        if not clean_id:
            raise ValueError("thread id cannot be empty")
        await self.start()
        subscription = EventSubscription(
            self, clean_id, self._generation,
            max_events=self.subscription_max_events,
            max_bytes=self.subscription_max_bytes,
        )
        self._subscriptions[clean_id].add(subscription)
        return subscription

    async def subscribe_connection(self) -> EventSubscription:
        """Subscribe to connection-scoped notifications such as command output."""
        await self.start()
        subscription = EventSubscription(
            self, None, self._generation,
            max_events=self.subscription_max_events,
            max_bytes=self.subscription_max_bytes,
        )
        self._subscriptions[None].add(subscription)
        return subscription

    async def close(self) -> None:
        async with self._lock:
            self._closing = True
            await self._stop_pump_locked()
            self._close_subscriptions()

    async def _pump(self, server: CodexAppServer, generation: int) -> None:
        try:
            while True:
                notification = await server.next_notification()
                runtime_server = getattr(self.runtime, "server", server)
                runtime_generation = int(
                    getattr(self.runtime, "generation", generation))
                if (runtime_server is not server
                        or runtime_generation != generation):
                    raise AppServerProcessError(
                        "stale Codex runtime generation retired")
                thread_id = _notification_thread_id(notification)
                for subscription in tuple(self._subscriptions.get(thread_id, ())):
                    subscription._publish(notification)
                # Connection-scoped consumers must also see thread events so
                # they can own one stream without competing for the transport.
                if thread_id is not None:
                    for subscription in tuple(self._subscriptions.get(None, ())):
                        subscription._publish(notification)
        except asyncio.CancelledError:
            raise
        except AppServerError:
            self._close_subscriptions()

    async def _stop_pump_locked(self) -> None:
        task = self._pump_task
        self._pump_task = None
        self._server = None
        self._generation = 0
        if task is not None and not task.done():
            task.cancel()
        if task is not None:
            with suppress(asyncio.CancelledError):
                await task

    def _discard(self, subscription: EventSubscription) -> None:
        subscriptions = self._subscriptions.get(subscription.thread_id)
        if subscriptions is None:
            return
        subscriptions.discard(subscription)
        if not subscriptions:
            self._subscriptions.pop(subscription.thread_id, None)

    def _close_subscriptions(self) -> None:
        subscriptions = [subscription
                         for group in self._subscriptions.values()
                         for subscription in group]
        self._subscriptions.clear()
        for subscription in subscriptions:
            subscription._close_from_owner()


def _notification_thread_id(notification: dict[str, Any]) -> str | None:
    params = notification.get("params")
    if not isinstance(params, dict):
        return None
    thread_id = params.get("threadId")
    if isinstance(thread_id, str) and thread_id:
        return thread_id
    thread = params.get("thread")
    if isinstance(thread, dict):
        thread_id = thread.get("id")
        if isinstance(thread_id, str) and thread_id:
            return thread_id
    return None
