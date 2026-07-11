"""Native context-compaction lifecycle tests."""

import asyncio

import pytest

from backend.codex.compact import CodexCompactService


class Subscription:
    def __init__(self, notifications):
        self.notifications = asyncio.Queue()
        for notification in notifications:
            self.notifications.put_nowait(notification)
        self.closed = False

    async def next(self):
        return await self.notifications.get()

    async def close(self):
        self.closed = True


class Events:
    def __init__(self, notifications):
        self.subscription = Subscription(notifications)

    async def subscribe(self, thread_id):
        assert thread_id == "thread-1"
        return self.subscription


class Runtime:
    def __init__(self):
        self.calls = []

    async def request(self, method, params):
        self.calls.append((method, params))
        return {}


class Turns:
    def __init__(self):
        self.calls = []

    async def begin_operation(self, thread_id, *, model=""):
        self.calls.append(("begin", thread_id, model))

    async def end_operation(self, thread_id):
        self.calls.append(("end", thread_id))


class Usage:
    def __init__(self):
        self.raw = None

    def update(self, thread_id, raw):
        assert thread_id == "thread-1"
        self.raw = raw
        return {"context_used": 4}

    def get(self, thread_id, *, model=""):
        assert thread_id == "thread-1"
        return {"context_used": 4, "model": model}


def _event(method, **params):
    return {"method": method, "params": {"threadId": "thread-1", **params}}


@pytest.mark.asyncio
async def test_compact_waits_for_its_turn_and_updates_usage():
    token_usage = {"last": {"totalTokens": 4}}
    events = Events([
        _event("turn/started", turn={"id": "compact-1", "status": "inProgress"}),
        _event(
            "thread/tokenUsage/updated",
            turnId="compact-1",
            tokenUsage=token_usage,
        ),
        _event(
            "item/completed",
            turnId="compact-1",
            item={"id": "item-1", "type": "contextCompaction"},
        ),
        _event(
            "turn/completed",
            turn={"id": "compact-1", "status": "completed", "items": []},
        ),
    ])
    runtime = Runtime()
    turns = Turns()
    usage = Usage()
    service = CodexCompactService(runtime, events, turns, usage, timeout=1)

    result = await service.compact("thread-1", model="gpt-test")

    assert result == {
        "ok": True,
        "session_usage": {"context_used": 4, "model": "gpt-test"},
    }
    assert runtime.calls == [("thread/compact/start", {"threadId": "thread-1"})]
    assert turns.calls == [
        ("begin", "thread-1", "gpt-test"),
        ("end", "thread-1"),
    ]
    assert usage.raw == token_usage
    assert events.subscription.closed is True


@pytest.mark.asyncio
async def test_compact_timeout_releases_thread_reservation():
    events = Events([])
    turns = Turns()
    service = CodexCompactService(Runtime(), events, turns, Usage(), timeout=0.01)

    with pytest.raises(TimeoutError):
        await service.compact("thread-1")

    assert turns.calls == [
        ("begin", "thread-1", ""),
        ("end", "thread-1"),
    ]
    assert events.subscription.closed is True
