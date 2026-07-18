"""Concurrency tests for the single-reader notification fan-out."""

import asyncio

import pytest

from backend.codex import CodexEventRouter
from backend.codex.event_router import EventSubscriptionResyncRequired
from backend.codex.process import AppServerProcessError


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


@pytest.mark.asyncio
async def test_slow_event_subscription_isolated_with_resync():
    server = NotificationServer()
    router = CodexEventRouter(
        Runtime(server), subscription_max_events=1,
        subscription_max_bytes=1024)
    subscription = await router.subscribe("thread-1")
    await server.notifications.put({
        "method": "item/agentMessage/delta",
        "params": {"threadId": "thread-1", "delta": "one"},
    })
    await server.notifications.put({
        "method": "item/agentMessage/delta",
        "params": {"threadId": "thread-1", "delta": "two"},
    })
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    with pytest.raises(EventSubscriptionResyncRequired):
        await subscription.next()
    await router.close()


@pytest.mark.asyncio
async def test_runtime_generation_switch_closes_old_subscription():
    old_server = NotificationServer()

    class GenerationalRuntime(Runtime):
        generation = 1

    runtime = GenerationalRuntime(old_server)
    router = CodexEventRouter(runtime)
    old = await router.subscribe("thread-1")
    runtime.server = NotificationServer()
    runtime.generation = 2
    new = await router.subscribe("thread-1")

    with pytest.raises(AppServerProcessError):
        await old.next()
    await runtime.server.notifications.put({
        "method": "item/agentMessage/delta",
        "params": {"threadId": "thread-1", "delta": "new"},
    })
    assert (await asyncio.wait_for(new.next(), 1))["params"]["delta"] == "new"
    await router.close()
