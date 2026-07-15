"""Queue-to-turn handoff must be FIFO, bounded, and failure-safe."""

import pytest

from backend.codex.queue import CodexQueueService
from backend.codex.queue_drain import CodexQueueDrainService
from backend.codex.turns import TurnAlreadyActive


class Attachments:
    def prepare(self, _thread_id, _image_ids):
        return type("Prepared", (), {
            "inputs": [], "images": [], "docs": [],
            "client_user_message_id": None,
        })()


class Turns:
    def __init__(self, *, fail=False, busy=False):
        self.fail = fail
        self.is_busy = busy
        self.started = []

    def busy(self, _thread_id):
        return self.is_busy

    async def start(self, thread_id, prompt, **kwargs):
        if self.fail:
            raise TurnAlreadyActive("previous turn still running")
        self.started.append((thread_id, prompt, kwargs))


@pytest.mark.asyncio
async def test_drain_starts_fifo_head_once_when_thread_is_idle():
    queue = CodexQueueService()
    queue.enqueue(
        "thread-1", "first", permission="plan",
        model="gpt-test", model_provider="openai", effort="high",
        source_device_kind="desktop")
    queue.enqueue("thread-1", "second")
    turns = Turns()
    drain = CodexQueueDrainService(queue, turns, Attachments())

    assert await drain.drain("thread-1") is True
    assert len(turns.started) == 1
    thread_id, prompt, kwargs = turns.started[0]
    assert (thread_id, prompt) == ("thread-1", "first")
    assert kwargs["permission"] == "plan"
    assert kwargs["model"] == "gpt-test"
    assert kwargs["model_provider"] == "openai"
    assert kwargs["effort"] == "high"
    from backend import turn_notifications
    assert turn_notifications._turn_origins["thread-1"] == "desktop"
    turn_notifications.clear_turn_origin("thread-1")
    assert [item["text"] for item in queue.get("thread-1")["items"]] == ["second"]


@pytest.mark.asyncio
async def test_failed_drain_restores_head_and_pauses_queue():
    queue = CodexQueueService()
    queue.enqueue("thread-1", "keep me")
    drain = CodexQueueDrainService(queue, Turns(fail=True), Attachments())

    assert await drain.drain("thread-1") is False
    state = queue.get("thread-1")
    assert state["paused"] is True
    assert [item["text"] for item in state["items"]] == ["keep me"]


@pytest.mark.asyncio
async def test_busy_or_paused_queue_does_not_consume_item():
    queue = CodexQueueService()
    queue.enqueue("thread-1", "wait")
    drain = CodexQueueDrainService(queue, Turns(busy=True), Attachments())
    assert await drain.drain("thread-1") is False
    assert queue.get("thread-1")["items"]
    queue.pause("thread-1", True)
    assert await CodexQueueDrainService(
        queue, Turns(), Attachments()).drain("thread-1") is False
    assert queue.get("thread-1")["items"]


def test_queue_survives_service_restart_and_persists_every_mutation(tmp_path):
    state_dir = tmp_path / "queues"
    first = CodexQueueService(state_dir)
    queued = first.enqueue(
        "thread-1", "survive restart", image_ids="a" * 32,
        permission="plan", model="gpt-test", effort="high",
        source_device_kind="mobile",
    )
    first.pause("thread-1", True)

    restored = CodexQueueService(state_dir)
    state = restored.get("thread-1")
    assert state["paused"] is True
    assert state["items"] == [queued]

    restored.pause("thread-1", False)
    assert restored.take_next("thread-1") == queued
    assert not (state_dir / "thread-1.json").exists()
    assert CodexQueueService(state_dir).get("thread-1") == {
        "items": [], "paused": False,
    }
