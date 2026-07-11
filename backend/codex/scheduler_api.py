"""HTTP adapter for Codex-native scheduled prompts."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..auth import require_token


router = APIRouter(prefix="/api/scheduler", tags=["scheduler"])


class ScheduleIn(BaseModel):
    kind: str = Field(pattern="^(daily|weekly|monthly|once)$")
    hour: int = Field(ge=0, le=23)
    minute: int = Field(ge=0, le=59)
    weekdays: list[int] | None = None
    day: int | None = Field(default=None, ge=1, le=31)
    year: int | None = Field(default=None, ge=2024, le=2100)
    month: int | None = Field(default=None, ge=1, le=12)
    tz_offset_minutes: int | None = Field(default=None, ge=-840, le=840)
    tz: str | None = Field(default=None, max_length=64)
    times: list[dict[str, int]] | None = Field(default=None, max_length=24)


class TaskIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    prompt: str = Field(min_length=1, max_length=20000)
    schedule: ScheduleIn
    model: str = ""
    session_mode: str = Field(default="fresh", pattern="^(fresh|reuse)$")


class TaskPatch(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    prompt: str | None = Field(default=None, max_length=20000)
    schedule: ScheduleIn | None = None
    model: str | None = None
    enabled: bool | None = None
    session_mode: str | None = Field(default=None, pattern="^(fresh|reuse)$")


def _scheduler(request: Request):
    return request.app.state.codex_scheduler


@router.get("/tasks", dependencies=[Depends(require_token)])
async def list_tasks(request: Request) -> dict[str, Any]:
    return await _scheduler(request).list_tasks()


@router.post("/tasks", dependencies=[Depends(require_token)])
async def create_task(request: Request, body: TaskIn) -> dict[str, Any]:
    try:
        return await _scheduler(request).create(body.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.patch("/tasks/{task_id}", dependencies=[Depends(require_token)])
async def patch_task(request: Request, task_id: str, body: TaskPatch) -> dict[str, Any]:
    try:
        result = await _scheduler(request).update(
            task_id, body.model_dump(exclude_unset=True, exclude_none=True))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if result is None:
        raise HTTPException(404, "task not found")
    return result


@router.delete("/tasks/{task_id}", dependencies=[Depends(require_token)])
async def delete_task(request: Request, task_id: str) -> dict[str, str]:
    if not await _scheduler(request).delete(task_id):
        raise HTTPException(404, "task not found")
    return {"deleted": task_id}


@router.post("/tasks/{task_id}/run", dependencies=[Depends(require_token)])
async def run_task(request: Request, task_id: str) -> dict[str, Any]:
    if not await _scheduler(request).run_now(task_id):
        raise HTTPException(404, "task not found")
    return {"ok": True, "task_id": task_id}


@router.get("/history", dependencies=[Depends(require_token)])
async def history(request: Request, limit: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
    return await _scheduler(request).history(limit=limit)


@router.delete("/history", dependencies=[Depends(require_token)])
async def clear_history(request: Request) -> dict[str, int]:
    return {"cleared": await _scheduler(request).clear_history()}


@router.delete("/history/{ts}", dependencies=[Depends(require_token)])
async def delete_history(request: Request, ts: float, task_id: str = Query("")) -> dict[str, bool]:
    await _scheduler(request).delete_history(ts, task_id)
    return {"deleted": True}


@router.get("/tasks/{task_id}/history", dependencies=[Depends(require_token)])
async def task_history(request: Request, task_id: str,
                       limit: int = Query(100, ge=1, le=200)) -> dict[str, Any]:
    return await _scheduler(request).history(task_id=task_id, limit=limit)


@router.post("/ack", dependencies=[Depends(require_token)])
async def ack(request: Request) -> dict[str, int]:
    return {"unread_count": await _scheduler(request).ack()}
