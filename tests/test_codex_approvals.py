"""Approval correlation and native-decision mapping tests."""

import asyncio

import pytest

from backend.codex import CodexApprovalBroker, ServerRequest


@pytest.mark.asyncio
async def test_browser_allow_resolves_exact_app_server_request():
    broker = CodexApprovalBroker(timeout=1)
    published = []

    async def publish(thread_id, event):
        published.append((thread_id, event))

    broker.publisher = publish
    task = asyncio.create_task(broker.handle(ServerRequest(
        id="request-7",
        method="item/commandExecution/requestApproval",
        params={
            "threadId": "thread-1",
            "turnId": "turn-1",
            "itemId": "item-1",
            "command": "pwd",
        },
    )))
    await asyncio.sleep(0)

    assert published == [("thread-1", {
        "id": "request-7",
        "tool": "Bash",
        "summary": "pwd",
    })]
    assert broker.submit("thread-1", "request-7", "allow") is True
    assert await task == {"decision": "accept"}
    assert broker.submit("thread-1", "request-7", "allow") is False


@pytest.mark.asyncio
async def test_always_maps_to_session_scoped_native_acceptance():
    broker = CodexApprovalBroker(timeout=1)

    async def publish(_thread_id, _event):
        return None

    broker.publisher = publish
    task = asyncio.create_task(broker.handle(ServerRequest(
        id=9,
        method="item/fileChange/requestApproval",
        params={
            "threadId": "thread-2",
            "turnId": "turn-2",
            "itemId": "item-2",
            "reason": "write generated file",
        },
    )))
    await asyncio.sleep(0)
    assert broker.submit("thread-2", "9", "always") is True
    assert await task == {"decision": "acceptForSession"}


def test_invalid_browser_decision_is_rejected():
    broker = CodexApprovalBroker()
    with pytest.raises(ValueError, match="invalid approval decision"):
        broker.submit("thread-1", "request-1", "maybe")


@pytest.mark.asyncio
async def test_permission_profile_grant_preserves_requested_scope():
    broker = CodexApprovalBroker(timeout=1)
    broker.publisher = lambda *_args: asyncio.sleep(0)
    permissions = {
        "network": {"enabled": True},
        "fileSystem": {"entries": [{
            "access": "write",
            "path": {"type": "path", "path": "/tmp/output"},
        }]},
    }
    task = asyncio.create_task(broker.handle(ServerRequest(
        id="permissions-1",
        method="item/permissions/requestApproval",
        params={
            "threadId": "thread-1",
            "turnId": "turn-1",
            "itemId": "item-1",
            "cwd": "/workspace",
            "startedAtMs": 1,
            "permissions": permissions,
            "reason": "Write the generated artifact",
        },
    )))
    await asyncio.sleep(0)

    assert broker.submit("thread-1", "permissions-1", "always") is True
    assert await task == {
        "permissions": permissions,
        "scope": "session",
    }
