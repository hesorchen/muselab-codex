from types import SimpleNamespace

import pytest
from fastapi import Response

from backend.activity import ActivityService
from backend.activity_api import _conditional_json


class Threads:
    async def read(self, thread_id, *, include_turns=False):
        return {
            "id": thread_id, "name": "Build report",
            "preview": "Create the July allocation report", "cwd": "/work/report",
        }

    def list_workspaces(self):
        return [SimpleNamespace(path="/work/report", name="Reports")]


@pytest.mark.asyncio
async def test_activity_tracks_cross_workspace_lifecycle_and_ack(tmp_path):
    service = ActivityService(tmp_path, Threads())

    started = await service.start("thread-1", summary="Running tests")
    assert started["workspace_name"] == "Workspace"
    await service.drain()
    assert service.latest_thread("thread-1")["workspace_name"] == "Reports"
    assert service.summary()["running"] == 1

    waiting = await service.set_state(
        "thread-1", "waiting_approval", summary="Approve command")
    assert waiting["needs_attention"] is True
    assert waiting["task_summary"] == "Running tests"
    assert waiting["status_detail"] == "Approve command"
    assert service.summary()["attention"] == 1

    await service.set_state("thread-1", "running", summary="Continuing")
    finished = await service.finish("thread-1", "completed")
    assert finished["state"] == "completed"
    assert finished["task_summary"] == "Running tests"
    assert finished["status_detail"] == "Task completed"
    assert service.summary() == {
        "running": 0,
        "unread": 1,
        "attention": 0,
        "workspaces": [{
            "path": "/work/report", "name": "Reports",
            "running": 0, "unread": 1, "attention": 0,
        }],
    }

    assert service.ack_thread("thread-1") == 1
    assert service.summary()["unread"] == 0
    assert service.ack(started["id"]) == 0


@pytest.mark.asyncio
async def test_activity_recovers_inflight_task_after_restart(tmp_path):
    service = ActivityService(tmp_path, Threads())
    await service.start("thread-1")
    await service.drain()

    recovered = ActivityService(tmp_path, Threads())
    event = recovered.latest_thread("thread-1")

    assert event["state"] == "failed"
    assert event["status_detail"] == "Interrupted by service restart"
    assert recovered.summary()["attention"] == 1


@pytest.mark.asyncio
async def test_activity_is_bounded(tmp_path):
    service = ActivityService(tmp_path, Threads())
    service._events = [
        {"id": str(index), "thread_id": f"old-{index}", "state": "completed"}
        for index in range(500)
    ]
    await service.start("new-thread")

    assert len(service.list(limit=500)) == 500


@pytest.mark.asyncio
async def test_activity_backfills_legacy_generic_task_labels(tmp_path):
    service = ActivityService(tmp_path, Threads())
    service._events = [{
        "id": "legacy", "thread_id": "thread-1", "state": "completed",
        "summary": "Task completed", "session_name": "Muse task",
    }]

    await service.refresh_metadata()

    event = service.latest_thread("thread-1")
    assert event["task_summary"] == "Create the July allocation report"
    assert event["session_name"] == "Build report"


@pytest.mark.asyncio
async def test_activity_reuses_one_card_for_each_conversation(tmp_path):
    service = ActivityService(tmp_path, Threads())
    first = await service.start("thread-1", summary="First task")
    await service.finish("thread-1", "completed")

    second = await service.start("thread-1", summary="Follow-up task")
    await service.finish("thread-1", "completed")

    assert second["id"] == first["id"]
    assert len(service.list()) == 1
    event = service.latest_thread("thread-1")
    assert event["task_summary"] == "Follow-up task"
    assert event["turn_count"] == 2


def test_activity_conditional_json_returns_304_for_matching_etag():
    response = Response()
    payload = {"running": 1, "unread": 0}
    assert _conditional_json(
        SimpleNamespace(headers={}), response, payload) == payload
    etag = response.headers["etag"]

    cached = _conditional_json(
        SimpleNamespace(headers={"if-none-match": etag}), Response(), payload)
    assert cached.status_code == 304
    assert cached.headers["etag"] == etag
