"""Turn lifecycle and SSE-shape mapping tests."""

import asyncio

import pytest

from backend.codex import CodexTurnService, TurnAlreadyActive
from backend.codex.turns import (
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

    async def resume(self, thread_id, *, model=None):
        self.calls.append((thread_id, model))
        return {"id": thread_id, "turns": []}

    def mark_materialized(self, thread_id):
        self.materialized.append(thread_id)

    def invalidate_list_cache(self):
        pass


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
        effort="high")

    turn_params = next(params for method, params, _timeout in runtime.calls
                       if method == "turn/start")
    assert turn_params["effort"] == "high"
    assert "approvalPolicy" not in turn_params
    assert "sandboxPolicy" not in turn_params

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
    assert events.subscription.closed is True
    assert threads.calls == [("thread-1", "gpt-test")]

    replay = stream.subscribe()
    assert [(await replay.get())["event"] for _ in range(5)] == [
        "thinking", "tool_use", "text", "tool_result", "done",
    ]

    events.subscription = Subscription()
    await service.start("thread-1", "second", model="gpt-test")
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
