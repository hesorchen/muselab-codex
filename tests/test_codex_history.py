"""Bounded history-read degradation for large persisted threads."""

import asyncio

import pytest

from backend.codex import AppServerTimeoutError, CodexHistoryService
from backend.codex.history import _project_turns


def test_transcript_projection_preserves_user_turn_boundaries():
    turns = _project_turns((
        {"type": "userMessage", "content": []},
        {"type": "agentMessage", "text": "one"},
        {"type": "commandExecution", "id": "cmd-1"},
        {"type": "userMessage", "content": []},
        {"type": "agentMessage", "text": "two"},
    ))

    assert len(turns) == 2
    assert [item["type"] for item in turns[0]["items"]] == [
        "userMessage", "agentMessage", "commandExecution",
    ]


@pytest.mark.asyncio
async def test_history_prefers_paginated_native_thread_items():
    class Requester:
        def __init__(self):
            self.calls = []

        async def request(self, method, params, *, timeout=None):
            self.calls.append((method, params, timeout))
            if len(self.calls) == 1:
                return {
                    "data": [{"type": "userMessage", "content": []}],
                    "nextCursor": "page-2",
                }
            return {
                "data": [{"type": "agentMessage", "text": "done"}],
                "nextCursor": None,
            }

    class NativeThreads:
        def __init__(self):
            self.requester = Requester()

        async def read(self, thread_id, *, include_turns=True, timeout=None):
            assert include_turns is False
            return {"id": thread_id}

    class ForbiddenTranscripts:
        async def read(self, _thread_id):
            raise AssertionError("native item history must not read rollout JSONL")

    threads = NativeThreads()
    history = CodexHistoryService(
        threads, Runtime(), Events(), timeout=1, transcripts=ForbiddenTranscripts())

    result = await history.read("thread-1")

    assert result["turns"] == [{"items": [
        {"type": "userMessage", "content": []},
        {"type": "agentMessage", "text": "done"},
    ]}]
    assert [call[1].get("cursor") for call in threads.requester.calls] == [None, "page-2"]


class Threads:
    def __init__(self):
        self.calls = []

    async def read(self, thread_id, *, include_turns=True, timeout=None):
        self.calls.append((thread_id, include_turns, timeout))
        if include_turns:
            await asyncio.sleep(0)
            raise AppServerTimeoutError("thread/read timed out")
        return {"id": thread_id, "turns": []}


class Runtime:
    def __init__(self):
        self.restarts = 0

    async def restart(self):
        self.restarts += 1


class Events:
    def __init__(self):
        self.starts = 0

    async def start(self):
        self.starts += 1


class NoTranscripts:
    async def read(self, _thread_id):
        return None


@pytest.mark.asyncio
async def test_slow_history_restarts_runtime_once_then_serves_metadata_only():
    threads = Threads()
    runtime = Runtime()
    events = Events()
    history = CodexHistoryService(
        threads, runtime, events, timeout=0.1, transcripts=NoTranscripts())

    first, second = await asyncio.gather(
        history.read("thread-1"),
        history.read("thread-1"),
    )

    assert first == second == {"id": "thread-1", "turns": []}
    assert history.degraded("thread-1") is True
    assert runtime.restarts == 1
    assert events.starts == 1
    assert threads.calls == [
        ("thread-1", True, 0.1),
        ("thread-1", False, None),
        ("thread-1", False, None),
    ]


def test_history_timeout_must_be_positive():
    with pytest.raises(ValueError, match="must be positive"):
        CodexHistoryService(Threads(), Runtime(), Events(), timeout=0)


@pytest.mark.asyncio
async def test_full_history_reads_for_different_threads_are_serialized():
    class ConcurrentThreads:
        def __init__(self):
            self.active = 0
            self.max_active = 0

        async def read(self, thread_id, *, include_turns=True, timeout=None):
            assert include_turns is True
            assert timeout == 1.0
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            await asyncio.sleep(0.01)
            self.active -= 1
            return {"id": thread_id, "turns": []}

    threads = ConcurrentThreads()
    history = CodexHistoryService(
        threads, Runtime(), Events(), timeout=1.0, transcripts=NoTranscripts())

    first, second = await asyncio.gather(
        history.read("thread-1"),
        history.read("thread-2"),
    )

    assert first["id"] == "thread-1"
    assert second["id"] == "thread-2"
    assert threads.max_active == 1
    assert history.degraded("thread-1") is False
    assert history.degraded("thread-2") is False


@pytest.mark.asyncio
async def test_native_item_timeout_falls_back_to_transcript_without_restart():
    class ReadRequester:
        async def read_request(self, method, params, *, timeout=None):
            assert method == "thread/items/list"
            raise AppServerTimeoutError("native items timed out")

    class NativeThreads:
        requester = ReadRequester()

        async def read(self, thread_id, *, include_turns=True, timeout=None):
            assert include_turns is False
            return {"id": thread_id}

    class Transcripts:
        async def read(self, _thread_id):
            return type("Snapshot", (), {
                "items": ({"type": "agentMessage", "text": "cached"},),
                "settings": {},
            })()

    runtime = Runtime()
    history = CodexHistoryService(
        NativeThreads(), runtime, Events(), timeout=0.1, transcripts=Transcripts())

    result = await history.read("thread-1")

    assert result["turns"] == [{"items": [
        {"type": "agentMessage", "text": "cached"},
    ]}]
    assert runtime.restarts == 0
