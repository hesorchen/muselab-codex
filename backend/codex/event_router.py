"""Single-reader fan-out for app-server notifications."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from contextlib import suppress
from typing import Any

from .process import AppServerError, AppServerProcessError, CodexAppServer
from .runtime import CodexRuntime


_CLOSED = object()


class EventSubscription:
    """A thread-scoped or connection-scoped view over app-server events."""

    def __init__(self, owner: "CodexEventRouter", thread_id: str | None):
        self._owner = owner
        self.thread_id = thread_id
        self._queue: asyncio.Queue = asyncio.Queue()
        self._closed = False

    async def next(self) -> dict[str, Any]:
        item = await self._queue.get()
        if item is _CLOSED:
            self._queue.put_nowait(_CLOSED)
            raise AppServerProcessError("Codex event subscription closed")
        return item

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._owner._discard(self)
        self._queue.put_nowait(_CLOSED)

    def _publish(self, notification: dict[str, Any]) -> None:
        if not self._closed:
            self._queue.put_nowait(notification)

    def _close_from_owner(self) -> None:
        if not self._closed:
            self._closed = True
            self._queue.put_nowait(_CLOSED)


class CodexEventRouter:
    """Own the only reader of ``next_notification`` and fan out by thread.

    Reading the app-server queue from individual HTTP streams is unsafe: two
    concurrent sessions would consume each other's notifications. This router
    keeps the transport single-reader while allowing one subscription per
    active turn.
    """

    def __init__(self, runtime: CodexRuntime):
        self.runtime = runtime
        self._server: CodexAppServer | None = None
        self._pump_task: asyncio.Task | None = None
        self._subscriptions: dict[str | None, set[EventSubscription]] = defaultdict(set)
        self._lock = asyncio.Lock()
        self._closing = False

    async def start(self) -> None:
        async with self._lock:
            if self._closing:
                raise AppServerProcessError("Codex event router is closing")
            server = await self.runtime.ensure_ready()
            if (self._server is server and self._pump_task is not None
                    and not self._pump_task.done()):
                return
            await self._stop_pump_locked()
            self._server = server
            self._pump_task = asyncio.create_task(
                self._pump(server), name="codex-app-server-events")

    async def subscribe(self, thread_id: str) -> EventSubscription:
        clean_id = thread_id.strip()
        if not clean_id:
            raise ValueError("thread id cannot be empty")
        await self.start()
        subscription = EventSubscription(self, clean_id)
        self._subscriptions[clean_id].add(subscription)
        return subscription

    async def subscribe_connection(self) -> EventSubscription:
        """Subscribe to connection-scoped notifications such as command output."""
        await self.start()
        subscription = EventSubscription(self, None)
        self._subscriptions[None].add(subscription)
        return subscription

    async def close(self) -> None:
        async with self._lock:
            self._closing = True
            await self._stop_pump_locked()
            self._close_subscriptions()

    async def _pump(self, server: CodexAppServer) -> None:
        try:
            while True:
                notification = await server.next_notification()
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
