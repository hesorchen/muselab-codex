"""Codex turn lifecycle, replay buffer, and UI-neutral event mapping."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from collections.abc import Awaitable, Callable
from typing import Any

from .event_router import CodexEventRouter, EventSubscription
from .history import CodexHistoryService
from .process import AppServerError, AppServerProtocolError
from .runtime import CodexRuntime
from .threads import CodexThreadService
from .usage import CodexUsageService


_STREAM_CLOSED = object()


@dataclass
class TurnStream:
    thread_id: str
    user_text: str
    model: str
    user_images: list[dict[str, Any]] = field(default_factory=list)
    user_docs: list[dict[str, Any]] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    turn_id: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    done: bool = False
    status: str = "inProgress"
    session_usage: dict[str, Any] | None = None
    _subscribers: set[asyncio.Queue] = field(default_factory=set, repr=False)

    def publish(self, event: str, data: dict[str, Any]) -> None:
        envelope = {"event": event, "data": data}
        self.events.append(envelope)
        for queue in tuple(self._subscribers):
            queue.put_nowait(envelope)

    def finish(self) -> None:
        self.done = True
        for queue in tuple(self._subscribers):
            queue.put_nowait(_STREAM_CLOSED)

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        for event in self.events:
            queue.put_nowait(event)
        if self.done:
            queue.put_nowait(_STREAM_CLOSED)
        else:
            self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)


class CodexTurnService:
    """Start, stream, approve, and interrupt one active turn per thread."""

    def __init__(
        self,
        runtime: CodexRuntime,
        events: CodexEventRouter,
        threads: CodexThreadService,
        history: CodexHistoryService | None = None,
        usage: CodexUsageService | None = None,
        on_turn_finished: Callable[[str, str], Awaitable[None]] | None = None,
    ):
        self.runtime = runtime
        self.events = events
        self.threads = threads
        self.history = history
        self.usage = usage
        self.on_turn_finished = on_turn_finished
        self._active: dict[str, TurnStream] = {}
        self._operations: set[str] = set()
        self._loaded_generation: dict[str, int] = {}
        self._tasks: set[asyncio.Task] = set()
        self._lock = asyncio.Lock()

    async def start(
        self,
        thread_id: str,
        prompt: str,
        *,
        model: str = "",
        model_provider: str = "",
        config: dict[str, Any] | None = None,
        permission: str = "default",
        effort: str = "",
        inputs: list[dict[str, Any]] | None = None,
        user_images: list[dict[str, Any]] | None = None,
        user_docs: list[dict[str, Any]] | None = None,
        client_user_message_id: str | None = None,
    ) -> TurnStream:
        clean_id = thread_id.strip()
        clean_prompt = prompt.strip()
        extra_inputs = list(inputs or [])
        if not clean_id:
            raise ValueError("thread id cannot be empty")
        if not clean_prompt and not extra_inputs:
            raise ValueError("prompt and attachments cannot both be empty")

        async with self._lock:
            existing = self._active.get(clean_id)
            if existing is not None and not existing.done:
                raise TurnAlreadyActive("previous turn still running")
            if clean_id in self._operations:
                raise TurnAlreadyActive("thread operation still running")
            generation = self.runtime.health().restart_count
            if self._loaded_generation.get(clean_id) != generation:
                # A persisted thread is not automatically loaded into a fresh
                # app-server process. ``turn/start`` against that unloaded id
                # returns -32600, so resume once per runtime generation. For a
                # brand-new pre-first-turn thread, CodexThreadService's pending
                # sidecar absorbs the expected -32600 and the original process
                # can still accept the first turn.
                resume_kwargs: dict[str, Any] = {"model": model or None}
                if model_provider:
                    resume_kwargs["model_provider"] = model_provider
                if config:
                    resume_kwargs["config"] = config
                await self.threads.resume(clean_id, **resume_kwargs)
                self._loaded_generation[clean_id] = generation
            subscription = await self.events.subscribe(clean_id)
            stream = TurnStream(
                clean_id,
                clean_prompt,
                model,
                user_images=list(user_images or []),
                user_docs=list(user_docs or []),
            )
            self._active[clean_id] = stream
            turn_input = []
            if clean_prompt:
                turn_input.append({"type": "text", "text": clean_prompt})
            turn_input.extend(extra_inputs)
            params: dict[str, Any] = {
                "threadId": clean_id,
                "input": turn_input,
            }
            params.update(_permission_overrides(permission, model, effort))
            if model:
                params["model"] = model
            if effort:
                params["effort"] = effort
            if client_user_message_id:
                params["clientUserMessageId"] = client_user_message_id
            try:
                result = await self.runtime.request("turn/start", params)
                turn = _turn_from_result(result)
                self.threads.mark_materialized(clean_id)
                stream.turn_id = turn["id"]
                stream.status = str(turn.get("status") or "inProgress")
            except BaseException:
                self._active.pop(clean_id, None)
                await subscription.close()
                raise
            task = asyncio.create_task(
                self._pump(stream, subscription),
                name=f"codex-turn-{stream.turn_id}",
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return stream

    def active(self, thread_id: str) -> TurnStream | None:
        stream = self._active.get(thread_id)
        return stream if stream is not None and not stream.done else None

    def busy(self, thread_id: str) -> bool:
        return self.active(thread_id) is not None or thread_id in self._operations

    async def begin_operation(self, thread_id: str, *, model: str = "") -> None:
        clean_id = thread_id.strip()
        if not clean_id:
            raise ValueError("thread id cannot be empty")
        async with self._lock:
            stream = self._active.get(clean_id)
            if stream is not None and not stream.done:
                raise TurnAlreadyActive("previous turn still running")
            if clean_id in self._operations:
                raise TurnAlreadyActive("thread operation still running")
            self._operations.add(clean_id)
            try:
                generation = self.runtime.health().restart_count
                if self._loaded_generation.get(clean_id) != generation:
                    await self.threads.resume(clean_id, model=model or None)
                    self._loaded_generation[clean_id] = generation
            except BaseException:
                self._operations.discard(clean_id)
                raise

    async def end_operation(self, thread_id: str) -> None:
        async with self._lock:
            self._operations.discard(thread_id.strip())

    async def publish_permission(self, thread_id: str, data: dict[str, Any]) -> None:
        stream = self.active(thread_id)
        if stream is None:
            raise ValueError("approval has no active turn")
        stream.publish("permission_request", data)

    async def publish_user_input(self, thread_id: str, data: dict[str, Any]) -> None:
        stream = self.active(thread_id)
        if stream is None:
            raise ValueError("user input request has no active turn")
        stream.publish("ask_user_question", data)

    async def publish_elicitation(
        self, thread_id: str, mode: str, data: dict[str, Any],
    ) -> None:
        stream = self.active(thread_id)
        if stream is None:
            raise ValueError("elicitation has no active turn")
        stream.publish(
            "ask_user_question" if mode == "form" else "permission_request",
            data,
        )

    async def interrupt(self, thread_id: str) -> bool:
        stream = self.active(thread_id)
        if stream is None or stream.turn_id is None:
            return False
        result = await self.runtime.request("turn/interrupt", {
            "threadId": thread_id,
            "turnId": stream.turn_id,
        })
        if result != {}:
            raise AppServerProtocolError("turn/interrupt returned an invalid result")
        return True

    async def close(self) -> None:
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        for stream in tuple(self._active.values()):
            if not stream.done:
                stream.publish("error", {
                    "error": "Codex runtime stopped",
                    "kind": "runtime_stopped",
                    "retryable": True,
                })
                stream.finish()
        self._active.clear()
        self._operations.clear()

    async def _pump(self, stream: TurnStream, subscription: EventSubscription) -> None:
        try:
            while not stream.done:
                notification = await subscription.next()
                if not _belongs_to_turn(notification, stream.turn_id):
                    continue
                terminal = _map_notification(stream, notification, self.usage)
                if terminal:
                    break
        except asyncio.CancelledError:
            raise
        except AppServerError as exc:
            stream.publish("error", {
                "error": str(exc),
                "kind": "runtime_unavailable",
                "retryable": True,
            })
        except Exception:
            stream.publish("error", {
                "error": "Invalid event received from Codex",
                "kind": "protocol_error",
                "retryable": False,
            })
        finally:
            await subscription.close()
            # Turn settlement changes updatedAt, preview, and sometimes the
            # generated title. Idle polls can use the 30s list cache, but the
            # first poll after a completed/interrupted turn must be fresh.
            self.threads.invalidate_list_cache()
            if not stream.done:
                stream.finish()
            if self._active.get(stream.thread_id) is stream:
                self._active.pop(stream.thread_id, None)
            if (self.on_turn_finished is not None
                    and stream.status == "completed"):
                try:
                    await self.on_turn_finished(stream.thread_id, stream.status)
                except Exception:
                    # Queue draining is an optional follow-up. It must never
                    # turn a completed native turn into an SSE failure.
                    pass


class TurnAlreadyActive(RuntimeError):
    pass


def _permission_overrides(
    permission: str,
    model: str,
    effort: str,
) -> dict[str, Any]:
    """Translate legacy UI values into explicit Codex-native turn settings."""
    if permission == "bypassPermissions":
        approval_policy = "never"
        sandbox_policy = {"type": "dangerFullAccess"}
    elif permission == "acceptEdits":
        approval_policy = "untrusted"
        sandbox_policy = {"type": "workspaceWrite"}
    elif permission == "plan":
        approval_policy = "on-request"
        sandbox_policy = {"type": "readOnly"}
    elif permission == "default":
        # Omit permission overrides so the thread keeps the approval and
        # sandbox policy inherited from the user's native Codex config.
        approval_policy = None
        sandbox_policy = None
    else:
        raise ValueError("unknown permission mode")

    result: dict[str, Any] = {}
    if approval_policy is not None:
        result["approvalPolicy"] = approval_policy
    if sandbox_policy is not None:
        result["sandboxPolicy"] = sandbox_policy
    if model:
        result["collaborationMode"] = {
            "mode": "plan" if permission == "plan" else "default",
            "settings": {
                "model": model,
                "reasoning_effort": effort or None,
                "developer_instructions": None,
            },
        }
    return result


def _turn_from_result(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict) or not isinstance(result.get("turn"), dict):
        raise AppServerProtocolError("turn/start returned an invalid turn")
    turn = result["turn"]
    if not isinstance(turn.get("id"), str) or not turn["id"]:
        raise AppServerProtocolError("turn/start returned a turn without an id")
    return turn


def _belongs_to_turn(notification: dict[str, Any], turn_id: str | None) -> bool:
    if turn_id is None:
        return True
    params = notification.get("params")
    if not isinstance(params, dict):
        return False
    event_turn_id = params.get("turnId")
    turn = params.get("turn")
    if event_turn_id is None and isinstance(turn, dict):
        event_turn_id = turn.get("id")
    return event_turn_id is None or event_turn_id == turn_id


def _map_notification(
    stream: TurnStream,
    notification: dict[str, Any],
    usage: CodexUsageService | None = None,
) -> bool:
    method = notification.get("method")
    params = notification.get("params")
    if not isinstance(method, str) or not isinstance(params, dict):
        return False

    if method == "thread/tokenUsage/updated":
        token_usage = params.get("tokenUsage")
        if usage is not None and isinstance(token_usage, dict):
            stream.session_usage = usage.update(
                stream.thread_id, token_usage, model=stream.model)
    elif method == "item/agentMessage/delta":
        _publish_delta(stream, "text", params)
    elif method in {"item/reasoning/summaryTextDelta", "item/reasoning/textDelta"}:
        _publish_delta(stream, "thinking", params)
    elif method == "item/started":
        item = params.get("item")
        if isinstance(item, dict) and _is_tool_item(item):
            stream.publish("tool_use", _tool_use(item))
    elif method == "item/completed":
        item = params.get("item")
        if isinstance(item, dict) and _is_tool_item(item):
            stream.publish("tool_result", _tool_result(item))
    elif method == "turn/completed":
        turn = params.get("turn")
        if not isinstance(turn, dict):
            raise AppServerProtocolError("turn/completed is missing turn")
        stream.status = str(turn.get("status") or "failed")
        failed = stream.status == "failed"
        data: dict[str, Any] = {
            "turn_id": stream.turn_id,
            "status": stream.status,
            "is_error": failed,
            "duration_ms": turn.get("durationMs"),
        }
        if failed:
            data["errors"] = [_turn_error(turn.get("error"))]
        if stream.session_usage is not None:
            data["session_usage"] = stream.session_usage
        stream.publish("done", data)
        stream.finish()
        return True
    return False


def _publish_delta(stream: TurnStream, event: str, params: dict[str, Any]) -> None:
    delta = params.get("delta")
    if isinstance(delta, str) and delta:
        stream.publish(event, {"text": delta})


def _is_tool_item(item: dict[str, Any]) -> bool:
    return item.get("type") in {
        "commandExecution",
        "fileChange",
        "mcpToolCall",
        "dynamicToolCall",
        "webSearch",
        "imageView",
        "imageGeneration",
        "collabAgentToolCall",
        "plan",
        "subAgentActivity",
        "sleep",
    }


def _tool_use(item: dict[str, Any]) -> dict[str, Any]:
    item_type = str(item.get("type") or "tool")
    names = {
        "commandExecution": "Bash",
        "fileChange": "FileChange",
        "mcpToolCall": str(item.get("tool") or "MCP"),
        "dynamicToolCall": str(item.get("tool") or "Tool"),
        "webSearch": "WebSearch",
        "imageView": "ViewImage",
        "imageGeneration": "ImageGeneration",
        "collabAgentToolCall": "Agent",
        "plan": "Plan",
        "subAgentActivity": "AgentActivity",
        "sleep": "Wait",
    }
    inputs = {
        key: item[key]
        for key in (
            "command", "cwd", "changes", "arguments", "query", "path", "prompt",
            "text", "agentPath", "agentThreadId", "kind", "durationMs",
        )
        if key in item
    }
    if item_type == "collabAgentToolCall":
        receivers = item.get("receiverThreadIds")
        receiver_ids = [thread_id for thread_id in receivers if isinstance(thread_id, str)] \
            if isinstance(receivers, list) else []
        inputs["receiver_thread_ids"] = receiver_ids
        return {
            "id": str(item.get("id") or ""),
            "name": "Agent",
            "summary": _tool_summary(item),
            "input": inputs,
            "task": {
                "subagent_type": str(item.get("tool") or "agent"),
                "description": str(item.get("prompt") or item.get("tool") or ""),
                "prompt": str(item.get("prompt") or ""),
                "thread_id": receiver_ids[0] if receiver_ids else "",
                "thread_ids": receiver_ids,
                "status": str(item.get("status") or ""),
            },
        }
    return {
        "id": str(item.get("id") or ""),
        "name": names.get(item_type, item_type),
        "summary": _tool_summary(item),
        "input": inputs,
    }


def _tool_result(item: dict[str, Any]) -> dict[str, Any]:
    text = _tool_result_text(item)
    status = str(item.get("status") or "completed")
    return {
        "id": str(item.get("id") or ""),
        "tool_name": _tool_use(item)["name"],
        "preview": text[:500],
        "text": text[:50_000],
        "truncated": len(text) > 50_000,
        "text_truncated": len(text) > 50_000,
        "is_error": status in {"failed", "declined"},
    }


def _tool_summary(item: dict[str, Any]) -> str:
    for key in ("command", "query", "path", "prompt", "text", "agentPath", "tool"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    if isinstance(item.get("changes"), list):
        return f"{len(item['changes'])} file change(s)"
    if item.get("type") == "sleep":
        return f"Wait {item.get('durationMs', 0)} ms"
    return str(item.get("type") or "Tool")


def _tool_result_text(item: dict[str, Any]) -> str:
    for key in ("aggregatedOutput", "result", "text"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    if item.get("type") == "subAgentActivity":
        return str(item.get("kind") or "Agent activity completed")
    if item.get("type") == "sleep":
        return f"Waited {item.get('durationMs', 0)} ms"
    return ""


def _turn_error(error: Any) -> str:
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message:
            return message
    return "Codex turn failed"
