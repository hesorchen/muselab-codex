"""Authenticated activity-center API."""

from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, Depends, Query, Request, Response

from .auth import require_token


router = APIRouter(prefix="/api/activity", tags=["activity"])


def _conditional_json(request: Request, response: Response, payload: dict):
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.blake2b(encoded, digest_size=12).hexdigest()
    etag = f'W/"{digest}"'
    headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    response.headers.update(headers)
    return payload


@router.get("", dependencies=[Depends(require_token)])
def list_activity(
    request: Request, response: Response,
    limit: int = Query(100, ge=1, le=500),
):
    service = request.app.state.activity
    return _conditional_json(request, response, {
        "events": service.list(limit=limit), "summary": service.summary(),
    })


@router.get("/summary", dependencies=[Depends(require_token)])
def activity_summary(request: Request, response: Response):
    return _conditional_json(
        request, response, request.app.state.activity.summary())


@router.post("/{event_id}/ack", dependencies=[Depends(require_token)])
def ack_activity(request: Request, event_id: str) -> dict:
    changed = request.app.state.activity.ack(event_id)
    return {"ok": True, "changed": changed, "summary": request.app.state.activity.summary()}


@router.post("/ack-all", dependencies=[Depends(require_token)])
def ack_all_activity(request: Request) -> dict:
    changed = request.app.state.activity.ack()
    return {"ok": True, "changed": changed, "summary": request.app.state.activity.summary()}


@router.post("/thread/{thread_id}/ack", dependencies=[Depends(require_token)])
def ack_thread_activity(request: Request, thread_id: str) -> dict:
    changed = request.app.state.activity.ack_thread(thread_id)
    return {"ok": True, "changed": changed, "summary": request.app.state.activity.summary()}
