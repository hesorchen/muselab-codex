"""Native request-user-input correlation and validation tests."""

import asyncio

import pytest

from backend.codex import (
    CodexApprovalBroker,
    CodexClientRequestRouter,
    CodexElicitationBroker,
    CodexUserInputBroker,
    ServerRequest,
)


def user_input_request(**overrides):
    params = {
        "threadId": "thread-1",
        "turnId": "turn-1",
        "itemId": "item-1",
        "questions": [{
            "id": "scope",
            "header": "Scope",
            "question": "Which scope?",
            "options": [{
                "label": "Current file",
                "description": "Only edit the current file.",
            }],
        }],
    }
    params.update(overrides)
    return ServerRequest(
        id="request-1",
        method="item/tool/requestUserInput",
        params=params,
    )


@pytest.mark.asyncio
async def test_browser_answer_maps_question_ids_to_native_shape():
    broker = CodexUserInputBroker(timeout=1)
    published = []

    async def publish(thread_id, event):
        published.append((thread_id, event))

    broker.publisher = publish
    task = asyncio.create_task(broker.handle(user_input_request()))
    await asyncio.sleep(0)

    assert published == [("thread-1", {
        "id": "request-1",
        "questions": [{
            "id": "scope",
            "header": "Scope",
            "question": "Which scope?",
            "options": [{
                "label": "Current file",
                "description": "Only edit the current file.",
            }],
            "multiSelect": False,
            "isOther": False,
            "isSecret": False,
        }],
    })]
    assert broker.submit(
        "thread-1", "request-1", {"scope": "Current file"}) is True
    assert await task == {
        "answers": {"scope": {"answers": ["Current file"]}},
    }
    assert broker.submit(
        "thread-1", "request-1", {"scope": "Current file"}) is False


@pytest.mark.asyncio
async def test_auto_resolution_returns_empty_answers():
    broker = CodexUserInputBroker(timeout=1)

    async def publish(_thread_id, _event):
        return None

    broker.publisher = publish
    result = await broker.handle(user_input_request(autoResolutionMs=0))
    assert result == {"answers": {}}


@pytest.mark.asyncio
async def test_missing_active_browser_stream_returns_empty_answers():
    broker = CodexUserInputBroker(timeout=1)

    async def missing_stream(_thread_id, _event):
        raise ValueError("no active turn")

    broker.publisher = missing_stream
    assert await broker.handle(user_input_request()) == {"answers": {}}


@pytest.mark.asyncio
async def test_router_keeps_approval_and_user_input_protocols_separate():
    approvals = CodexApprovalBroker(timeout=1)
    user_input = CodexUserInputBroker(timeout=1)
    elicitation = CodexElicitationBroker(timeout=1)
    router = CodexClientRequestRouter(approvals, user_input, elicitation)
    approvals.publisher = user_input.publisher = _noop_publish

    approval_task = asyncio.create_task(router.handle(ServerRequest(
        id="approval-1",
        method="item/commandExecution/requestApproval",
        params={
            "threadId": "thread-1",
            "turnId": "turn-1",
            "itemId": "command-1",
            "command": "pwd",
        },
    )))
    input_task = asyncio.create_task(router.handle(user_input_request()))
    await asyncio.sleep(0)

    assert approvals.submit("thread-1", "approval-1", "allow") is True
    assert user_input.submit(
        "thread-1", "request-1", {"scope": ["Whole project"]}) is True
    assert await approval_task == {"decision": "accept"}
    assert await input_task == {
        "answers": {"scope": {"answers": ["Whole project"]}},
    }


@pytest.mark.asyncio
async def test_invalid_or_secret_answers_are_not_retained_after_close():
    broker = CodexUserInputBroker(timeout=1)
    broker.publisher = _noop_publish
    task = asyncio.create_task(broker.handle(user_input_request(
        questions=[{
            "id": "token",
            "header": "Token",
            "question": "Enter the token",
            "options": None,
            "isSecret": True,
        }],
    )))
    await asyncio.sleep(0)

    with pytest.raises(ValueError, match="pending question ids"):
        broker.submit("thread-1", "request-1", {"wrong": "secret"})
    await broker.close()
    assert await task == {"answers": {}}
    assert broker._pending == {}


async def _noop_publish(_thread_id, _event):
    return None


def test_frontend_submits_native_question_ids_and_masks_secrets():
    app_js = __import__("pathlib").Path("frontend/app.js").read_text()
    index_html = __import__("pathlib").Path("frontend/index.html").read_text()

    assert "q?.id || q?.question" in app_js
    assert "/api/chat/answer/${encodeURIComponent(this.currentId)}" in app_js
    assert 'type="password"' in index_html
    assert "m.askOtherText[askQuestionKey(q)]" in index_html
