"""Codex turn lifecycle, replay buffer, and UI-neutral event mapping."""

from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from collections.abc import Awaitable, Callable
from typing import Any
from weakref import WeakValueDictionary

from .event_router import (
    CodexEventRouter,
    EventSubscription,
    EventSubscriptionResyncRequired,
)
from .history import CodexHistoryService
from .process import AppServerError, AppServerProtocolError
from .runtime import CodexRuntime
from .threads import CodexThreadService, normalize_service_tier
from .usage import CodexUsageService


_STREAM_CLOSED = object()
_STREAM_REPLAY_MAX_EVENTS = 512
_STREAM_REPLAY_MAX_BYTES = 2 * 1024 * 1024
_STREAM_SUBSCRIBER_MAX_EVENTS = 256
_STREAM_SUBSCRIBER_MAX_BYTES = 1024 * 1024
_LOADED_THREADS_MAX = 2048
_CANONICAL_POLL_SECONDS = 2.0
_TERMINAL_TURN_STATUSES = frozenset({
    "completed", "failed", "interrupted", "cancelled",
})


def _envelope_size(envelope: dict[str, Any]) -> int:
    try:
        return len(json.dumps(
            envelope, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8"))
    except (TypeError, ValueError):
        return _STREAM_SUBSCRIBER_MAX_BYTES + 1


class _TurnSubscriber:
    """Byte- and item-bounded live SSE handoff for one HTTP client."""

    def __init__(self, max_events: int, max_bytes: int):
        self._queue: asyncio.Queue = asyncio.Queue()
        self._max_events = max_events
        self._max_bytes = max_bytes
        self._pending_bytes = 0
        self._accepting = True

    async def get(self):
        item = await self._queue.get()
        if isinstance(item, tuple) and len(item) == 2:
            envelope, size = item
            self._pending_bytes = max(0, self._pending_bytes - int(size))
            return envelope
        return item

    def publish(self, envelope: dict[str, Any]) -> bool:
        if not self._accepting:
            return False
        size = _envelope_size(envelope)
        if (self._queue.qsize() >= self._max_events
                or self._pending_bytes + size > self._max_bytes):
            self.resync("slow_subscriber")
            return False
        self._pending_bytes += size
        self._queue.put_nowait((envelope, size))
        return True

    def replay(self, envelope: dict[str, Any]) -> bool:
        return self.publish({"event": envelope["event"], "data": dict(envelope["data"])})

    def resync(self, reason: str) -> None:
        if not self._accepting:
            return
        self._accepting = False
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._pending_bytes = 0
        self._queue.put_nowait({
            "event": "resync",
            "data": {"reason": reason, "retryable": True},
        })
        self._queue.put_nowait(_STREAM_CLOSED)

    def close(self) -> None:
        if self._accepting:
            self._accepting = False
            self._queue.put_nowait(_STREAM_CLOSED)


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
    replay_max_events: int = _STREAM_REPLAY_MAX_EVENTS
    replay_max_bytes: int = _STREAM_REPLAY_MAX_BYTES
    subscriber_max_events: int = _STREAM_SUBSCRIBER_MAX_EVENTS
    subscriber_max_bytes: int = _STREAM_SUBSCRIBER_MAX_BYTES
    # Relative timing must use a monotonic clock. ``started_at`` remains the
    # wall-clock epoch exposed for diagnostics/deduplication, but subtracting
    # it from a browser's Date.now() is invalid when browser and server run on
    # different devices with slightly different clocks.
    _started_monotonic: float = field(default_factory=time.monotonic, repr=False)
    _subscribers: set[_TurnSubscriber] = field(default_factory=set, repr=False)
    _replay_bytes: int = field(default=0, repr=False)
    _replay_truncated: bool = field(default=False, repr=False)

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, time.monotonic() - self._started_monotonic)

    def publish(self, event: str, data: dict[str, Any]) -> None:
        envelope = {"event": event, "data": data}
        # Live subscribers need every delta for low-latency rendering, while
        # reconnects only need the exact accumulated text.  Keeping thousands
        # of one-token envelopes for a long answer wastes substantially more
        # memory than the text itself, so compact only consecutive, plain-text
        # deltas in the replay buffer.  Use a separate envelope so extending
        # the replay copy never mutates an event already queued to a client.
        replay = {"event": event, "data": dict(data)}
        self.events.append(replay)
        self._replay_bytes += _envelope_size(replay)
        compactable = (
            event in {"text", "thinking"}
            and set(data) == {"text"}
            and isinstance(data.get("text"), str)
        )
        if compactable:
            # Binary compaction keeps replay exact without the O(n²) copying
            # of repeatedly appending every token to one ever-growing string.
            # One-byte deltas form power-of-two chunks, so 100k deltas retain
            # only O(log n) replay envelopes and each byte is copied O(log n).
            while len(self.events) >= 2:
                left = self.events[-2]
                right = self.events[-1]
                left_data = left.get("data") if isinstance(left, dict) else None
                right_data = right.get("data") if isinstance(right, dict) else None
                if (
                    not isinstance(left_data, dict)
                    or not isinstance(right_data, dict)
                    or left.get("event") != event
                    or right.get("event") != event
                    or set(left_data) != {"text"}
                    or set(right_data) != {"text"}
                    or not isinstance(left_data.get("text"), str)
                    or not isinstance(right_data.get("text"), str)
                    or len(left_data["text"]) > len(right_data["text"])
                ):
                    break
                merged = {
                    "event": event,
                    "data": {"text": left_data["text"] + right_data["text"]},
                }
                self._replay_bytes -= (
                    _envelope_size(left) + _envelope_size(right))
                self.events[-2:] = [merged]
                self._replay_bytes += _envelope_size(merged)
        if (len(self.events) > self.replay_max_events
                or self._replay_bytes > self.replay_max_bytes):
            # Partial replay is more dangerous than no replay: it can append
            # half a tool/result or assistant response to canonical history.
            # Keep live delivery intact, discard the reconnect copy, and make
            # every later subscriber explicitly reload canonical history.
            self.events.clear()
            self._replay_bytes = 0
            self._replay_truncated = True
        for subscriber in tuple(self._subscribers):
            if not subscriber.publish(envelope):
                self._subscribers.discard(subscriber)

    def finish(self) -> None:
        self.done = True
        for subscriber in tuple(self._subscribers):
            subscriber.close()
        self._subscribers.clear()

    def subscribe(self) -> _TurnSubscriber:
        subscriber = _TurnSubscriber(
            self.subscriber_max_events, self.subscriber_max_bytes)
        if self._replay_truncated:
            subscriber.resync("replay_truncated")
            return subscriber
        for event in self.events:
            if not subscriber.replay(event):
                return subscriber
        if self.done:
            subscriber.close()
        else:
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: _TurnSubscriber) -> None:
        self._subscribers.discard(subscriber)
        subscriber.close()


class CodexTurnService:
    """Start, stream, approve, and interrupt one active turn per thread."""

    def __init__(
        self,
        runtime: CodexRuntime,
        events: CodexEventRouter,
        threads: CodexThreadService,
        history: CodexHistoryService | None = None,
        usage: CodexUsageService | None = None,
        on_turn_started: Callable[[str, str], Awaitable[None]] | None = None,
        on_turn_settled: Callable[[str, str], Awaitable[None]] | None = None,
        on_turn_finished: Callable[[str, str], Awaitable[None]] | None = None,
    ):
        self.runtime = runtime
        self.events = events
        self.threads = threads
        self.history = history
        self.usage = usage
        self.on_turn_started = on_turn_started
        self.on_turn_settled = on_turn_settled
        self.on_turn_finished = on_turn_finished
        self._active: dict[str, TurnStream] = {}
        self._operations: set[str] = set()
        self._loaded_generation: OrderedDict[str, int] = OrderedDict()
        self._loaded_runtime_generation: int | None = None
        self._external_active: dict[str, str] = {}
        self._tasks: set[asyncio.Task] = set()
        self._thread_locks: WeakValueDictionary[str, asyncio.Lock] = (
            WeakValueDictionary())

    def _coordinator(self, thread_id: str) -> asyncio.Lock:
        lock = self._thread_locks.get(thread_id)
        if lock is None:
            lock = asyncio.Lock()
            self._thread_locks[thread_id] = lock
        return lock

    def _runtime_generation(self) -> int:
        generation = getattr(self.runtime, "generation", None)
        if isinstance(generation, int):
            return generation
        return int(getattr(self.runtime.health(), "restart_count", 0))

    def _current_generation(self) -> int:
        generation = self._runtime_generation()
        if (self._loaded_runtime_generation is not None
                and self._loaded_runtime_generation != generation):
            self._loaded_generation.clear()
        self._loaded_runtime_generation = generation
        return generation

    def _remember_loaded(self, thread_id: str) -> None:
        generation = self._current_generation()
        self._loaded_generation[thread_id] = generation
        self._loaded_generation.move_to_end(thread_id)
        while len(self._loaded_generation) > _LOADED_THREADS_MAX:
            self._loaded_generation.popitem(last=False)

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
        service_tier: str | None = None,
        inputs: list[dict[str, Any]] | None = None,
        user_images: list[dict[str, Any]] | None = None,
        user_docs: list[dict[str, Any]] | None = None,
        client_user_message_id: str | None = None,
        _reserved_operation: bool = False,
    ) -> TurnStream:
        clean_id = thread_id.strip()
        clean_prompt = prompt.strip()
        extra_inputs = list(inputs or [])
        if not clean_id:
            raise ValueError("thread id cannot be empty")
        if not clean_prompt and not extra_inputs:
            raise ValueError("prompt and attachments cannot both be empty")
        effective_service_tier = normalize_service_tier(service_tier)
        if effective_service_tier is None:
            effective_service_tier = self.threads.service_tier(clean_id)

        async with self._coordinator(clean_id):
            existing = self._active.get(clean_id)
            if existing is not None and not existing.done:
                raise TurnAlreadyActive("previous turn still running")
            if clean_id in self._external_active:
                raise TurnAlreadyActive("native turn status requires resync")
            if _reserved_operation:
                if clean_id not in self._operations:
                    raise RuntimeError("thread operation was not reserved")
                self._operations.discard(clean_id)
            elif clean_id in self._operations:
                raise TurnAlreadyActive("thread operation still running")
            generation = self._current_generation()
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
                if effective_service_tier is not None:
                    resume_kwargs["service_tier"] = effective_service_tier
                await self.threads.resume(clean_id, **resume_kwargs)
                self._remember_loaded(clean_id)
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
            reasoning_summary = self.threads.reasoning_summary(clean_id)
            if reasoning_summary is not None:
                params["summary"] = reasoning_summary
            if effective_service_tier is not None:
                # Native ``null`` means Standard and is distinct from omission,
                # which inherits whatever tier another Codex surface selected.
                params["serviceTier"] = effective_service_tier or None
            if model:
                params["model"] = model
            if effort:
                params["effort"] = effort
            if client_user_message_id:
                params["clientUserMessageId"] = client_user_message_id
            try:
                result = await self.runtime.request("turn/start", params)
                turn = _turn_from_result(result)
                stream.turn_id = turn["id"]
                stream.status = str(turn.get("status") or "inProgress")
            except BaseException:
                self._active.pop(clean_id, None)
                await subscription.close()
                raise
            try:
                # Native success is the commit point.  A secondary sidecar
                # fsync failure must not make the caller retry an already
                # accepted turn.
                self.threads.mark_materialized(clean_id)
            except OSError:
                pass
            task = asyncio.create_task(
                self._pump(stream, subscription),
                name=f"codex-turn-{stream.turn_id}",
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            if self.on_turn_started is not None:
                try:
                    await self.on_turn_started(clean_id, clean_prompt[:500])
                except Exception:
                    pass
            return stream

    def active(self, thread_id: str) -> TurnStream | None:
        stream = self._active.get(thread_id)
        return stream if stream is not None and not stream.done else None

    def busy(self, thread_id: str) -> bool:
        return (self.active(thread_id) is not None
                or thread_id in self._operations
                or thread_id in self._external_active)

    async def begin_operation(
        self,
        thread_id: str,
        *,
        model: str = "",
        ensure_loaded: bool = True,
    ) -> None:
        clean_id = thread_id.strip()
        if not clean_id:
            raise ValueError("thread id cannot be empty")
        async with self._coordinator(clean_id):
            stream = self._active.get(clean_id)
            if stream is not None and not stream.done:
                raise TurnAlreadyActive("previous turn still running")
            if clean_id in self._external_active:
                raise TurnAlreadyActive("native turn status requires resync")
            if clean_id in self._operations:
                raise TurnAlreadyActive("thread operation still running")
            self._operations.add(clean_id)
            try:
                generation = self._current_generation()
                if (ensure_loaded
                        and self._loaded_generation.get(clean_id) != generation):
                    await self.threads.resume(clean_id, model=model or None)
                    self._remember_loaded(clean_id)
            except BaseException:
                self._operations.discard(clean_id)
                raise

    async def end_operation(self, thread_id: str) -> None:
        clean_id = thread_id.strip()
        async with self._coordinator(clean_id):
            self._operations.discard(clean_id)

    def forget_thread(self, thread_id: str) -> None:
        clean_id = thread_id.strip()
        self._loaded_generation.pop(clean_id, None)
        self._external_active.pop(clean_id, None)

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
        clean_id = thread_id.strip()
        async with self._coordinator(clean_id):
            stream = self.active(clean_id)
            if stream is None or stream.turn_id is None:
                return False
            result = await self.runtime.request("turn/interrupt", {
                "threadId": clean_id,
                "turnId": stream.turn_id,
            })
            if result != {}:
                raise AppServerProtocolError(
                    "turn/interrupt returned an invalid result")
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
        except EventSubscriptionResyncRequired:
            stream.status = "resyncRequired"
            stream.publish("resync", {
                "reason": "event_subscriber_overflow",
                "retryable": True,
            })
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
            if self.on_turn_settled is not None:
                try:
                    await self.on_turn_settled(stream.thread_id, stream.status)
                except Exception:
                    pass
            if self.on_turn_finished is not None and stream.status == "completed":
                try:
                    await self.on_turn_finished(stream.thread_id, stream.status)
                except Exception:
                    # Activity/notification/queue follow-ups are optional and
                    # must never turn a settled native turn into an SSE error.
                    pass
            if stream.status != "completed":
                # Failed/interrupted turns skip the success callback (no push,
                # no queue drain), but their device-origin entry must not leak.
                from ..turn_notifications import clear_turn_origin
                clear_turn_origin(stream.thread_id)


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
        interrupted = stream.status == "interrupted"
        data: dict[str, Any] = {
            "turn_id": stream.turn_id,
            "status": stream.status,
            "is_error": failed,
            "cancelled": interrupted,
            "duration_ms": turn.get("durationMs"),
            # Browser timers can be throttled/suspended while a phone is in
            # the background. Preserve server-observed completion timing so
            # the footer does not stamp the wake-up time or a stale tick.
            "elapsed_ms": round(stream.elapsed_seconds * 1000),
            "completed_at": time.time(),
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
