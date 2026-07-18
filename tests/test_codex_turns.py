"""Turn lifecycle and SSE-shape mapping tests."""

import asyncio
import time

import pytest

from backend.codex import CodexTurnService, TurnAlreadyActive
from backend.codex.turns import (
    TurnStream,
    _is_tool_item,
    _permission_overrides,
    _tool_result,
    _tool_use,
)


class Subscription:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.closed = False

    async def next(self):
        return await self.queue.get()

    async def close(self):
        self.closed = True


class Events:
    def __init__(self):
        self.subscription = Subscription()

    async def subscribe(self, _thread_id):
        return self.subscription


class Runtime:
    def __init__(self):
        self.calls = []

    def health(self):
        return type("Health", (), {"restart_count": 0})()

    async def request(self, method, params=None, *, timeout=None):
        self.calls.append((method, params, timeout))
        if method == "turn/start":
            return {"turn": {"id": "turn-1", "status": "inProgress", "items": []}}
        if method == "turn/interrupt":
            return {}
        raise AssertionError(method)


class Threads:
    def __init__(self):
        self.calls = []
        self.materialized = []
        self.summary = None
        self.tier = None

    async def resume(self, thread_id, *, model=None, service_tier=None):
        self.calls.append((thread_id, model))
        return {"id": thread_id, "turns": []}

    def mark_materialized(self, thread_id):
        self.materialized.append(thread_id)

    def invalidate_list_cache(self):
        pass

    def reasoning_summary(self, _thread_id):
        return self.summary

    def service_tier(self, _thread_id):
        return self.tier


class History:
    def __init__(self, degraded=False):
        self.is_degraded = degraded

    def degraded(self, _thread_id):
        return self.is_degraded


class Usage:
    def __init__(self):
        self.calls = []

    def update(self, thread_id, token_usage, *, model=""):
        self.calls.append((thread_id, token_usage, model))
        return {"context_used": 10, "context_limit": 100, "context_used_pct": 10.0}


@pytest.mark.asyncio
async def test_replay_compacts_deltas_without_delaying_live_subscribers():
    stream = TurnStream("thread-1", "hello", "gpt-test")
    live = stream.subscribe()

    stream.publish("text", {"text": "first "})
    stream.publish("text", {"text": "second"})
    stream.finish()

    assert await live.get() == {"event": "text", "data": {"text": "first "}}
    assert await live.get() == {"event": "text", "data": {"text": "second"}}
    assert stream.events == [
        {"event": "text", "data": {"text": "first second"}},
    ]

    replay = stream.subscribe()
    assert await replay.get() == {
        "event": "text",
        "data": {"text": "first second"},
    }


def test_replay_binary_compaction_handles_100k_single_byte_deltas():
    stream = TurnStream(
        "thread-stress", "hello", "gpt-test",
        replay_max_events=128, replay_max_bytes=512_000,
    )
    for _ in range(100_000):
        stream.publish("text", {"text": "x"})

    assert len(stream.events) <= 20
    assert sum(event["data"]["text"].count("x")
               for event in stream.events) == 100_000
    assert stream._replay_bytes <= 512_000


@pytest.mark.parametrize(("permission", "approval", "sandbox", "mode"), [
    ("default", None, None, "default"),
    ("acceptEdits", "untrusted", "workspaceWrite", "default"),
    ("plan", "on-request", "readOnly", "plan"),
    ("bypassPermissions", "never", "dangerFullAccess", "default"),
])
def test_permission_modes_map_to_explicit_codex_turn_settings(
    permission, approval, sandbox, mode,
):
    params = _permission_overrides(permission, "gpt-test", "high")

    if approval is None:
        assert "approvalPolicy" not in params
        assert "sandboxPolicy" not in params
    else:
        assert params["approvalPolicy"] == approval
        assert params["sandboxPolicy"] == {"type": sandbox}
    assert params["collaborationMode"] == {
        "mode": mode,
        "settings": {
            "model": "gpt-test",
            "reasoning_effort": "high",
            "developer_instructions": None,
        },
    }


def test_unknown_permission_mode_is_rejected():
    with pytest.raises(ValueError, match="unknown permission mode"):
        _permission_overrides("legacy-mode", "gpt-test", "")


@pytest.mark.asyncio
async def test_explicit_standard_tier_overrides_a_persisted_fast_choice():
    runtime = Runtime()
    threads = Threads()
    threads.tier = "fast"
    service = CodexTurnService(runtime, Events(), threads, History())

    await service.start("thread-1", "standard", service_tier="")
    params = next(
        value for method, value, _timeout in runtime.calls
        if method == "turn/start"
    )
    assert "serviceTier" in params
    assert params["serviceTier"] is None
    await service.close()


@pytest.mark.parametrize(("item_type", "name"), [
    ("plan", "Plan"),
    ("subAgentActivity", "AgentActivity"),
    ("sleep", "Wait"),
])
def test_native_progress_items_are_rendered_as_tool_cards(item_type, name):
    item = {"type": item_type, "id": f"{item_type}-1", "status": "completed"}
    if item_type == "plan":
        item["text"] = "1. Inspect\n2. Fix"
    elif item_type == "subAgentActivity":
        item.update({"agentPath": "reviewer", "agentThreadId": "child-1", "kind": "completed"})
    else:
        item["durationMs"] = 250

    assert _is_tool_item(item) is True
    assert _tool_use(item)["name"] == name
    assert _tool_result(item)["tool_name"] == name


@pytest.mark.asyncio
async def test_turn_maps_reasoning_tools_text_and_done_with_replay():
    runtime = Runtime()
    events = Events()
    threads = Threads()
    service = CodexTurnService(runtime, events, threads, History())
    stream = await service.start(
        "thread-1", "hello", model="gpt-test", permission="default",
        effort="high", service_tier="priority")
    # Make server-observed elapsed timing deterministic without sleeping.
    stream._started_monotonic = time.monotonic() - 2.5

    turn_params = next(params for method, params, _timeout in runtime.calls
                       if method == "turn/start")
    assert turn_params["effort"] == "high"
    assert turn_params["serviceTier"] == "priority"
    assert "approvalPolicy" not in turn_params
    assert "sandboxPolicy" not in turn_params

    threads.summary = "none"

    notifications = [
        ("item/reasoning/summaryTextDelta", {"delta": "think"}),
        ("item/started", {"item": {
            "id": "cmd-1", "type": "commandExecution", "command": "pwd",
            "cwd": "/tmp", "commandActions": [], "status": "inProgress",
        }}),
        ("item/agentMessage/delta", {"delta": "answer"}),
        ("item/completed", {"item": {
            "id": "cmd-1", "type": "commandExecution", "command": "pwd",
            "cwd": "/tmp", "commandActions": [], "status": "completed",
            "aggregatedOutput": "/tmp\n", "exitCode": 0,
        }}),
        ("turn/completed", {"turn": {
            "id": "turn-1", "status": "completed", "items": [], "durationMs": 8,
        }}),
    ]
    for method, extra in notifications:
        params = {"threadId": "thread-1", "turnId": "turn-1", **extra}
        await events.subscription.queue.put({"method": method, "params": params})

    for _ in range(100):
        if stream.done:
            break
        await asyncio.sleep(0.01)
    assert stream.done is True
    assert [event["event"] for event in stream.events] == [
        "thinking", "tool_use", "text", "tool_result", "done",
    ]
    assert stream.events[1]["data"]["name"] == "Bash"
    assert stream.events[-1]["data"]["duration_ms"] == 8
    assert stream.events[-1]["data"]["cancelled"] is False
    assert 2_500 <= stream.events[-1]["data"]["elapsed_ms"] < 3_000
    assert abs(stream.events[-1]["data"]["completed_at"] - time.time()) < 1
    assert events.subscription.closed is True
    assert threads.calls == [("thread-1", "gpt-test")]

    replay = stream.subscribe()
    assert [(await replay.get())["event"] for _ in range(5)] == [
        "thinking", "tool_use", "text", "tool_result", "done",
    ]

    events.subscription = Subscription()
    await service.start("thread-1", "second", model="gpt-test")
    second_params = [params for method, params, _timeout in runtime.calls
                     if method == "turn/start"][-1]
    assert second_params["summary"] == "none"
    assert threads.calls == [("thread-1", "gpt-test")]
    await service.close()


@pytest.mark.asyncio
async def test_interrupt_uses_both_native_ids():
    runtime = Runtime()
    events = Events()
    service = CodexTurnService(runtime, events, Threads(), History())
    await service.start("thread-1", "hello")

    assert await service.interrupt("thread-1") is True
    assert runtime.calls[-1] == ("turn/interrupt", {
        "threadId": "thread-1",
        "turnId": "turn-1",
    }, None)
    await service.close()


@pytest.mark.asyncio
async def test_degraded_history_still_resumes_original_thread():
    runtime = Runtime()
    threads = Threads()
    service = CodexTurnService(runtime, Events(), threads, History(degraded=True))

    stream = await service.start("thread-large", "hello")

    assert threads.calls == [("thread-large", None)]
    assert stream.thread_id == "thread-large"
    assert runtime.calls[-1][0] == "turn/start"
    await service.close()


@pytest.mark.asyncio
async def test_attachment_only_turn_uses_native_user_inputs():
    runtime = Runtime()
    service = CodexTurnService(runtime, Events(), Threads(), History())

    stream = await service.start(
        "thread-1",
        "",
        inputs=[
            {"type": "localImage", "path": "/workspace/image.png"},
            {"type": "mention", "name": "notes.md", "path": "/workspace/notes.md"},
        ],
        user_images=[{"mime": "image/png"}],
        user_docs=[{"name": "notes.md", "kind": "text"}],
        client_user_message_id="a" * 32,
    )

    assert runtime.calls[-1][1]["input"] == [
        {"type": "localImage", "path": "/workspace/image.png"},
        {"type": "mention", "name": "notes.md", "path": "/workspace/notes.md"},
    ]
    assert stream.user_images == [{"mime": "image/png"}]
    assert stream.user_docs == [{"name": "notes.md", "kind": "text"}]
    assert runtime.calls[-1][1]["clientUserMessageId"] == "a" * 32
    await service.close()


@pytest.mark.asyncio
async def test_turn_includes_native_usage_in_done_event():
    runtime = Runtime()
    events = Events()
    usage = Usage()
    service = CodexTurnService(runtime, events, Threads(), History(), usage)
    stream = await service.start("thread-1", "hello")

    await events.subscription.queue.put({
        "method": "thread/tokenUsage/updated",
        "params": {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "tokenUsage": {"last": {"totalTokens": 10}},
        },
    })
    await events.subscription.queue.put({
        "method": "turn/completed",
        "params": {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "turn": {"id": "turn-1", "status": "completed", "items": []},
        },
    })

    for _ in range(100):
        if stream.done:
            break
        await asyncio.sleep(0.01)
    assert usage.calls == [("thread-1", {"last": {"totalTokens": 10}}, "")]
    assert stream.events[-1]["data"]["session_usage"]["context_used"] == 10


@pytest.mark.asyncio
async def test_interrupted_turn_is_cancelled_without_success_followups():
    from backend import turn_notifications

    runtime = Runtime()
    events = Events()
    completed = []

    async def on_completed(thread_id, status):
        completed.append((thread_id, status))

    service = CodexTurnService(
        runtime, events, Threads(), History(), on_turn_finished=on_completed)
    stream = await service.start("thread-1", "stop")
    turn_notifications.record_turn_origin("thread-1", "mobile")
    await events.subscription.queue.put({
        "method": "turn/completed",
        "params": {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "turn": {"id": "turn-1", "status": "interrupted", "items": []},
        },
    })

    for _ in range(100):
        if stream.done and "thread-1" not in service._active:
            break
        await asyncio.sleep(0.01)
    done = stream.events[-1]
    assert done["event"] == "done"
    assert done["data"]["cancelled"] is True
    assert done["data"]["is_error"] is False
    assert completed == []
    assert "thread-1" not in turn_notifications._turn_origins


@pytest.mark.asyncio
async def test_turn_maps_native_collaboration_tool_to_subagent_card():
    runtime = Runtime()
    events = Events()
    service = CodexTurnService(runtime, events, Threads(), History())
    stream = await service.start("thread-1", "delegate")

    await events.subscription.queue.put({
        "method": "item/started",
        "params": {
            "threadId": "thread-1", "turnId": "turn-1",
            "item": {
                "id": "agent-1", "type": "collabAgentToolCall", "tool": "spawnAgent",
                "prompt": "Review the change", "receiverThreadIds": ["child-1"],
                "senderThreadId": "thread-1", "agentsStates": {}, "status": "inProgress",
            },
        },
    })
    await events.subscription.queue.put({
        "method": "turn/completed",
        "params": {"threadId": "thread-1", "turnId": "turn-1",
                   "turn": {"id": "turn-1", "status": "completed", "items": []}},
    })
    for _ in range(100):
        if stream.done:
            break
        await asyncio.sleep(0.01)

    event = next(event for event in stream.events if event["event"] == "tool_use")
    assert event["data"]["name"] == "Agent"
    assert event["data"]["task"]["thread_id"] == "child-1"
    await service.close()


@pytest.mark.asyncio
async def test_reserved_thread_operation_blocks_new_turns():
    service = CodexTurnService(Runtime(), Events(), Threads(), History())
    await service.begin_operation("thread-1")
    assert service.busy("thread-1") is True

    with pytest.raises(TurnAlreadyActive, match="operation"):
        await service.start("thread-1", "hello")

    await service.end_operation("thread-1")
    assert service.busy("thread-1") is False
    await service.start("thread-1", "hello")
    assert service.busy("thread-1") is True
    await service.close()


@pytest.mark.asyncio
async def test_turn_stream_bounds_replay_and_isolates_slow_subscriber():
    stream = TurnStream(
        "thread-1", "hello", "gpt-test",
        replay_max_events=1, replay_max_bytes=4096,
        subscriber_max_events=1, subscriber_max_bytes=4096,
    )
    slow = stream.subscribe()
    stream.publish("tool_use", {"id": "one"})
    stream.publish("tool_result", {"id": "two"})

    assert await slow.get() == {
        "event": "resync",
        "data": {"reason": "slow_subscriber", "retryable": True},
    }
    replay = stream.subscribe()
    assert await replay.get() == {
        "event": "resync",
        "data": {"reason": "replay_truncated", "retryable": True},
    }
    assert stream.events == []


class PerThreadEvents:
    async def subscribe(self, _thread_id):
        return Subscription()


class CoordinatedRuntime:
    generation = 1

    def __init__(self):
        self.entered = 0
        self.inflight = 0
        self.max_inflight = 0
        self.two_entered = asyncio.Event()
        self.release = asyncio.Event()

    def health(self):
        return type("Health", (), {"restart_count": 0})()

    async def request(self, method, params=None, *, timeout=None):
        assert method == "turn/start"
        self.entered += 1
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        if self.entered >= 2:
            self.two_entered.set()
        await self.release.wait()
        self.inflight -= 1
        return {"turn": {
            "id": f"turn-{self.entered}", "status": "inProgress", "items": [],
        }}


@pytest.mark.asyncio
async def test_turn_coordinator_parallelizes_threads_but_serializes_one_thread():
    runtime = CoordinatedRuntime()
    service = CodexTurnService(runtime, PerThreadEvents(), Threads(), History())
    different = [
        asyncio.create_task(service.start("thread-1", "one")),
        asyncio.create_task(service.start("thread-2", "two")),
    ]
    await asyncio.wait_for(runtime.two_entered.wait(), 1)
    assert runtime.max_inflight == 2
    runtime.release.set()
    await asyncio.gather(*different)
    await service.close()

    runtime = CoordinatedRuntime()
    service = CodexTurnService(runtime, PerThreadEvents(), Threads(), History())
    first = asyncio.create_task(service.start("same", "one"))
    while runtime.entered < 1:
        await asyncio.sleep(0)
    second = asyncio.create_task(service.start("same", "two"))
    await asyncio.sleep(0)
    assert runtime.entered == 1
    runtime.release.set()
    await first
    with pytest.raises(TurnAlreadyActive):
        await second
    assert runtime.max_inflight == 1
    await service.close()
