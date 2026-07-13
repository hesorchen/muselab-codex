"""Codex-native implementation of the existing ``/api/chat`` contract."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse, ServerSentEvent

from ..auth import require_token, require_token_header_or_query, require_token_query
from .approvals import CodexApprovalBroker
from .attachments import CodexAttachmentService
from .elicitation import CodexElicitationBroker
from .process import AppServerError, AppServerResponseError
from .providers import model_for_provider, provider_for_model
from .threads import CodexThreadService
from .turns import (
    CodexTurnService,
    TurnAlreadyActive,
    TurnStream,
    _is_tool_item,
    _tool_result,
    _tool_use,
)
from .user_input import CodexUserInputBroker


_LEGACY_PLACEHOLDER_NAME = re.compile(
    r"^(?:New chat|新会话) \d{2}-\d{2} \d{2}:\d{2}$"
)


router = APIRouter(prefix="/api/chat", tags=["chat"])

_SSE_HEADERS = {
    "Content-Encoding": "identity",
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
}
_STREAM_TICKET_TTL = 60.0
_STREAM_TICKET_LIMIT = 64
_STREAM_TICKETS: dict[str, tuple[float, dict[str, str]]] = {}


class CreateSessionRequest(BaseModel):
    id: str | None = None
    name: str = ""
    model: str = ""
    model_provider: str = ""
    permission: str = "default"
    open_ids: list[str] | None = None


class PatchSessionRequest(BaseModel):
    name: str | None = None
    model: str | None = None
    model_provider: str | None = None
    effort: str | None = None
    thinking: bool | None = None
    pinned: bool | None = None
    system_prompt: str | None = None


class ForkSessionRequest(BaseModel):
    last_turn_id: str | None = None
    model: str = ""
    model_provider: str = ""


class StreamStartRequest(BaseModel):
    prompt: str = ""
    session_id: str
    model: str = ""
    model_provider: str = ""
    permission: str = "default"
    effort: str = ""
    image_ids: str = ""
    source_device_kind: str = Field(
        default="unknown", pattern="^(mobile|desktop|unknown)$")


class PermissionDecisionRequest(BaseModel):
    decision: str
    message: str | None = None


class UserInputAnswerRequest(BaseModel):
    answers: dict[str, Any]


class QueueEnqueueRequest(BaseModel):
    text: str = ""
    image_ids: str = ""
    permission: str = ""
    model: str = ""
    model_provider: str = ""
    effort: str = ""
    source_device_kind: str = Field(
        default="unknown", pattern="^(mobile|desktop|unknown)$")


class QueuePauseRequest(BaseModel):
    paused: bool


class QueueReorderRequest(BaseModel):
    order: list[str]


class SkillConfigRequest(BaseModel):
    path: str
    enabled: bool


class McpServerRequest(BaseModel):
    name: str
    transport: str
    command: str = ""
    args: list[str] = Field(default_factory=list)
    url: str = ""
    bearer_token_env_var: str = ""
    enabled: bool = True


class McpToggleRequest(BaseModel):
    enabled: bool


class TerminalStartRequest(BaseModel):
    command: list[str] = Field(min_length=1, max_length=128)
    cwd: str = ""


class TerminalWriteRequest(BaseModel):
    data: str = ""
    close_stdin: bool = False


def _services(request: Request) -> tuple[CodexThreadService, CodexTurnService]:
    return request.app.state.codex_threads, request.app.state.codex_turns


@router.get("/sessions", dependencies=[Depends(require_token)])
async def list_sessions(
    request: Request,
    response: Response,
    limit: int = Query(100, ge=0, le=2000),
    ids: str = Query(""),
    q: str = Query(""),
    if_none_match: str | None = Header(default=None),
) -> Any:
    del ids
    threads, turns = _services(request)
    try:
        page = await threads.list(limit=limit or 100, search_term=q.strip() or None)
    except (AppServerError, ValueError) as exc:
        raise _http_error(exc) from exc
    sessions = [_session_meta(thread, turns) for thread in page.data]
    payload = {
        "sessions": sessions,
        "total": len(sessions),
        "returned": len(sessions),
        "next_cursor": page.next_cursor,
    }
    digest = hashlib.blake2b(
        json.dumps(payload, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8"),
        digest_size=12,
    ).hexdigest()
    etag = f'W/"{digest}"'
    if if_none_match == etag:
        return Response(status_code=304, headers={"ETag": etag})
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "private, no-cache"
    return payload


@router.post("/sessions", dependencies=[Depends(require_token)])
async def create_session(request: Request, body: CreateSessionRequest) -> dict[str, Any]:
    # app-server owns UUIDv7 thread ids. An application-provided UUID is
    # intentionally ignored; the frontend now adopts the returned native id.
    threads, turns = _services(request)
    try:
        if body.permission not in {"default", "plan", "bypassPermissions"}:
            raise ValueError("unknown permission mode")
        provider = _native_provider_id(body.model, body.model_provider)
        config = request.app.state.codex_providers.thread_config(provider) if provider else None
        thread = await threads.start(
            name=body.name or None,
            model=body.model or None,
            model_provider=provider or None,
            config=config,
        )
    except (AppServerError, ValueError) as exc:
        raise _http_error(exc) from exc
    return _session_meta(
        thread, turns, model=body.model, permission=body.permission)


@router.get("/sessions/{thread_id}", dependencies=[Depends(require_token)])
async def get_session(
    request: Request,
    thread_id: str,
    full: bool = Query(False),
    tail: int = Query(0, ge=0),
    offset: int = Query(-1),
    limit: int = Query(0, ge=0),
) -> dict[str, Any]:
    del full
    _threads, turns = _services(request)
    history = request.app.state.codex_history
    try:
        thread = await history.read(thread_id)
    except AppServerResponseError as exc:
        if exc.code in {-32004, -32600}:
            raise HTTPException(404, "session not found") from exc
        raise _http_error(exc) from exc
    except (AppServerError, ValueError) as exc:
        raise _http_error(exc) from exc
    messages = _thread_messages(thread, request.app.state.codex_attachments)
    total = len(messages)
    if offset >= 0:
        start = min(offset, total)
        end = total if limit <= 0 else min(total, start + limit)
    elif tail > 0:
        start = max(0, total - tail)
        end = total
    else:
        start, end = 0, total
    return {
        **_session_meta(thread, turns),
        "history_unavailable": history.degraded(thread_id),
        "messages": messages[start:end],
        "total": total,
        "offset": start,
        "has_more": start > 0,
    }


@router.get("/sessions/{thread_id}/outline", dependencies=[Depends(require_token)])
async def get_session_outline(request: Request, thread_id: str) -> dict[str, Any]:
    _threads, _turns = _services(request)
    history = request.app.state.codex_history
    try:
        thread = await history.read(thread_id)
    except AppServerResponseError as exc:
        if exc.code in {-32004, -32600}:
            raise HTTPException(404, "session not found") from exc
        raise _http_error(exc) from exc
    except (AppServerError, ValueError) as exc:
        raise _http_error(exc) from exc
    return {
        "outline": _thread_outline(thread),
        "history_unavailable": history.degraded(thread_id),
    }


@router.patch("/sessions/{thread_id}", dependencies=[Depends(require_token)])
async def patch_session(
    request: Request,
    thread_id: str,
    body: PatchSessionRequest,
) -> dict[str, Any]:
    threads, turns = _services(request)
    unsupported = body.system_prompt is not None
    if unsupported:
        raise HTTPException(400, "field is not supported by the Codex-native runtime")
    try:
        if body.name is not None:
            await threads.rename(thread_id, body.name)
        if body.model is not None or body.effort is not None:
            provider = _native_provider_id(body.model or "", body.model_provider or "")
            config = dict(
                request.app.state.codex_providers.thread_config(provider)
                if provider else {}
            )
            if body.effort is not None:
                config["model_reasoning_effort"] = body.effort or None
            await threads.resume(
                thread_id,
                model=(body.model or None) if body.model is not None else None,
                model_provider=provider or None,
                config=config or None,
            )
        thread = await threads.read(thread_id, include_turns=False)
        if body.pinned is not None:
            threads.set_pinned(thread_id, body.pinned)
            thread = {**thread, "pinned": body.pinned}
    except AppServerResponseError as exc:
        if exc.code in {-32004, -32600}:
            raise HTTPException(404, "session not found") from exc
        raise _http_error(exc) from exc
    except (AppServerError, ValueError) as exc:
        raise _http_error(exc) from exc
    return _session_meta(
        thread,
        turns,
        model=body.model or "",
        effort=body.effort,
    )


@router.delete("/sessions/{thread_id}", dependencies=[Depends(require_token)])
async def delete_session(request: Request, thread_id: str) -> dict[str, Any]:
    threads, turns = _services(request)
    if turns.busy(thread_id):
        raise HTTPException(409, "thread operation still running")
    try:
        await threads.delete(thread_id)
    except AppServerResponseError as exc:
        if exc.code == -32004:
            raise HTTPException(404, "session not found") from exc
        raise _http_error(exc) from exc
    except (AppServerError, ValueError) as exc:
        raise _http_error(exc) from exc
    await asyncio.to_thread(
        request.app.state.codex_attachments.delete_thread, thread_id)
    request.app.state.codex_usage.delete(thread_id)
    return {"ok": True}


@router.post("/sessions/{thread_id}/fork", dependencies=[Depends(require_token)])
async def fork_session(
    request: Request,
    thread_id: str,
    body: ForkSessionRequest,
) -> dict[str, Any]:
    threads, turns = _services(request)
    if turns.busy(thread_id):
        raise HTTPException(409, "thread operation still running")
    try:
        provider = _native_provider_id(body.model, body.model_provider)
        config = request.app.state.codex_providers.thread_config(provider) if provider else None
        thread = await threads.fork(
            thread_id,
            last_turn_id=body.last_turn_id or None,
            model=body.model or None,
            model_provider=provider or None,
            config=config,
        )
    except AppServerResponseError as exc:
        if exc.code in {-32004, -32600}:
            raise HTTPException(404, "session not found") from exc
        raise _http_error(exc) from exc
    except (AppServerError, ValueError) as exc:
        raise _http_error(exc) from exc
    return _session_meta(thread, turns, model=body.model)


@router.get("/sessions/{thread_id}/children", dependencies=[Depends(require_token)])
async def list_session_children(request: Request, thread_id: str) -> dict[str, Any]:
    """Expose app-server subagent threads without manufacturing local state."""
    threads, turns = _services(request)
    try:
        # Preserve the normal 404 contract even when a missing parent happens
        # to have no matching child threads in the workspace list.
        await threads.read(thread_id, include_turns=False)
        children = await threads.children(thread_id)
    except AppServerResponseError as exc:
        if exc.code in {-32004, -32600}:
            raise HTTPException(404, "session not found") from exc
        raise _http_error(exc) from exc
    except (AppServerError, ValueError) as exc:
        raise _http_error(exc) from exc
    return {
        "children": [_session_meta(child, turns) for child in children],
        "total": len(children),
    }


@router.get("/sessions/{thread_id}/active", dependencies=[Depends(require_token)])
async def active_session(request: Request, thread_id: str) -> dict[str, Any]:
    _threads, turns = _services(request)
    stream = turns.active(thread_id)
    if stream is None:
        return {"active": False}
    return {
        "active": True,
        "model": stream.model,
        "started_at": stream.started_at,
        "events_so_far": len(stream.events),
        "continuation": False,
        "user_text": stream.user_text,
        "user_images": stream.user_images,
        "user_docs": stream.user_docs,
    }


@router.get("/sessions/{thread_id}/queue", dependencies=[Depends(require_token)])
async def get_queue(request: Request, thread_id: str) -> dict[str, Any]:
    try:
        return request.app.state.codex_queue.get(thread_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/sessions/{thread_id}/queue", dependencies=[Depends(require_token)])
async def enqueue_queue(request: Request, thread_id: str,
                        body: QueueEnqueueRequest) -> dict[str, Any]:
    try:
        provider = _native_provider_id(body.model, body.model_provider)
        item = request.app.state.codex_queue.enqueue(
            thread_id,
            body.text,
            body.image_ids,
            body.permission,
            model=body.model,
            model_provider=provider,
            effort=body.effort,
            source_device_kind=body.source_device_kind,
        )
    except OverflowError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "item": item, **request.app.state.codex_queue.get(thread_id)}


@router.delete("/sessions/{thread_id}/queue/{item_id}", dependencies=[Depends(require_token)])
async def remove_queue(request: Request, thread_id: str, item_id: str) -> dict[str, Any]:
    try:
        if not request.app.state.codex_queue.remove(thread_id, item_id):
            raise HTTPException(404, "queue item not found")
        return {"ok": True, **request.app.state.codex_queue.get(thread_id)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/sessions/{thread_id}/queue", dependencies=[Depends(require_token)])
async def clear_queue(request: Request, thread_id: str) -> dict[str, Any]:
    try:
        request.app.state.codex_queue.clear(thread_id)
        return {"ok": True, **request.app.state.codex_queue.get(thread_id)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/sessions/{thread_id}/queue/pause", dependencies=[Depends(require_token)])
async def pause_queue(request: Request, thread_id: str,
                      body: QueuePauseRequest) -> dict[str, Any]:
    try:
        result = request.app.state.codex_queue.pause(thread_id, body.paused)
        if not body.paused:
            await request.app.state.codex_queue_drain.drain(thread_id)
        return result
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/sessions/{thread_id}/queue/reorder", dependencies=[Depends(require_token)])
async def reorder_queue(request: Request, thread_id: str,
                        body: QueueReorderRequest) -> dict[str, Any]:
    try:
        return request.app.state.codex_queue.reorder(thread_id, body.order)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/providers", dependencies=[Depends(require_token)])
async def providers(request: Request) -> dict[str, Any]:
    runtime = request.app.state.codex_runtime
    try:
        result = await runtime.request("model/list", {
            "limit": 100,
            "includeHidden": False,
        })
    except AppServerError as exc:
        raise _http_error(exc) from exc
    if not isinstance(result, dict) or not isinstance(result.get("data"), list):
        raise HTTPException(502, "model/list returned an invalid result")
    models = []
    default_model = ""
    for model in result["data"]:
        if not isinstance(model, dict) or not isinstance(model.get("model"), str):
            continue
        model_id = model["model"]
        if model.get("isDefault"):
            default_model = model_id
        models.append({
            "group": "Codex",
            "label": str(model.get("displayName") or model_id),
            "model": model_id,
            "supports_thinking": True,
            "supports_effort": bool(model.get("supportedReasoningEfforts")),
            "reasoning_efforts": [
                str(item.get("reasoningEffort"))
                for item in model.get("supportedReasoningEfforts", [])
                if isinstance(item, dict) and item.get("reasoningEffort")
            ],
        })
    configured = await request.app.state.codex_providers.list()
    enabled_ids = {
        str(provider["id"])
        for provider in configured["providers"]
        if provider.get("enabled")
    }
    existing = {(str(item["model"]), str(item.get("provider") or "")) for item in models}
    for item in request.app.state.codex_providers.model_entries(enabled_ids):
        key = (str(item["model"]), str(item.get("provider") or ""))
        if key not in existing:
            models.append(item)
    if not default_model and models:
        default_model = models[0]["model"]
    return {
        "models": models,
        "default_model": default_model,
        "runtime": "codex",
        "has_any_provider": bool(models),
    }


@router.get("/usage/{thread_id}", dependencies=[Depends(require_token)])
async def session_usage(request: Request, thread_id: str, model: str = "") -> dict[str, Any]:
    try:
        native = request.app.state.codex_usage.get(thread_id, model=model)
        if native.get("context_limit"):
            return native
        # Migration fallback for a pre-sidecar thread. New usage is populated
        # only from thread/tokenUsage/updated app-server notifications.
        snapshot = await request.app.state.codex_history.transcripts.read(thread_id)
        if snapshot is not None and snapshot.token_usage is not None:
            return request.app.state.codex_usage.update(
                thread_id, snapshot.token_usage, model=model)
        return native
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/usage", dependencies=[Depends(require_token)])
async def aggregate_usage(request: Request) -> dict[str, Any]:
    """Return the legacy dashboard shape from native per-thread sidecars.

    Codex does not expose a server-wide cost ledger.  Keep the aggregate
    explicitly token-only instead of fabricating provider prices.
    """
    directory = request.app.state.codex_usage.directory
    totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    thread_count = 0
    for path in directory.glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            usage = record.get("token_usage", {})
            total = usage.get("total", {}) if isinstance(usage, dict) else {}
            for key, native_key in (("input_tokens", "inputTokens"),
                                    ("output_tokens", "outputTokens"),
                                    ("total_tokens", "totalTokens")):
                totals[key] += max(0, int(total.get(native_key, 0) or 0))
            thread_count += 1
        except (OSError, ValueError, TypeError):
            continue
    return {
        "runtime": "codex",
        "total_cost_usd": None,
        "cost_available": False,
        "total_messages": 0,
        "active_sessions": thread_count,
        **totals,
    }


@router.get("/cost-dashboard", dependencies=[Depends(require_token)])
async def cost_dashboard(
    request: Request,
    days: int = Query(default=30, ge=1, le=365),
    tz_offset_minutes: int = Query(default=0, ge=-1440, le=1440),
) -> dict[str, Any]:
    """Prefer Codex's authoritative account activity, with a local fallback."""
    try:
        result = await request.app.state.codex_runtime.request(
            "account/usage/read", None, timeout=15)
        if not isinstance(result, dict):
            raise ValueError("account/usage/read returned an invalid result")
        return request.app.state.codex_usage.account_dashboard(
            result, days=days, tz_offset_minutes=tz_offset_minutes)
    except (AppServerError, ValueError):
        return request.app.state.codex_usage.dashboard(
            days=days, tz_offset_minutes=tz_offset_minutes)


@router.get("/context-info", dependencies=[Depends(require_token)])
async def context_info(request: Request) -> dict[str, Any]:
    """Describe native Codex context sources without exposing prompt content."""
    root = Path(request.app.state.codex_threads.workspace)
    config_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    workspace_agents = root / "AGENTS.md"
    global_agents = config_home / "AGENTS.md"
    workspace_instructions_available = workspace_agents.is_file()
    global_instructions_available = global_agents.is_file()
    effective_agents = workspace_agents if workspace_instructions_available else global_agents
    instructions_available = (
        workspace_instructions_available or global_instructions_available
    )
    sources = []
    for scope, path in (("project", workspace_agents),
                        ("user_agents", global_agents),
                        ("project_codex", root / ".codex"),
                        ("user_codex", config_home)):
        if path.exists():
            sources.append({"scope": scope, "path": str(path), "available": True})
    instructions_mtime = None
    if instructions_available:
        try:
            instructions_mtime = effective_agents.stat().st_mtime
        except OSError:
            pass
    return {
        "runtime": "codex",
        "sources": sources,
        "has_any_provider": True,
        "instructions_source": "AGENTS.md",
        "instructions_available": instructions_available,
        # Compatibility fields consumed by the existing archive-status UI.
        "instructions_exists": instructions_available,
        "instructions_mtime": instructions_mtime,
        "workspace_instructions_available": workspace_instructions_available,
        "global_instructions_available": global_instructions_available,
        "workspace_agents_path": str(workspace_agents),
        "global_agents_path": str(global_agents),
        "workspace_root": str(root),
    }


@router.get("/interrupted-turns", dependencies=[Depends(require_token)])
async def interrupted_turns() -> dict[str, Any]:
    # Codex app-server owns turn persistence; it has no equivalent to the
    # There is no application-owned active-turn sidecar. Returning an explicit empty result keeps
    # the browser contract honest and avoids reading legacy session files.
    return {"turns": [], "runtime": "codex"}


@router.post("/interrupted-turns/{thread_id}/dismiss", dependencies=[Depends(require_token)])
async def dismiss_interrupted_turn(thread_id: str) -> dict[str, Any]:
    del thread_id
    return {"ok": True, "runtime": "codex"}


@router.get("/context-breakdown/{thread_id}", dependencies=[Depends(require_token)])
async def context_breakdown(request: Request, thread_id: str) -> dict[str, Any]:
    try:
        result = request.app.state.codex_usage.breakdown(thread_id)
        if result.get("maxTokens"):
            return result
        snapshot = await request.app.state.codex_history.transcripts.read(thread_id)
        if snapshot is not None and snapshot.token_usage is not None:
            request.app.state.codex_usage.update(thread_id, snapshot.token_usage)
            return request.app.state.codex_usage.breakdown(thread_id)
        return result
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/codex-rate-limit", dependencies=[Depends(require_token)])
async def codex_rate_limit(request: Request, refresh: bool = False) -> dict[str, Any]:
    """Read the native account rate-limit snapshot when Codex auth supports it."""
    del refresh  # app-server always returns the current snapshot.
    try:
        result = await request.app.state.codex_runtime.request(
            "account/rateLimits/read", None, timeout=15)
    except AppServerError as exc:
        return {"ok": False, "provider_authoritative": False,
                "windows": {}, "updated_at": time.time(),
                "error": str(exc)}
    result = result if isinstance(result, dict) else {}
    legacy_snapshot = result.get("rateLimits")
    by_limit_id = result.get("rateLimitsByLimitId")
    snapshots = (
        by_limit_id
        if isinstance(by_limit_id, dict) and by_limit_id
        else {"codex": legacy_snapshot if isinstance(legacy_snapshot, dict) else {}}
    )
    windows: dict[str, dict[str, Any]] = {}
    plan_type = None
    for limit_id, snapshot in snapshots.items():
        if not isinstance(snapshot, dict):
            continue
        plan_type = plan_type or snapshot.get("planType")
        for slot in ("primary", "secondary"):
            native = snapshot.get(slot)
            if not (isinstance(native, dict)
                    and isinstance(native.get("usedPercent"), (int, float))
                    and not isinstance(native.get("usedPercent"), bool)):
                continue
            duration = native.get("windowDurationMins")
            window_type = _rate_limit_window_type(duration, slot)
            key = window_type
            if key in windows:
                key = f"{limit_id}:{slot}"
            used_percent = max(0, min(100, native["usedPercent"]))
            windows[key] = {
                "rate_limit_type": window_type,
                "limit_id": str(limit_id),
                "limit_name": snapshot.get("limitName"),
                "used_percent": used_percent,
                "remaining_percent": 100 - used_percent,
                "resets_at": native.get("resetsAt"),
                "window_duration_mins": duration,
            }
    return {"ok": True, "provider_authoritative": bool(windows),
            "windows": windows, "updated_at": time.time(),
            "plan_type": plan_type}


def _rate_limit_window_type(duration: Any, slot: str) -> str:
    """Name a native rolling window by duration, never by array position."""
    names = {
        300: "five_hour",
        1440: "one_day",
        10_080: "seven_day",
        43_200: "thirty_day",
    }
    if isinstance(duration, int) and not isinstance(duration, bool):
        return names.get(duration, f"rolling_{duration}_minutes")
    return slot


@router.get("/skills", dependencies=[Depends(require_token)])
async def list_skills(request: Request, force_reload: bool = False) -> dict[str, Any]:
    try:
        return await request.app.state.codex_skills.list(force_reload=force_reload)
    except (AppServerError, ValueError) as exc:
        raise _http_error(exc) from exc


@router.patch("/skills", dependencies=[Depends(require_token)])
async def configure_skill(request: Request, body: SkillConfigRequest) -> dict[str, Any]:
    try:
        return await request.app.state.codex_skills.set_enabled(body.path, body.enabled)
    except (AppServerError, ValueError) as exc:
        raise _http_error(exc) from exc


@router.get("/mcp", dependencies=[Depends(require_token)])
async def list_mcp(request: Request, reload: bool = False) -> dict[str, Any]:
    try:
        return await request.app.state.codex_mcp.list(reload=reload)
    except (AppServerError, ValueError) as exc:
        raise _http_error(exc) from exc


@router.post("/mcp", dependencies=[Depends(require_token)])
async def add_mcp(request: Request, body: McpServerRequest) -> dict[str, Any]:
    try:
        return await request.app.state.codex_mcp.add(
            body.name,
            transport=body.transport,
            command=body.command,
            args=body.args,
            url=body.url,
            bearer_token_env_var=body.bearer_token_env_var,
            enabled=body.enabled,
        )
    except (AppServerError, ValueError) as exc:
        raise _http_error(exc) from exc


@router.patch("/mcp/{name}", dependencies=[Depends(require_token)])
async def toggle_mcp(
    request: Request,
    name: str,
    body: McpToggleRequest,
) -> dict[str, Any]:
    try:
        return await request.app.state.codex_mcp.set_enabled(name, body.enabled)
    except (AppServerError, ValueError) as exc:
        raise _http_error(exc) from exc


@router.delete("/mcp/{name}", dependencies=[Depends(require_token)])
async def delete_mcp(request: Request, name: str) -> dict[str, Any]:
    try:
        return await request.app.state.codex_mcp.delete(name)
    except (AppServerError, ValueError) as exc:
        raise _http_error(exc) from exc


@router.post("/mcp/{name}/oauth", dependencies=[Depends(require_token)])
async def login_mcp_oauth(request: Request, name: str) -> dict[str, str]:
    try:
        return await request.app.state.codex_mcp.oauth_login(name)
    except (AppServerError, ValueError) as exc:
        raise _http_error(exc) from exc


@router.get("/terminal", dependencies=[Depends(require_token)])
async def list_terminal_processes(request: Request) -> dict[str, Any]:
    return {"processes": request.app.state.codex_terminal.list()}


@router.post("/terminal", dependencies=[Depends(require_token)])
async def start_terminal_process(
    request: Request, body: TerminalStartRequest,
) -> dict[str, Any]:
    try:
        return await request.app.state.codex_terminal.start(body.command, cwd=body.cwd)
    except OverflowError as exc:
        raise HTTPException(409, str(exc)) from exc
    except (AppServerError, ValueError) as exc:
        raise _http_error(exc) from exc


@router.get("/terminal/{process_id}", dependencies=[Depends(require_token)])
async def get_terminal_process(request: Request, process_id: str) -> dict[str, Any]:
    try:
        return request.app.state.codex_terminal.get(process_id)
    except KeyError as exc:
        raise HTTPException(404, "terminal process not found") from exc


@router.post("/terminal/{process_id}/input", dependencies=[Depends(require_token)])
async def write_terminal_process(
    request: Request, process_id: str, body: TerminalWriteRequest,
) -> dict[str, Any]:
    try:
        return await request.app.state.codex_terminal.write(
            process_id, body.data, close_stdin=body.close_stdin)
    except KeyError as exc:
        raise HTTPException(404, "terminal process not found") from exc
    except (AppServerError, ValueError) as exc:
        raise _http_error(exc) from exc


@router.post("/terminal/{process_id}/terminate", dependencies=[Depends(require_token)])
async def terminate_terminal_process(request: Request, process_id: str) -> dict[str, Any]:
    try:
        return await request.app.state.codex_terminal.terminate(process_id)
    except KeyError as exc:
        raise HTTPException(404, "terminal process not found") from exc
    except (AppServerError, ValueError) as exc:
        raise _http_error(exc) from exc


@router.post(
    "/sessions/{thread_id}/native-compact",
    dependencies=[Depends(require_token)],
)
async def compact_session(request: Request, thread_id: str) -> dict[str, Any]:
    _threads, turns = _services(request)
    if turns.busy(thread_id):
        raise HTTPException(409, "previous turn still running")
    try:
        return await request.app.state.codex_compact.compact(thread_id)
    except TurnAlreadyActive as exc:
        raise HTTPException(409, str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(504, "native compact timed out") from exc
    except (AppServerError, ValueError) as exc:
        raise _http_error(exc) from exc


@router.post("/stream/start", dependencies=[Depends(require_token)])
async def stream_start(body: StreamStartRequest) -> dict[str, str]:
    now = time.monotonic()
    for key in [key for key, (expires, _value) in _STREAM_TICKETS.items()
                if expires <= now]:
        _STREAM_TICKETS.pop(key, None)
    while len(_STREAM_TICKETS) >= _STREAM_TICKET_LIMIT:
        _STREAM_TICKETS.pop(next(iter(_STREAM_TICKETS)))
    ticket = secrets.token_urlsafe(32)
    _STREAM_TICKETS[ticket] = (now + _STREAM_TICKET_TTL, {
        "prompt": body.prompt,
        "session_id": body.session_id,
        "model": body.model,
        "model_provider": body.model_provider,
        "permission": body.permission,
        "effort": body.effort,
        "image_ids": body.image_ids,
        "source_device_kind": body.source_device_kind,
    })
    return {"ticket": ticket}


@router.get("/stream")
async def stream(
    request: Request,
    ticket: str = Query(""),
    prompt: str = Query(""),
    session_id: str = Query(""),
    model: str = Query(""),
    model_provider: str = Query(""),
    permission: str = Query("default"),
    effort: str = Query(""),
    image_ids: str = Query(""),
    source_device_kind: str = Query("unknown"),
    token: str = Query(""),
):
    if ticket:
        entry = _STREAM_TICKETS.pop(ticket, None)
        if entry is None or entry[0] <= time.monotonic():
            raise HTTPException(401, "invalid or expired stream ticket")
        params = entry[1]
        prompt = params["prompt"]
        session_id = params["session_id"]
        model = params["model"]
        model_provider = params["model_provider"]
        permission = params["permission"]
        effort = params["effort"]
        image_ids = params["image_ids"]
        source_device_kind = params["source_device_kind"]
    else:
        from ..auth import _token_ok
        if not _token_ok(token):
            raise HTTPException(401, "bad token")
        if not session_id:
            raise HTTPException(422, "session_id required")

    _threads, turns = _services(request)
    if prompt.strip() or image_ids.strip():
        from ..turn_notifications import clear_turn_origin, record_turn_origin
        record_turn_origin(session_id, source_device_kind)
        try:
            prepared = request.app.state.codex_attachments.prepare(
                session_id, image_ids)
            provider = _native_provider_id(model, model_provider)
            config = request.app.state.codex_providers.thread_config(provider) if provider else None
            turn_stream = await turns.start(
                session_id,
                prompt,
                model=model,
                model_provider=provider,
                config=config,
                permission=permission,
                effort=effort,
                inputs=prepared.inputs,
                user_images=prepared.images,
                user_docs=prepared.docs,
                client_user_message_id=prepared.client_user_message_id,
            )
        except TurnAlreadyActive as exc:
            clear_turn_origin(session_id)
            return _event_source(_one_event("error", {
                "error": str(exc),
                "kind": "turn_busy",
                "retryable": True,
            }))
        except (AppServerError, ValueError) as exc:
            clear_turn_origin(session_id)
            return _event_source(_one_event("error", {
                "error": str(exc),
                "kind": "turn_start_failed",
                "retryable": True,
            }))
        except BaseException:
            clear_turn_origin(session_id)
            raise
    else:
        turn_stream = turns.active(session_id)
        if turn_stream is None:
            return _event_source(_one_event("error", {
                "error": "no active turn",
                "kind": "no_active_turn",
                "retryable": False,
            }))
    return _event_source(_stream_events(turn_stream))


@router.post("/upload-image", dependencies=[Depends(require_token)])
async def upload_attachment(request: Request, file: UploadFile = File(...)) -> dict[str, Any]:
    attachments: CodexAttachmentService = request.app.state.codex_attachments
    return await attachments.upload(file)


@router.get(
    "/attachments/{thread_id}/{filename}",
    dependencies=[Depends(require_token_query)],
)
async def get_attachment(request: Request, thread_id: str, filename: str) -> FileResponse:
    attachments: CodexAttachmentService = request.app.state.codex_attachments
    try:
        path, mime = attachments.resolve(thread_id, filename)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return FileResponse(
        path,
        media_type=mime,
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


@router.post("/permission/{thread_id}/{request_id}", dependencies=[Depends(require_token)])
async def submit_permission(
    request: Request,
    thread_id: str,
    request_id: str,
    body: PermissionDecisionRequest,
) -> dict[str, bool]:
    broker: CodexApprovalBroker = request.app.state.codex_approvals
    try:
        submitted = broker.submit(thread_id, request_id, body.decision)
        if not submitted:
            elicitation: CodexElicitationBroker = request.app.state.codex_elicitation
            submitted = elicitation.submit_decision(
                thread_id, request_id, body.decision)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not submitted:
        raise HTTPException(404, "no pending permission request with that id")
    return {"ok": True}


@router.post("/answer/{thread_id}/{request_id}", dependencies=[Depends(require_token)])
async def submit_user_input(
    request: Request,
    thread_id: str,
    request_id: str,
    body: UserInputAnswerRequest,
) -> dict[str, bool]:
    broker: CodexUserInputBroker = request.app.state.codex_user_input
    try:
        submitted = broker.submit(thread_id, request_id, body.answers)
        if not submitted:
            elicitation: CodexElicitationBroker = request.app.state.codex_elicitation
            submitted = elicitation.submit_answers(
                thread_id, request_id, body.answers)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not submitted:
        raise HTTPException(404, "no pending user input request with that id")
    return {"ok": True}


@router.post("/interrupt", dependencies=[Depends(require_token_header_or_query)])
async def interrupt(request: Request, session_id: str) -> dict[str, Any]:
    _threads, turns = _services(request)
    try:
        interrupted = await turns.interrupt(session_id)
    except AppServerError as exc:
        raise _http_error(exc) from exc
    return {
        "ok": True,
        "interrupted": [session_id] if interrupted else [],
    }


def _event_source(generator) -> EventSourceResponse:
    return EventSourceResponse(
        generator,
        headers=_SSE_HEADERS,
        ping_message_factory=lambda: ServerSentEvent(data="", event="ping"),
    )


async def _one_event(event: str, data: dict[str, Any]):
    yield ServerSentEvent(
        event=event,
        data=json.dumps(data, ensure_ascii=False, separators=(",", ":")),
    )


async def _stream_events(stream: TurnStream):
    queue = stream.subscribe()
    try:
        while True:
            envelope = await queue.get()
            if not isinstance(envelope, dict):
                break
            yield ServerSentEvent(
                event=envelope["event"],
                data=json.dumps(
                    envelope["data"], ensure_ascii=False, separators=(",", ":")),
            )
    finally:
        stream.unsubscribe(queue)


def _session_meta(
    thread: dict[str, Any],
    turns: CodexTurnService,
    *,
    model: str = "",
    effort: str | None = None,
    permission: str | None = None,
) -> dict[str, Any]:
    thread_id = str(thread.get("id") or "")
    messages = _thread_messages(thread)
    created = float(thread.get("createdAt") or time.time())
    updated = float(thread.get("updatedAt") or created)
    explicit_name = thread.get("name")
    if isinstance(explicit_name, str) and _LEGACY_PLACEHOLDER_NAME.fullmatch(
        explicit_name
    ):
        # Older muselab-codex builds persisted a timestamp placeholder via
        # thread/name/set. That converted an unnamed Codex thread into an
        # explicitly named one, permanently masking Codex's native preview.
        # Treat only that exact app-generated shape as unnamed for display.
        explicit_name = None
    name = explicit_name or thread.get("preview") or "New chat"
    settings = thread.get("_settings")
    settings = settings if isinstance(settings, dict) else {}
    resolved_model = (
        model
        or str(settings.get("model") or "")
        or model_for_provider(str(thread.get("modelProvider") or ""))
    )
    return {
        "id": thread_id,
        "name": str(name),
        "model": resolved_model,
        "model_provider": str(thread.get("modelProvider") or ""),
        "system_prompt": "",
        "created_at": created,
        "updated_at": updated,
        "message_count": len(messages),
        "turn_count": len(thread.get("turns") or []),
        "auto_named": explicit_name is None,
        "pinned": bool(thread.get("pinned", False)),
        "active": turns.active(thread_id) is not None,
        "effort": effort if effort is not None else str(settings.get("effort") or ""),
        "permission": (
            permission
            if permission is not None
            else str(settings.get("permission") or "default")
        ),
        "thinking": True,
        "parent_thread_id": (
            thread["parentThreadId"]
            if isinstance(thread.get("parentThreadId"), str) else None
        ),
    }


def _native_provider_id(model: str, requested_provider: str) -> str:
    """Validate optional client metadata against the curated model registry."""
    detected = provider_for_model(model)
    requested = requested_provider.strip()
    if requested and detected is not None and requested != detected.id:
        raise ValueError("model does not belong to the requested provider")
    if requested:
        # Validate names even when the model is omitted (e.g. a new thread).
        request_provider = provider_for_model(model_for_provider(requested))
        if request_provider is None:
            raise ValueError("unknown Codex-native provider")
    return detected.id if detected is not None else requested


def _thread_messages(
    thread: dict[str, Any],
    attachments: CodexAttachmentService | None = None,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    user_ids: dict[str, int] = {}
    user_ordinal = 0
    turns = thread.get("turns")
    if not isinstance(turns, list):
        return messages
    for turn in turns:
        if not isinstance(turn, dict) or not isinstance(turn.get("items"), list):
            continue
        for item in _visible_turn_items(turn["items"]):
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "userMessage":
                user_ordinal += 1
                content = item.get("content", [])
                text = "".join(
                    str(part.get("text") or "")
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                )
                message: dict[str, Any] = {
                    "role": "user",
                    "text": text,
                    "uuid": _user_message_uuid(
                        thread, item, user_ordinal, user_ids),
                }
                if attachments is not None and isinstance(content, list):
                    images, docs = attachments.history_items(
                        str(thread.get("id") or ""),
                        content,
                        item.get("clientId"),
                    )
                    if images:
                        message["images"] = images
                    if docs:
                        message["docs"] = docs
                messages.append(message)
            elif item_type == "agentMessage":
                messages.append({
                    "role": "assistant",
                    "text": str(item.get("text") or ""),
                })
            elif item_type == "reasoning":
                parts = item.get("summary") or item.get("content") or []
                messages.append({
                    "role": "thinking",
                    "text": "\n".join(str(part) for part in parts),
                })
            elif item_type == "toolUse":
                messages.append({
                    "role": "tool_use",
                    "id": str(item.get("id") or ""),
                    "name": str(item.get("name") or "Tool"),
                    "summary": str(item.get("summary") or ""),
                    "input": (
                        dict(item["input"])
                        if isinstance(item.get("input"), dict) else {}
                    ),
                })
            elif item_type == "toolResult":
                text = str(item.get("text") or "")
                tool_id = str(item.get("id") or "")
                messages.append({
                    "role": "tool_result",
                    "id": tool_id,
                    "tool_use_id": tool_id,
                    "tool_name": str(item.get("toolName") or "Tool"),
                    "preview": str(item.get("preview") or text[:500]),
                    "text": text,
                    "truncated": bool(item.get("truncated")),
                    "text_truncated": bool(item.get("truncated")),
                    "is_error": bool(item.get("isError")),
                })
            elif _is_tool_item(item):
                # ``thread/read(includeTurns=true)`` returns the same native
                # ThreadItem kinds that arrive through item/started and
                # item/completed while a turn is live.  Reuse the live-stream
                # projection so a post-turn history refresh does not replace
                # command/file/MCP cards with only the final agent message.
                tool_use = _tool_use(item)
                messages.append({"role": "tool_use", **tool_use})
                if item.get("status") not in {"inProgress", "pending"}:
                    tool_result = _tool_result(item)
                    messages.append({
                        "role": "tool_result",
                        "tool_use_id": tool_result["id"],
                        **tool_result,
                    })
            elif item_type == "contextCompaction":
                messages.append({
                    "role": "assistant",
                    "text": "",
                    "_is_compact_summary": True,
                })
    return messages


def _thread_outline(thread: dict[str, Any]) -> list[dict[str, Any]]:
    outline: list[dict[str, Any]] = []
    user_ids: dict[str, int] = {}
    user_ordinal = 0
    turns = thread.get("turns")
    if not isinstance(turns, list):
        return outline
    for turn in turns:
        if not isinstance(turn, dict) or not isinstance(turn.get("items"), list):
            continue
        for item in _visible_turn_items(turn["items"]):
            if not isinstance(item, dict) or item.get("type") != "userMessage":
                continue
            user_ordinal += 1
            text = "".join(
                str(part.get("text") or "")
                for part in item.get("content", [])
                if isinstance(part, dict) and part.get("type") == "text"
            )
            outline.append({
                "preview": _outline_preview(text),
                "uuid": _user_message_uuid(
                    thread, item, user_ordinal, user_ids),
            })
    return outline


def _visible_turn_items(items: list[Any]) -> list[Any]:
    """Hide Codex-injected user context from legacy thread/read results.

    Paginated and JSONL history is already normalized by ``_project_turns``.
    This duplicate-id coalescing keeps the older ``thread/read`` fallback from
    exposing the same internal context when native item history is unavailable.
    """
    visible: list[Any] = []
    for item in items:
        if isinstance(item, dict) and item.get("type") == "userMessage" and visible:
            previous = visible[-1]
            item_id = item.get("id")
            if (
                isinstance(previous, dict)
                and previous.get("type") == "userMessage"
                and isinstance(item_id, str)
                and bool(item_id)
                and item_id == previous.get("id")
            ):
                visible[-1] = item
                continue
        visible.append(item)
    return visible


def _user_message_uuid(
    thread: dict[str, Any],
    item: dict[str, Any],
    ordinal: int,
    seen: dict[str, int],
) -> str:
    """Return a stable, unique DOM/jump id for one user message.

    Codex transcript projections can attach the turn id to more than one
    userMessage item.  That value is useful but is not unique enough for an
    Alpine ``x-for`` key.  Keep the first occurrence unchanged for backwards
    compatibility and suffix later occurrences deterministically.  Missing
    ids fall back to the thread id plus user-message ordinal; messages and the
    outline call this helper in the same order, so outline jumps still target
    the matching bubble.
    """
    value = item.get("id")
    base = value if isinstance(value, str) and value else (
        f"{str(thread.get('id') or 'thread')}-user-{ordinal}")
    occurrence = seen.get(base, 0) + 1
    seen[base] = occurrence
    return base if occurrence == 1 else f"{base}-{occurrence}"


def _outline_preview(text: str) -> str:
    raw = text.strip()
    if not raw:
        return "(empty)"
    lines = [line.strip() for line in raw.splitlines()
             if line.strip() and not line.lstrip().startswith(">")]
    preview = (lines[0] if lines else raw.splitlines()[0]).lstrip("#").strip()
    return preview[:77] + "…" if len(preview) > 80 else preview


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ValueError):
        return HTTPException(400, str(exc))
    return HTTPException(502, str(exc))
