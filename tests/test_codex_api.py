"""HTTP/SSE compatibility slice backed by the deterministic app-server."""

# ruff: noqa: E402 -- backend.settings validates env during import.

import asyncio
import os
import sys
import io
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

# backend.settings validates this at import time. The test overrides the auth
# dependencies, but still supplies a non-secret fixture value for collection.
os.environ.setdefault("MUSELAB_TOKEN", "test-token-1234567890abcdef-secure-min-32")
os.environ.setdefault("MUSELAB_ROOT", "/tmp/muselab-codex-api-tests")
Path(os.environ["MUSELAB_ROOT"]).mkdir(parents=True, exist_ok=True)

from backend.auth import require_token, require_token_header_or_query
from backend.codex import (
    CodexAppServer,
    CodexAttachmentService,
    CodexApprovalBroker,
    CodexCompactService,
    CodexClientRequestRouter,
    CodexEventRouter,
    CodexElicitationBroker,
    CodexHistoryService,
    CodexMcpService,
    CodexProviderService,
    CodexRuntime,
    CodexSkillsService,
    CodexThreadService,
    CodexTurnService,
    CodexUsageService,
    CodexUserInputBroker,
    CodexQueueService,
    CodexQueueDrainService,
)
from backend.codex.api import (
    QueueEnqueueRequest,
    _session_meta,
    _stream_ticket_events,
    _thread_messages,
    _thread_outline,
    active_session,
    codex_rate_limit,
    enqueue_queue,
    interrupt as interrupt_session,
    router,
    session_usage,
)
from backend.codex.settings_api import router as settings_router


FAKE_SERVER = Path(__file__).parent / "fixtures" / "fake_codex_app_server.py"
PNG_BYTES = b"\x89PNG\r\n\x1a\nfixture-image"


def test_stream_flushes_ping_before_touching_native_runtime():
    """The SSE response must open before a slow native turn/start await."""

    async def first_event():
        generator = _stream_ticket_events(None, {})
        try:
            return await anext(generator)
        finally:
            await generator.aclose()

    event = asyncio.run(first_event())

    assert event.event == "ping"
    assert event.data == ""


def test_enqueue_can_atomically_drain_when_busy_state_went_stale():
    queue = CodexQueueService()

    class Drain:
        calls = []

        async def drain(self, thread_id):
            self.calls.append(thread_id)
            assert queue.take_next(thread_id)["text"] == "late follow-up"
            return True

    attachments = SimpleNamespace(describe_staged=lambda _ids: [])
    drain = Drain()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        codex_queue=queue,
        codex_queue_drain=drain,
        codex_attachments=attachments,
    )))

    result = asyncio.run(enqueue_queue(
        request,
        "thread-race",
        QueueEnqueueRequest(text="late follow-up", drain_if_idle=True),
    ))

    assert result["started"] is True
    assert result["items"] == []
    assert drain.calls == ["thread-race"]


def test_active_session_exposes_clock_skew_safe_relative_elapsed():
    stream = SimpleNamespace(
        turn_id="turn-timing",
        model="gpt-test",
        started_at=1_700_000_000.0,
        elapsed_seconds=7.25,
        events=[{"event": "thinking"}],
        user_text="hello",
        user_images=[],
        user_docs=[],
    )
    turns = SimpleNamespace(active=lambda thread_id: (
        stream if thread_id == "thread-timing" else None
    ))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        codex_threads=object(), codex_turns=turns,
    )))

    result = asyncio.run(active_session(request, "thread-timing"))

    assert result["started_at"] == 1_700_000_000.0
    assert result["elapsed_seconds"] == 7.25
    assert result["turn_id"] == "turn-timing"


def test_interrupt_pauses_pending_queue_before_interrupting_turn():
    calls = []

    class QueueStub:
        def get(self, thread_id):
            calls.append(("get", thread_id))
            return {"items": [{"id": "queued-1"}], "paused": False}

        def pause(self, thread_id, paused):
            calls.append(("pause", thread_id, paused))
            return {"items": [{"id": "queued-1"}], "paused": paused}

    class TurnsStub:
        async def interrupt(self, thread_id):
            calls.append(("interrupt", thread_id))
            return True

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        codex_threads=object(),
        codex_turns=TurnsStub(),
        codex_queue=QueueStub(),
    )))

    result = asyncio.run(interrupt_session(request, "thread-stop"))

    assert calls == [
        ("get", "thread-stop"),
        ("pause", "thread-stop", True),
        ("interrupt", "thread-stop"),
    ]
    assert result == {
        "ok": True,
        "interrupted": ["thread-stop"],
        "queue_paused": True,
    }


def test_session_meta_uses_native_preview_for_legacy_timestamp_placeholder():
    turns = SimpleNamespace(active=lambda _thread_id: None)
    meta = _session_meta({
        "id": "thread-auto-title",
        "name": "新会话 07-11 11:48",
        "preview": "Fix the usage dashboard",
        "createdAt": 1,
        "updatedAt": 2,
        "turns": [],
    }, turns)

    assert meta["name"] == "Fix the usage dashboard"
    assert meta["auto_named"] is True


def test_session_meta_preserves_explicit_user_name_over_preview():
    turns = SimpleNamespace(active=lambda _thread_id: None)
    meta = _session_meta({
        "id": "thread-explicit-title",
        "name": "Release checklist",
        "preview": "Fix the usage dashboard",
        "createdAt": 1,
        "updatedAt": 2,
        "turns": [],
    }, turns)

    assert meta["name"] == "Release checklist"
    assert meta["auto_named"] is False


def test_duplicate_codex_user_ids_get_stable_unique_message_and_outline_ids():
    thread = {
        "id": "thread-duplicate-user-ids",
        "turns": [{"items": [
            {"type": "userMessage", "id": "turn-1", "content": [
                {"type": "text", "text": "first"},
            ]},
            {"type": "agentMessage", "text": "reply"},
            {"type": "userMessage", "id": "turn-1", "content": [
                {"type": "text", "text": "second"},
            ]},
        ]}],
    }

    messages = _thread_messages(thread)
    outline = _thread_outline(thread)

    assert [message["uuid"] for message in messages if message["role"] == "user"] == [
        "turn-1", "turn-1-2",
    ]
    assert [item["uuid"] for item in outline] == ["turn-1", "turn-1-2"]


def test_injected_user_context_is_hidden_inside_one_native_turn():
    thread = {
        "id": "thread-injected-context",
        "turns": [{"items": [
            {"type": "userMessage", "id": "turn-1", "content": [
                {"type": "text", "text": "<recommended_plugins>hidden"},
            ]},
            {"type": "userMessage", "id": "turn-1", "content": [
                {"type": "text", "text": "visible prompt"},
            ]},
            {"type": "agentMessage", "text": "reply"},
        ]}],
    }

    assert [message["text"] for message in _thread_messages(thread)] == [
        "visible prompt", "reply",
    ]
    assert [item["preview"] for item in _thread_outline(thread)] == [
        "visible prompt",
    ]


def test_projected_tool_items_become_ui_tool_messages():
    thread = {
        "id": "thread-tools",
        "turns": [{"items": [
            {
                "type": "toolUse", "id": "call-1", "name": "ApplyPatch",
                "summary": "Apply code changes", "input": {"code": "patch"},
            },
            {
                "type": "toolResult", "id": "call-1",
                "toolName": "ApplyPatch", "preview": "ok", "text": "ok",
                "truncated": False, "isError": False,
            },
        ]}],
    }

    assert _thread_messages(thread) == [
        {
            "role": "tool_use", "id": "call-1", "name": "ApplyPatch",
            "summary": "Apply code changes", "input": {"code": "patch"},
        },
        {
            "role": "tool_result", "id": "call-1", "tool_use_id": "call-1",
            "tool_name": "ApplyPatch", "preview": "ok", "text": "ok",
            "truncated": False, "text_truncated": False, "is_error": False,
        },
    ]


def test_native_thread_items_survive_history_projection():
    thread = {
        "id": "thread-native-tools",
        "turns": [{"items": [
            {
                "type": "commandExecution", "id": "cmd-1",
                "command": "pwd", "cwd": "/workspace",
                "commandActions": [], "status": "completed",
                "aggregatedOutput": "/workspace\n", "exitCode": 0,
            },
            {
                "type": "fileChange", "id": "patch-1",
                "changes": [{"path": "/workspace/app.py", "kind": "update"}],
                "status": "completed",
            },
            {
                "type": "mcpToolCall", "id": "mcp-1",
                "server": "docs", "tool": "search", "arguments": {"q": "Codex"},
                "status": "inProgress",
            },
        ]}],
    }

    messages = _thread_messages(thread)

    assert [(message["role"], message.get("id")) for message in messages] == [
        ("tool_use", "cmd-1"),
        ("tool_result", "cmd-1"),
        ("tool_use", "patch-1"),
        ("tool_result", "patch-1"),
        ("tool_use", "mcp-1"),
    ]
    assert messages[0] == {
        "role": "tool_use", "id": "cmd-1", "name": "Bash",
        "summary": "pwd", "input": {"command": "pwd", "cwd": "/workspace"},
    }
    assert messages[1]["tool_name"] == "Bash"
    assert messages[1]["text"] == "/workspace\n"
    assert messages[1]["tool_use_id"] == "cmd-1"
    assert messages[2]["name"] == "FileChange"
    assert messages[4]["name"] == "search"


def native_app(workspace: Path) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        approvals = CodexApprovalBroker(timeout=1)
        user_input = CodexUserInputBroker(timeout=1)
        elicitation = CodexElicitationBroker(timeout=1)
        client_requests = CodexClientRequestRouter(
            approvals, user_input, elicitation)
        runtime = CodexRuntime(lambda: CodexAppServer(
            command=(sys.executable, str(FAKE_SERVER), "normal"),
            request_handler=client_requests.handle,
            initialize_capabilities={"experimentalApi": True},
        ))
        events = CodexEventRouter(runtime)
        app.state.codex_runtime = runtime
        app.state.codex_attachments = CodexAttachmentService(workspace)
        app.state.codex_usage = CodexUsageService(workspace)
        app.state.codex_queue = CodexQueueService()
        app.state.codex_threads = CodexThreadService(runtime, workspace)
        app.state.codex_mcp = CodexMcpService(runtime, workspace)
        app.state.codex_providers = CodexProviderService(runtime, workspace)
        app.state.codex_skills = CodexSkillsService(runtime, workspace)
        app.state.codex_approvals = approvals
        app.state.codex_user_input = user_input
        app.state.codex_elicitation = elicitation
        app.state.codex_client_requests = client_requests
        app.state.codex_events = events
        app.state.codex_history = CodexHistoryService(
            app.state.codex_threads, runtime, events, timeout=1)
        turns = CodexTurnService(
            runtime,
            events,
            app.state.codex_threads,
            app.state.codex_history,
            app.state.codex_usage,
        )
        approvals.publisher = turns.publish_permission
        user_input.publisher = turns.publish_user_input
        elicitation.publisher = turns.publish_elicitation
        app.state.codex_turns = turns
        app.state.codex_queue_drain = CodexQueueDrainService(
            app.state.codex_queue, turns, app.state.codex_attachments)
        app.state.codex_compact = CodexCompactService(
            runtime, events, turns, app.state.codex_usage, timeout=1)
        await runtime.start()
        await events.start()
        try:
            yield
        finally:
            await turns.close()
            await events.close()
            await approvals.close()
            await user_input.close()
            await elicitation.close()
            await runtime.close()

    app = FastAPI(lifespan=lifespan)
    app.include_router(router)
    app.include_router(settings_router)
    app.dependency_overrides[require_token] = lambda: None
    app.dependency_overrides[require_token_header_or_query] = lambda: None
    return app


def test_native_session_model_and_stream_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    app = native_app(tmp_path)
    with TestClient(app) as client:
        workspaces = client.get("/api/chat/workspaces")
        assert workspaces.status_code == 200
        assert workspaces.json()["workspaces"] == [{
            "path": str(tmp_path.resolve()),
            "name": tmp_path.name,
            "primary": True,
        }]
        extra_workspace = tmp_path / "second-project"
        extra_workspace.mkdir()
        (extra_workspace / "pyproject.toml").write_text(
            "[project]\nname='fixture'\n", encoding="utf-8")
        browser = client.get(
            "/api/chat/workspaces/browse", params={"path": str(tmp_path)})
        assert browser.status_code == 200
        candidate = next(
            item for item in browser.json()["directories"]
            if item["path"] == str(extra_workspace.resolve()))
        assert candidate == {
            "path": str(extra_workspace.resolve()),
            "name": "second-project",
            "registered": False,
            "selectable": True,
            "project": "Python",
        }
        registered = client.post("/api/chat/workspaces", json={
            "path": str(extra_workspace), "name": "Second project",
        })
        assert registered.status_code == 200
        assert registered.json()["path"] == str(extra_workspace.resolve())

        models = client.get("/api/chat/providers")
        assert models.status_code == 200
        assert models.json()["default_model"] == "gpt-test-codex"
        assert models.json()["runtime"] == "codex"
        assert models.json()["models"][0]["reasoning_efforts"] == ["medium"]
        assert models.json()["models"][0]["supports_fast"] is True
        assert models.json()["models"][0]["fast_service_tier"] == "priority"
        assert models.json()["models"][0]["service_tiers"] == [{
            "id": "priority",
            "name": "Fast",
            "description": "1.5x speed, increased usage",
        }]

        created = client.post("/api/chat/sessions", json={"name": "source"})
        custom_created = client.post("/api/chat/sessions", json={
            "name": "custom cwd", "cwd": str(extra_workspace),
        })
        assert custom_created.status_code == 200
        assert custom_created.json()["cwd"] == str(extra_workspace.resolve())
        forked = client.post(
            f"/api/chat/sessions/{custom_created.json()['id']}/fork",
            json={
                "model": "gpt-test-codex",
                "effort": "medium",
                "service_tier": "priority",
            },
        )
        assert forked.status_code == 200
        assert forked.json()["id"] != custom_created.json()["id"]
        assert forked.json()["cwd"] == str(extra_workspace.resolve())
        assert forked.json()["effort"] == "medium"
        assert forked.json()["service_tier"] == "priority"

        context = client.get("/api/chat/context-info")
        assert context.status_code == 200
        assert context.json()["runtime"] == "codex"
        assert context.json()["has_any_provider"] is True
        assert context.json()["instructions_source"] == "AGENTS.md"
        assert context.json()["instructions_available"] is False
        assert context.json()["instructions_exists"] is False
        assert context.json()["global_instructions_available"] is False
        assert context.json()["workspace_instructions_available"] is False

        settings = client.get("/api/settings")
        assert settings.status_code == 200
        assert settings.json()["runtime"] == "codex"
        assert settings.json()["providers"] == []
        assert settings.json()["defaults"]["model"] == "gpt-test-codex"

        native_providers = client.get("/api/settings/providers")
        assert native_providers.status_code == 200
        assert [provider["id"] for provider in native_providers.json()["providers"]] == [
            "minimax", "qwen", "mimo",
        ]
        enabled = client.patch("/api/settings/providers/minimax", json={"enabled": True})
        assert enabled.status_code == 200
        assert enabled.json()["providers"][0]["enabled"] is True
        models = client.get("/api/chat/providers").json()["models"]
        assert any(model["model"] == "minimax-m2.7" and model["provider"] == "minimax"
                   for model in models)

        native_created = client.post("/api/chat/sessions", json={
            "name": "MiniMax native", "model": "minimax-m2.7",
        })
        assert native_created.status_code == 200
        assert native_created.json()["model_provider"] == "minimax"

        usage = client.get("/api/chat/usage")
        dashboard = client.get(
            "/api/chat/cost-dashboard?days=30&tz_offset_minutes=480")
        assert dashboard.status_code == 200
        assert dashboard.json()["runtime"] == "codex"
        assert len(dashboard.json()["by_day"]) == 30
        assert usage.status_code == 200
        assert usage.json()["runtime"] == "codex"
        assert usage.json()["cost_available"] is False

        limits = client.get("/api/chat/codex-rate-limit")
        assert limits.status_code == 200
        assert limits.json()["provider_authoritative"] is False

        queued_upload = client.post(
            "/api/chat/upload-image",
            files={"file": ("queued.png", PNG_BYTES, "image/png")},
        )
        assert queued_upload.status_code == 200
        queued_id = queued_upload.json()["id"]
        queued = client.post("/api/chat/sessions/thread-1/queue",
                             json={"text": "later", "image_ids": queued_id})
        assert queued.status_code == 200
        assert queued.json()["items"][0]["text"] == "later"
        assert queued.json()["items"][0]["attachments"] == [{
            "id": queued_id,
            "kind": "image",
            "name": "queued.png",
            "mime": "image/png",
            "available": True,
        }]
        queued_preview = client.get(
            f"/api/chat/queued-image/{queued_id}",
            params={"token": os.environ["MUSELAB_TOKEN"]},
        )
        assert queued_preview.status_code == 200
        assert queued_preview.content == PNG_BYTES
        item_id = queued.json()["items"][0]["id"]
        paused = client.post("/api/chat/sessions/thread-1/queue/pause",
                             json={"paused": True})
        assert paused.json()["paused"] is True
        removed = client.delete(f"/api/chat/sessions/thread-1/queue/{item_id}")
        assert removed.status_code == 200

        skills = client.get("/api/chat/skills?force_reload=true")
        assert skills.status_code == 200
        assert skills.json()["skills"][0]["name"] == "fixture-skill"
        assert skills.json()["skills"][0]["enabled"] is True
        configured = client.patch("/api/chat/skills", json={
            "path": skills.json()["skills"][0]["path"],
            "enabled": False,
        })
        assert configured.status_code == 200
        assert configured.json()["skill"]["enabled"] is False

        mcp = client.get("/api/chat/mcp")
        assert mcp.status_code == 200
        assert mcp.json()["servers"][0]["name"] == "fixture-mcp"
        assert mcp.json()["servers"][0]["tool_count"] == 1
        toggled_mcp = client.patch("/api/chat/mcp/fixture-mcp", json={
            "enabled": False,
        })
        assert toggled_mcp.status_code == 200
        assert toggled_mcp.json()["servers"][0]["disabled"] is True
        deleted_mcp = client.delete("/api/chat/mcp/fixture-mcp")
        assert deleted_mcp.status_code == 200
        assert deleted_mcp.json()["servers"] == []
        added_mcp = client.post("/api/chat/mcp", json={
            "name": "remote-fixture",
            "transport": "http",
            "url": "https://mcp.example.test/mcp",
            "bearer_token_env_var": "MCP_TOKEN",
        })
        assert added_mcp.status_code == 200
        assert added_mcp.json()["servers"][0]["auth_status"] == "notLoggedIn"
        oauth = client.post("/api/chat/mcp/remote-fixture/oauth")
        assert oauth.status_code == 200
        assert oauth.json()["authorization_url"].endswith("/remote-fixture")

        created = client.post("/api/chat/sessions", json={
            "id": "client-id-is-not-authoritative",
            "name": "Native thread",
            "model": "gpt-test-codex",
            "permission": "bypassPermissions",
            "service_tier": "priority",
        })
        assert created.status_code == 200
        assert created.json()["permission"] == "bypassPermissions"
        assert created.json()["service_tier"] == "priority"
        thread_id = created.json()["id"]
        assert thread_id.startswith("thread-")
        configured_effort = client.patch(
            f"/api/chat/sessions/{thread_id}", json={"effort": "medium"})
        assert configured_effort.status_code == 200
        assert configured_effort.json()["effort"] == "medium"
        standard_tier = client.patch(
            f"/api/chat/sessions/{thread_id}", json={"service_tier": ""})
        assert standard_tier.status_code == 200
        assert standard_tier.json()["service_tier"] == ""
        configured_fast = client.patch(
            f"/api/chat/sessions/{thread_id}", json={"service_tier": "priority"})
        assert configured_fast.status_code == 200
        assert configured_fast.json()["service_tier"] == "priority"
        invalid_tier = client.patch(
            f"/api/chat/sessions/{thread_id}", json={"service_tier": "turbo"})
        assert invalid_tier.status_code == 422
        hidden_thinking = client.patch(
            f"/api/chat/sessions/{thread_id}", json={"thinking": False})
        assert hidden_thinking.status_code == 200
        assert hidden_thinking.json()["thinking"] is False
        changed_permission = client.patch(
            f"/api/chat/sessions/{thread_id}", json={"permission": "plan"})
        assert changed_permission.status_code == 200
        assert changed_permission.json()["permission"] == "plan"

        listed = client.get("/api/chat/sessions?limit=100")
        assert listed.status_code == 200
        listed_session = next(
            item for item in listed.json()["sessions"] if item["id"] == thread_id)
        assert listed_session["effort"] == "medium"
        assert listed_session["service_tier"] == "priority"
        assert listed_session["thinking"] is False
        assert listed_session["permission"] == "plan"
        assert listed.headers["etag"].startswith('W/"')
        unchanged = client.get(
            "/api/chat/sessions?limit=100",
            headers={"If-None-Match": listed.headers["etag"]},
        )
        assert unchanged.status_code == 304
        assert unchanged.content == b""

        ticket = client.post("/api/chat/stream/start", json={
            "prompt": "fixture prompt",
            "session_id": thread_id,
            "model": "gpt-test-codex",
            "permission": "bypassPermissions",
            "effort": "medium",
            "service_tier": "priority",
        })
        assert ticket.status_code == 200
        streamed = client.get(
            "/api/chat/stream", params={"ticket": ticket.json()["ticket"]})
        assert streamed.status_code == 200
        assert "event: ping" in streamed.text
        assert streamed.text.index("event: ping") < streamed.text.index("event: thinking")
        assert "event: thinking" in streamed.text
        assert "event: tool_use" in streamed.text
        assert "event: tool_result" in streamed.text
        assert "event: text" in streamed.text
        assert "event: done" in streamed.text
        assert "hello " in streamed.text
        assert '"context_used":9' in streamed.text

        usage = client.get(f"/api/chat/usage/{thread_id}?model=gpt-test-codex")
        assert usage.status_code == 200
        assert usage.json()["context_used"] == 9
        assert usage.json()["context_limit"] == 100
        assert usage.json()["context_used_pct"] == 9.0

        breakdown = client.get(f"/api/chat/context-breakdown/{thread_id}")
        assert breakdown.status_code == 200
        assert breakdown.json()["totalTokens"] == 9
        assert breakdown.json()["maxTokens"] == 100

        loaded = client.get(f"/api/chat/sessions/{thread_id}?tail=80")
        assert loaded.json()["effort"] == "medium"
        assert loaded.json()["service_tier"] == "priority"
        assert loaded.json()["thinking"] is False
        assert loaded.status_code == 200
        assert [message["role"] for message in loaded.json()["messages"]] == [
            "user", "thinking", "assistant",
        ]
        assert loaded.json()["messages"][-1]["text"] == "hello from Codex"

        compacted = client.post(f"/api/chat/sessions/{thread_id}/native-compact")
        assert compacted.status_code == 200
        assert compacted.json()["session_usage"]["context_used"] == 4

        after_compact = client.get(f"/api/chat/sessions/{thread_id}?tail=80")
        assert after_compact.status_code == 200
        assert after_compact.json()["messages"][-1]["_is_compact_summary"] is True

        outline = client.get(f"/api/chat/sessions/{thread_id}/outline")
        assert outline.status_code == 200
        assert outline.json() == {
            "outline": [{"preview": "fixture prompt", "uuid": "user-1"}],
            "history_unavailable": False,
        }

        renamed = client.patch(
            f"/api/chat/sessions/{thread_id}", json={"name": "Renamed"})
        assert renamed.status_code == 200
        assert renamed.json()["name"] == "Renamed"

        pinned = client.patch(
            f"/api/chat/sessions/{thread_id}", json={"pinned": True})
        assert pinned.status_code == 200
        assert pinned.json()["pinned"] is True
        assert client.get("/api/chat/sessions").json()["sessions"][0]["pinned"] is True

        deleted = client.delete(f"/api/chat/sessions/{thread_id}")
        assert deleted.status_code == 200
        assert deleted.json() == {"ok": True}


def test_codex_rate_limit_includes_remaining_percent():
    class RateLimitRuntime:
        async def request(self, method, params, timeout):
            assert method == "account/rateLimits/read"
            assert params is None
            assert timeout == 15
            return {"rateLimits": {
                "primary": {
                    "usedPercent": 15,
                    "resetsAt": 1_800_000_000,
                    "windowDurationMins": 300,
                },
                "secondary": {
                    "usedPercent": 2.5,
                    "resetsAt": 1_800_600_000,
                    "windowDurationMins": 10_080,
                },
                "planType": "prolite",
            }}

    request = SimpleNamespace(app=SimpleNamespace(
        state=SimpleNamespace(codex_runtime=RateLimitRuntime())))
    limits = asyncio.run(codex_rate_limit(request))

    assert limits["provider_authoritative"] is True
    assert limits["windows"]["five_hour"]["remaining_percent"] == 85
    assert limits["windows"]["seven_day"]["remaining_percent"] == 97.5
    assert limits["plan_type"] == "prolite"


def test_codex_rate_limit_names_primary_window_from_native_duration():
    class RateLimitRuntime:
        async def request(self, method, params, timeout):
            return {"rateLimits": {
                "primary": {
                    "usedPercent": 2,
                    "resetsAt": 1_800_000_000,
                    "windowDurationMins": 10_080,
                },
                "planType": "prolite",
            }}

    request = SimpleNamespace(app=SimpleNamespace(
        state=SimpleNamespace(codex_runtime=RateLimitRuntime())))
    limits = asyncio.run(codex_rate_limit(request))

    assert "five_hour" not in limits["windows"]
    assert limits["windows"]["seven_day"]["remaining_percent"] == 98
    assert limits["windows"]["seven_day"]["rate_limit_type"] == "seven_day"


def test_codex_rate_limit_preserves_names_for_equal_duration_limits():
    class RateLimitRuntime:
        async def request(self, method, params, timeout):
            return {"rateLimitsByLimitId": {
                "codex": {
                    "primary": {
                        "usedPercent": 5,
                        "resetsAt": 1_800_000_000,
                        "windowDurationMins": 10_080,
                    },
                    "planType": "prolite",
                },
                "codex_bengalfox": {
                    "primary": {
                        "usedPercent": 0,
                        "resetsAt": 1_800_600_000,
                        "windowDurationMins": 10_080,
                    },
                    "limitName": "GPT-5.3-Codex-Spark",
                    "planType": "prolite",
                },
            }}

    request = SimpleNamespace(app=SimpleNamespace(
        state=SimpleNamespace(codex_runtime=RateLimitRuntime())))
    limits = asyncio.run(codex_rate_limit(request))

    regular = limits["windows"]["seven_day"]
    spark = limits["windows"]["codex_bengalfox:primary"]
    assert regular["limit_id"] == "codex"
    assert regular["limit_name"] is None
    assert regular["remaining_percent"] == 95
    assert spark["limit_id"] == "codex_bengalfox"
    assert spark["limit_name"] == "GPT-5.3-Codex-Spark"
    assert spark["remaining_percent"] == 100
    assert spark["rate_limit_type"] == "seven_day"


def test_session_usage_prefers_native_notification_sidecar():
    class Transcripts:
        async def read(self, _thread_id):
            raise AssertionError("native sidecar must win")

    class Usage:
        def get(self, thread_id, *, model=""):
            assert (thread_id, model) == ("thread-usage", "gpt-test")
            return {"context_used": 380, "context_limit": 2000}

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        codex_history=SimpleNamespace(transcripts=Transcripts()),
        codex_usage=Usage(),
    )))

    result = asyncio.run(session_usage(request, "thread-usage", "gpt-test"))

    assert result == {"context_used": 380, "context_limit": 2000}


def test_native_user_input_sse_and_answer_route(tmp_path):
    app = native_app(tmp_path)
    with TestClient(app) as client:
        created = client.post("/api/chat/sessions", json={
            "name": "Question thread",
            "model": "gpt-test-codex",
        })
        thread_id = created.json()["id"]
        ticket = client.post("/api/chat/stream/start", json={
            "prompt": "request user input",
            "session_id": thread_id,
            "model": "gpt-test-codex",
            "permission": "bypassPermissions",
        }).json()["ticket"]

        with ThreadPoolExecutor(max_workers=1) as executor:
            streamed = executor.submit(
                client.get, "/api/chat/stream", params={"ticket": ticket})
            answer = None
            for _ in range(100):
                answer = client.post(
                    f"/api/chat/answer/{thread_id}/server-input",
                    json={"answers": {"scope": "Current file"}},
                )
                if answer.status_code != 404:
                    break
                time.sleep(0.01)
            response = streamed.result(timeout=2)

        assert answer is not None and answer.status_code == 200
        assert "event: ask_user_question" in response.text
        assert '"id":"scope"' in response.text
        assert "event: done" in response.text


def test_stream_ticket_is_single_use(tmp_path):
    app = native_app(tmp_path)
    with TestClient(app) as client:
        thread_id = client.post(
            "/api/chat/sessions", json={"name": "Once"}).json()["id"]
        ticket = client.post("/api/chat/stream/start", json={
            "prompt": "hello",
            "session_id": thread_id,
        }).json()["ticket"]
        assert client.get("/api/chat/stream", params={"ticket": ticket}).status_code == 200
        assert client.get("/api/chat/stream", params={"ticket": ticket}).status_code == 401
        assert client.get("/api/chat/stream", params={
            "prompt": "must-not-travel-in-url",
            "session_id": thread_id,
            "token": "legacy-token",
        }).status_code == 401


def test_context_info_recognizes_global_codex_agents(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    global_agents = codex_home / "AGENTS.md"
    global_agents.write_text("# Global instructions\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    app = native_app(tmp_path / "workspace")
    with TestClient(app) as client:
        context = client.get("/api/chat/context-info")

    assert context.status_code == 200
    data = context.json()
    assert data["instructions_available"] is True
    assert data["instructions_exists"] is True
    assert data["global_instructions_available"] is True
    assert data["workspace_instructions_available"] is False
    assert data["global_agents_path"] == str(global_agents)
    assert {source["scope"] for source in data["sources"]} >= {
        "user_agents", "user_codex",
    }


def test_native_image_and_document_attachments_roundtrip(tmp_path):
    app = native_app(tmp_path)
    with TestClient(app) as client:
        thread_id = client.post(
            "/api/chat/sessions", json={"name": "Attachments"}).json()["id"]
        image = client.post(
            "/api/chat/upload-image",
            files={"file": ("pixel.png", io.BytesIO(PNG_BYTES), "image/png")},
        )
        assert image.status_code == 200
        assert image.json()["kind"] == "image"
        document = client.post(
            "/api/chat/upload-image",
            files={"file": ("notes.md", io.BytesIO(b"# fixture"), "text/markdown")},
        )
        assert document.status_code == 200
        assert document.json()["kind"] == "text"

        ticket = client.post("/api/chat/stream/start", json={
            "prompt": "",
            "session_id": thread_id,
            "image_ids": f"{image.json()['id']},{document.json()['id']}",
        })
        streamed = client.get(
            "/api/chat/stream", params={"ticket": ticket.json()["ticket"]})
        assert streamed.status_code == 200
        assert "event: done" in streamed.text

        loaded = client.get(f"/api/chat/sessions/{thread_id}?tail=10")
        user = loaded.json()["messages"][0]
        assert user["role"] == "user"
        assert user["text"] == ""
        assert user["images"][0]["name"] == "pixel.png"
        assert user["docs"] == [{"name": "notes.md", "kind": "text"}]

        image_response = client.get(
            user["images"][0]["url"],
            params={"token": os.environ["MUSELAB_TOKEN"]},
        )
        assert image_response.status_code == 200
        assert image_response.content == PNG_BYTES

        deleted = client.delete(f"/api/chat/sessions/{thread_id}")
        assert deleted.status_code == 200
        assert client.get(
            user["images"][0]["url"],
            params={"token": os.environ["MUSELAB_TOKEN"]},
        ).status_code == 404


def test_visible_session_management_actions_have_native_routes(tmp_path):
    app = native_app(tmp_path)
    with TestClient(app) as client:
        organized = client.post("/api/chat/sessions/organize", json={
            "name": "[整理档案]", "model": "gpt-test-codex",
        })
        assert organized.status_code == 200
        assert organized.json()["name"] == "[整理档案]"
        assert "确认" in organized.json()["initial_message"]["zh"]

        found = client.get("/api/chat/search", params={"q": "整理档案"})
        assert found.status_code == 200
        assert found.json()["hits"][0]["sid"] == organized.json()["id"]

        exported = client.get(
            f"/api/chat/sessions/{organized.json()['id']}/export",
            params={"token": os.environ["MUSELAB_TOKEN"]},
        )
        assert exported.status_code == 200
        assert exported.text.startswith("# [整理档案]\n")
        assert "attachment" in exported.headers["content-disposition"]


def test_purge_old_is_server_authoritative_and_cleans_queue(tmp_path):
    app = native_app(tmp_path)
    with TestClient(app) as client:
        keep = client.post(
            "/api/chat/sessions", json={"name": "keep current"}).json()
        pinned = client.post(
            "/api/chat/sessions", json={"name": "keep pinned"}).json()
        victim = client.post(
            "/api/chat/sessions", json={"name": "remove old"}).json()
        assert client.patch(
            f"/api/chat/sessions/{pinned['id']}", json={"pinned": True}
        ).status_code == 200
        assert client.post(
            f"/api/chat/sessions/{victim['id']}/queue",
            json={"text": "queued"},
        ).status_code == 200

        preview = client.post("/api/chat/sessions/purge-old", json={
            "days": 7, "keep_id": keep["id"], "dry_run": True,
        })
        assert preview.status_code == 200
        assert preview.json()["ids"] == [victim["id"]]

        purged = client.post("/api/chat/sessions/purge-old", json={
            "days": 7, "keep_id": keep["id"],
        })
        assert purged.status_code == 200
        assert purged.json()["ids"] == [victim["id"]]
        assert client.get(f"/api/chat/sessions/{keep['id']}").status_code == 200
        assert client.get(f"/api/chat/sessions/{pinned['id']}").status_code == 200
        assert client.get(f"/api/chat/sessions/{victim['id']}").status_code == 404
        assert client.get(
            f"/api/chat/sessions/{victim['id']}/queue"
        ).json()["items"] == []


def test_purge_old_can_be_scoped_to_one_workspace(tmp_path):
    other = tmp_path / "other-project"
    other.mkdir()
    app = native_app(tmp_path)
    with TestClient(app) as client:
        registered = client.post(
            "/api/chat/workspaces",
            json={"path": str(other), "name": "Other"},
        )
        assert registered.status_code == 200

        primary = client.post(
            "/api/chat/sessions", json={"name": "primary old"}).json()
        secondary = client.post(
            "/api/chat/sessions",
            json={"name": "secondary old", "cwd": str(other)},
        ).json()

        purged = client.post("/api/chat/sessions/purge-old", json={
            "days": 7,
            "cwd": str(tmp_path),
        })
        assert purged.status_code == 200, purged.text
        assert purged.json()["ids"] == [primary["id"]]
        assert client.get(
            f"/api/chat/sessions/{secondary['id']}"
        ).status_code == 200


def test_model_switch_uses_native_thread_fork():
    app_js = Path("frontend/app.js").read_text(encoding="utf-8")
    assert '"/fork"' in app_js
    # Fork activation now goes through switchSession(), whose shared
    # _ensureSessionLoaded() path de-duplicates cold loads for resident panes,
    # hover-prefetch and ordinary tab switches.
    fork_block = app_js[app_js.index('"/fork"') :]
    assert "newSt._loaded = false" in fork_block
    assert "await this.switchSession()" in fork_block
