"""Concurrency tests for the single-reader notification fan-out."""

import asyncio

import pytest

from backend.codex import CodexEventRouter


class NotificationServer:
    def __init__(self):
        self.notifications = asyncio.Queue()

    async def next_notification(self):
        return await self.notifications.get()


class Runtime:
    def __init__(self, server):
        self.server = server

    async def ensure_ready(self):
        return self.server


@pytest.mark.asyncio
async def test_notifications_are_fanned_out_without_cross_thread_consumption():
    server = NotificationServer()
    router = CodexEventRouter(Runtime(server))
    first = await router.subscribe("thread-1")
    second = await router.subscribe("thread-2")
    try:
        await server.notifications.put({
            "method": "item/agentMessage/delta",
            "params": {"threadId": "thread-2", "delta": "two"},
        })
        await server.notifications.put({
            "method": "item/agentMessage/delta",
            "params": {"threadId": "thread-1", "delta": "one"},
        })

        assert (await asyncio.wait_for(first.next(), 1))["params"]["delta"] == "one"
        assert (await asyncio.wait_for(second.next(), 1))["params"]["delta"] == "two"
    finally:
        await first.close()
        await second.close()
        await router.close()


@pytest.mark.asyncio
async def test_multiple_subscribers_receive_the_same_thread_event():
    server = NotificationServer()
    router = CodexEventRouter(Runtime(server))
    first = await router.subscribe("thread-1")
    second = await router.subscribe("thread-1")
    try:
        notification = {
            "method": "turn/completed",
            "params": {"threadId": "thread-1", "turn": {"id": "turn-1"}},
        }
        await server.notifications.put(notification)
        assert await asyncio.wait_for(first.next(), 1) == notification
        assert await asyncio.wait_for(second.next(), 1) == notification
    finally:
        await router.close()
