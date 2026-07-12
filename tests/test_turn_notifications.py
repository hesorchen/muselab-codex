"""Completed-turn Web Push regression coverage."""

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


class Threads:
    async def read(self, thread_id, *, include_turns):
        assert thread_id == "thread-1"
        assert include_turns is False
        return {"id": thread_id, "name": "Daily report"}


@pytest.mark.asyncio
async def test_completed_turn_sends_push_and_preserves_follow_up(monkeypatch):
    sent = []
    followed = []
    monkeypatch.setattr(turn_notifications.presence, "recently_active", lambda: False)

    async def run_inline(func, **kwargs):
        return func(**kwargs)

    monkeypatch.setattr(turn_notifications.asyncio, "to_thread", run_inline)
    monkeypatch.setattr(
        turn_notifications.push,
        "send_to_all",
        lambda **kwargs: sent.append(kwargs) or {"sent": 1},
    )

    async def after_turn(thread_id, status):
        followed.append((thread_id, status))

    callback = turn_notifications.completed_turn_callback(Threads(), after_turn)
    await callback("thread-1", "completed")

    assert followed == [("thread-1", "completed")]
    assert sent == [{
        "title": "Muse 已回复",
        "body": "点按查看完整回复",
        "url": "/?session=thread-1",
        "tag": "turn-thread-1",
        "context": "turn-done thread-1",
        "mobile_only": True,
    }]


@pytest.mark.asyncio
async def test_completed_turn_skips_push_while_user_is_present(monkeypatch):
    monkeypatch.setattr(turn_notifications.presence, "recently_active", lambda: True)
    monkeypatch.setattr(turn_notifications.presence, "last_seen_age", lambda: 2.0)

    def unexpected_push(**_kwargs):
        raise AssertionError("push must be suppressed while a device is active")

    monkeypatch.setattr(turn_notifications.push, "send_to_all", unexpected_push)
    callback = turn_notifications.completed_turn_callback(Threads())
    await callback("thread-1", "completed")


@pytest.mark.asyncio
async def test_desktop_origin_skips_phone_push(monkeypatch):
    def unexpected_presence_check():
        raise AssertionError("desktop origin must short-circuit before presence")

    def unexpected_push(**_kwargs):
        raise AssertionError("desktop-origin turn must not push to phone")

    monkeypatch.setattr(
        turn_notifications.presence, "recently_active", unexpected_presence_check)
    monkeypatch.setattr(turn_notifications.push, "send_to_all", unexpected_push)
    turn_notifications.record_turn_origin("thread-1", "desktop")

    await turn_notifications._notify_completed_turn(Threads(), "thread-1")

    assert "thread-1" not in turn_notifications._turn_origins


@pytest.mark.asyncio
async def test_callback_claims_origin_before_queue_successor_records_its_own(monkeypatch):
    def unexpected_push(**_kwargs):
        raise AssertionError("desktop-origin turn must not push to phone")

    monkeypatch.setattr(turn_notifications.push, "send_to_all", unexpected_push)
    turn_notifications.record_turn_origin("thread-1", "desktop")

    async def start_successor(thread_id, _status):
        turn_notifications.record_turn_origin(thread_id, "mobile")

    callback = turn_notifications.completed_turn_callback(Threads(), start_successor)
    await callback("thread-1", "completed")

    assert turn_notifications._turn_origins["thread-1"] == "mobile"


@pytest.mark.asyncio
async def test_mobile_origin_keeps_phone_push_path(monkeypatch):
    sent = []
    monkeypatch.setattr(turn_notifications.presence, "recently_active", lambda: False)

    async def run_inline(func, **kwargs):
        return func(**kwargs)

    monkeypatch.setattr(turn_notifications.asyncio, "to_thread", run_inline)
    monkeypatch.setattr(
        turn_notifications.push,
        "send_to_all",
        lambda **kwargs: sent.append(kwargs) or {"sent": 1},
    )
    turn_notifications.record_turn_origin("thread-1", "mobile")

    await turn_notifications._notify_completed_turn(Threads(), "thread-1")

    assert len(sent) == 1


@pytest.mark.asyncio
async def test_scheduled_turn_uses_task_name_without_reply_content(monkeypatch):
    sent = []
    monkeypatch.setattr(turn_notifications.presence, "recently_active", lambda: False)

    async def run_inline(func, **kwargs):
        return func(**kwargs)

    class ScheduledThreads:
        async def read(self, _thread_id, *, include_turns):
            assert include_turns is False
            return {"name": "[Scheduled] Morning brief"}

    monkeypatch.setattr(turn_notifications.asyncio, "to_thread", run_inline)
    monkeypatch.setattr(
        turn_notifications.push,
        "send_to_all",
        lambda **kwargs: sent.append(kwargs) or {"sent": 1},
    )

    callback = turn_notifications.completed_turn_callback(ScheduledThreads())
    await callback("scheduled-1", "completed")

    assert sent[0]["title"] == "定时任务已完成"
    assert sent[0]["body"] == "Morning brief"
