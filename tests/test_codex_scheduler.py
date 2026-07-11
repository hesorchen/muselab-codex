"""Native scheduler metadata and headless-turn dispatch."""

# ruff: noqa: E402 -- backend.settings validates env at import time.

import asyncio
import os
from pathlib import Path

import pytest

os.environ.setdefault("MUSELAB_TOKEN", "test-token-1234567890abcdef-secure-min-32")
os.environ.setdefault("MUSELAB_ROOT", "/tmp/muselab-codex-scheduler-tests")
Path(os.environ["MUSELAB_ROOT"]).mkdir(parents=True, exist_ok=True)

from backend.codex.scheduler import CodexScheduler, _next_run


class Threads:
    async def start(self, *, name, model=None):
        return {"id": "thread-scheduled", "name": name, "model": model}


class Turns:
    async def start(self, thread_id, prompt, **kwargs):
        stream = type("Stream", (), {
            "done": True,
            "status": "completed",
            "events": [{"event": "text", "data": {"text": f"done: {prompt}"}}],
        })()
        return stream

    async def interrupt(self, _thread_id):
        return True


@pytest.mark.asyncio
async def test_scheduler_persists_task_and_records_native_headless_run(tmp_path):
    scheduler = CodexScheduler(tmp_path, Threads(), Turns())
    task = await scheduler.create({
        "name": "Daily check", "prompt": "check", "session_mode": "fresh",
        "schedule": {"kind": "daily", "hour": 9, "minute": 0},
    })
    assert task["next_run"] > 0
    assert await scheduler.run_now(task["id"])
    for _ in range(50):
        history = await scheduler.history()
        if history["history"]:
            break
        await asyncio.sleep(0.01)
    assert history["history"][0]["ok"] is True
    assert history["history"][0]["session_id"] == "thread-scheduled"
    assert (tmp_path / ".muselab-codex" / "scheduler.json").exists()
    await scheduler.close()


def test_next_run_supports_weekly_and_rejects_spent_once():
    assert _next_run({"kind": "weekly", "hour": 9, "minute": 0,
                      "weekdays": [0]}) is not None
    assert _next_run({"kind": "once", "year": 2024, "month": 1, "day": 1,
                      "hour": 0, "minute": 0}) is None
