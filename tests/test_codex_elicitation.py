"""MCP elicitation correlation, validation, and typed response tests."""

import asyncio

import pytest

from backend.codex import CodexElicitationBroker, ServerRequest


@pytest.mark.asyncio
async def test_form_elicitation_returns_typed_content():
    broker = CodexElicitationBroker(timeout=1)
    published = []

    async def publish(thread_id, mode, event):
        published.append((thread_id, mode, event))

    broker.publisher = publish
    task = asyncio.create_task(broker.handle(ServerRequest(
        id="form-1",
        method="mcpServer/elicitation/request",
        params={
            "threadId": "thread-1",
            "turnId": "turn-1",
            "serverName": "calendar",
            "mode": "form",
            "message": "Choose a scope",
            "requestedSchema": {
                "type": "object",
                "required": ["scope", "notify"],
                "properties": {
                    "scope": {
                        "type": "string",
                        "title": "Scope",
                        "enum": ["today", "week"],
                    },
                    "notify": {"type": "boolean", "title": "Notify"},
                },
            },
        },
    )))
    await asyncio.sleep(0)

    assert published[0][0:2] == ("thread-1", "form")
    assert published[0][2]["questions"][0]["options"][0]["value"] == "today"
    assert broker.submit_answers("thread-1", "form-1", {
        "scope": "week", "notify": "true",
    }) is True
    assert await task == {
        "action": "accept",
        "content": {"scope": "week", "notify": True},
    }


@pytest.mark.asyncio
async def test_url_elicitation_is_validated_and_can_be_declined():
    broker = CodexElicitationBroker(timeout=1)
    published = []

    async def publish(thread_id, mode, event):
        published.append((thread_id, mode, event))

    broker.publisher = publish
    task = asyncio.create_task(broker.handle(ServerRequest(
        id="url-1",
        method="mcpServer/elicitation/request",
        params={
            "threadId": "thread-1",
            "serverName": "calendar",
            "mode": "url",
            "message": "Authorize calendar",
            "elicitationId": "native-1",
            "url": "https://example.test/oauth",
        },
    )))
    await asyncio.sleep(0)

    assert published == [("thread-1", "url", {
        "id": "url-1",
        "kind": "mcp_url",
        "tool": "calendar",
        "summary": "Authorize calendar",
        "url": "https://example.test/oauth",
    })]
    assert broker.submit_decision("thread-1", "url-1", "deny") is True
    assert await task == {"action": "decline"}


@pytest.mark.asyncio
async def test_unsupported_extended_form_fails_closed():
    broker = CodexElicitationBroker(timeout=1)
    result = await broker.handle(ServerRequest(
        id="extended-1",
        method="mcpServer/elicitation/request",
        params={
            "threadId": "thread-1",
            "serverName": "server",
            "mode": "openai/form",
            "message": "Unsupported",
            "requestedSchema": {},
        },
    ))
    assert result == {"action": "cancel"}


def test_optional_form_fields_may_be_omitted():
    from backend.codex.elicitation import _coerce_form_content

    assert _coerce_form_content(
        {"required": "yes", "optional": None},
        {
            "required": ["required"],
            "properties": {
                "required": {"type": "string"},
                "optional": {"type": "string"},
            },
        },
    ) == {"required": "yes"}
