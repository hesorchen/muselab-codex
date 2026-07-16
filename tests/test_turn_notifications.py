"""Cross-device turn notification regression coverage."""

# ruff: noqa: E402 -- backend.settings validates env at import time.

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("MUSELAB_TOKEN", "test-token-1234567890abcdef-secure-min-32")
os.environ.setdefault("MUSELAB_ROOT", "/tmp/muselab-codex-notification-tests")
Path(os.environ["MUSELAB_ROOT"]).mkdir(parents=True, exist_ok=True)

from backend import turn_notifications


def setup_function():
    turn_notifications._turn_origins.clear()
    turn_notifications._pending_notifications.clear()
    task = turn_notifications._notification_flush_task
    if task is not None and not task.done():
        task.cancel()
    turn_notifications._notification_flush_task = None


class Threads:
    async def read(self, thread_id, *, include_turns):
        assert include_turns is False
        return {"id": thread_id, "name": "Daily report"}


class Activity:
    def latest_thread(self, thread_id):
        return {
            "thread_id": thread_id,
            "workspace_name": "Reports",
            "session_name": "Daily report",
        }

    def summary(self):
        return {"unread": 2}


@pytest.mark.asyncio
async def test_completed_turn_queues_cross_device_push_and_preserves_follow_up(
    monkeypatch,
):
    queued = []
    followed = []
    monkeypatch.setattr(turn_notifications, "_queue_notification", queued.append)

    async def after_turn(thread_id, status):
        followed.append((thread_id, status))

    callback = turn_notifications.completed_turn_callback(
        Threads(), after_turn, activity=Activity())
    await callback("thread-1", "completed")

    assert followed == [("thread-1", "completed")]
    assert queued == [{
        "thread_id": "thread-1",
        "workspace_name": "Reports",
        "session_name": "Daily report",
        "status": "completed",
        "badge_count": 2,
    }]


@pytest.mark.asyncio
async def test_desktop_origin_uses_same_per_device_push_path(monkeypatch):
    queued = []
    monkeypatch.setattr(turn_notifications, "_queue_notification", queued.append)
    turn_notifications.record_turn_origin("thread-1", "desktop")

    await turn_notifications._notify_completed_turn(
        Threads(), "thread-1", activity=Activity(),
        item=Activity().latest_thread("thread-1"))

    assert queued[0]["thread_id"] == "thread-1"
    assert "thread-1" not in turn_notifications._turn_origins


@pytest.mark.asyncio
async def test_callback_claims_origin_before_queue_successor_records_its_own(monkeypatch):
    monkeypatch.setattr(turn_notifications, "_queue_notification", lambda _item: None)
    turn_notifications.record_turn_origin("thread-1", "desktop")

    async def start_successor(thread_id, _status):
        turn_notifications.record_turn_origin(thread_id, "mobile")

    callback = turn_notifications.completed_turn_callback(
        Threads(), start_successor, activity=Activity())
    await callback("thread-1", "completed")

    assert turn_notifications._turn_origins["thread-1"] == "mobile"


@pytest.mark.asyncio
async def test_flush_groups_multiple_workspaces_and_updates_dock_badge(monkeypatch):
    sent = []

    async def no_sleep(_seconds):
        return None

    async def run_inline(func, **kwargs):
        return func(**kwargs)

    monkeypatch.setattr(turn_notifications.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(turn_notifications.asyncio, "to_thread", run_inline)
    monkeypatch.setattr(
        turn_notifications.push,
        "send_to_all",
        lambda **kwargs: sent.append(kwargs) or {"sent": 2},
    )
    turn_notifications._pending_notifications.extend([
        {"thread_id": "a", "workspace_name": "A", "session_name": "one",
         "status": "completed", "badge_count": 1},
        {"thread_id": "b", "workspace_name": "B", "session_name": "two",
         "status": "failed", "badge_count": 2},
    ])

    await turn_notifications._flush_notifications()

    assert sent == [{
        "title": "2 个任务已更新",
        "body": "1 个需要处理",
        "url": "/?activity=1",
        "tag": "activity-batch",
        "context": "activity batch=2",
        "mobile_only": False,
        "badge_count": 2,
    }]


@pytest.mark.asyncio
async def test_flush_deduplicates_state_changes_for_same_thread(monkeypatch):
    sent = []

    async def no_sleep(_seconds):
        return None

    async def run_inline(func, **kwargs):
        return func(**kwargs)

    monkeypatch.setattr(turn_notifications.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(turn_notifications.asyncio, "to_thread", run_inline)
    monkeypatch.setattr(
        turn_notifications.push,
        "send_to_all",
        lambda **kwargs: sent.append(kwargs) or {"sent": 1},
    )
    turn_notifications._pending_notifications.extend([
        {"thread_id": "a", "workspace_name": "A", "session_name": "one",
         "status": "waiting_approval", "badge_count": 1},
        {"thread_id": "a", "workspace_name": "A", "session_name": "one",
         "status": "completed", "badge_count": 1},
    ])

    await turn_notifications._flush_notifications()

    assert len(sent) == 1
    assert sent[0]["title"] == "任务已完成 · A"
    assert sent[0]["mobile_only"] is False
