"""Offline protocol tests for the Codex-native app-server client."""

import asyncio
import sys
from pathlib import Path

import pytest

from backend.codex import (
    AppServerProcessError,
    AppServerProtocolError,
    AppServerResponseError,
    AppServerTimeoutError,
    CodexAppServer,
    CodexSharedAppServer,
    TurnEventAccumulator,
)


FAKE_SERVER = Path(__file__).parent / "fixtures" / "fake_codex_app_server.py"


def fake_command(scenario: str = "normal") -> tuple[str, ...]:
    return sys.executable, str(FAKE_SERVER), scenario


def test_shared_app_server_uses_codex_proxy_and_exposes_remote_url(tmp_path):
    socket_path = tmp_path / "runtime" / "app-server.sock"
    server = CodexSharedAppServer(codex_bin="codex-test", socket_path=socket_path)

    assert server.command == (
        "codex-test", "app-server", "--listen", f"unix://{socket_path.resolve()}")
    assert server.remote_url == f"unix://{socket_path.resolve()}"


def test_shared_app_server_lock_rejects_second_owner(tmp_path):
    socket_path = tmp_path / "runtime" / "app-server.sock"
    first = CodexSharedAppServer(codex_bin="codex-test", socket_path=socket_path)
    second = CodexSharedAppServer(codex_bin="codex-test", socket_path=socket_path)
    socket_path.parent.mkdir(parents=True)

    first._acquire_listener_lock()
    try:
        with pytest.raises(AppServerProcessError, match="another muselab-codex"):
            second._acquire_listener_lock()
    finally:
        first._release_listener_lock()


@pytest.mark.asyncio
async def test_shared_app_server_disables_unix_websocket_keepalive(
    monkeypatch, tmp_path,
):
    captured = {}

    class FakeWebSocket:
        def __init__(self):
            self.messages = asyncio.Queue()

        async def send(self, raw):
            message = __import__("json").loads(raw)
            if message.get("method") == "initialize":
                await self.messages.put(__import__("json").dumps({
                    "id": message["id"],
                    "result": {"userAgent": "fake"},
                }))

        def __aiter__(self):
            return self

        async def __anext__(self):
            item = await self.messages.get()
            if item is None:
                raise StopAsyncIteration
            return item

        async def close(self):
            await self.messages.put(None)

    async def fake_connect(*args, **kwargs):
        captured.update(kwargs)
        return FakeWebSocket()

    class Server(CodexSharedAppServer):
        async def _start_listener(self):
            return None

        async def _close_listener(self):
            return None

    monkeypatch.setattr("backend.codex.process.websockets.unix_connect", fake_connect)
    server = Server(codex_bin="codex-test", socket_path=tmp_path / "app.sock")

    await server.start()
    await server.close()

    assert captured["compression"] is None
    assert captured["ping_interval"] is None


@pytest.mark.asyncio
async def test_handshake_stream_and_approval_roundtrip():
    approvals = []

    async def approve(request):
        approvals.append((request.method, request.params["itemId"], request.id))
        return {"decision": "accept"}

    server = CodexAppServer(
        command=fake_command(),
        request_handler=approve,
        environment={"OPENAI_API_KEY": "must-not-reach-child"},
    )
    async with server:
        assert server.initialize_result["userAgent"] == "fake-app-server"
        assert server.initialize_result["apiKeyPresent"] is False

        started = await server.request("thread/start", {"cwd": "/tmp/fixture"})
        assert started["thread"]["id"] == "thread-1"
        turn = await server.request("turn/start", {
            "threadId": "thread-1",
            "input": [{"type": "text", "text": "fixture prompt"}],
        })
        assert turn["turn"]["id"] == "turn-1"

        accumulator = TurnEventAccumulator("thread-1", "turn-1")
        completed = None
        while not accumulator.completed:
            notification = await server.next_notification(timeout=2)
            accumulator.apply(notification)
            if notification["method"] == "turn/completed":
                completed = notification

        assert accumulator.text == "hello from Codex"
        assert accumulator.status == "completed"
        assert accumulator.token_usage["total"]["totalTokens"] == 12
        assert accumulator.token_usage["last"]["totalTokens"] == 10
        assert accumulator.token_usage["modelContextWindow"] == 100
        assert completed["params"]["turn"]["approvalDecisions"] == [
            "accept", "accept",
        ]
        assert [(method, request_id) for method, _item_id, request_id in approvals] == [
            ("item/commandExecution/requestApproval", "server-command"),
            ("item/fileChange/requestApproval", "server-file"),
        ]
        assert [method for method, _item_id, _request_id in approvals] == [
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        ]


@pytest.mark.asyncio
async def test_experimental_user_input_request_roundtrip():
    requests = []

    async def handle(request):
        requests.append(request)
        return {"answers": {"scope": {"answers": ["Current file"]}}}

    server = CodexAppServer(
        command=fake_command(),
        request_handler=handle,
        initialize_capabilities={"experimentalApi": True},
    )
    async with server:
        assert server.initialize_result["clientCapabilities"] == {
            "experimentalApi": True,
        }
        await server.request("thread/start", {"cwd": "/tmp/fixture"})
        await server.request("turn/start", {
            "threadId": "thread-1",
            "input": [{"type": "text", "text": "request user input"}],
            "approvalPolicy": "never",
        })
        while True:
            notification = await server.next_notification(timeout=2)
            if notification["method"] == "turn/completed":
                completed = notification["params"]["turn"]
                break

        assert [request.method for request in requests] == [
            "item/tool/requestUserInput",
        ]
        assert completed["userInputAnswers"] == {
            "scope": {"answers": ["Current file"]},
        }


@pytest.mark.asyncio
async def test_concurrent_requests_are_correlated_by_id():
    async with CodexAppServer(command=fake_command()) as server:
        first = asyncio.create_task(server.request("test/reverse", {"value": 1}))
        second = asyncio.create_task(server.request("test/reverse", {"value": 2}))
        assert await first == {"value": 1}
        assert await second == {"value": 2}


@pytest.mark.asyncio
async def test_default_approval_decision_is_decline():
    async with CodexAppServer(command=fake_command()) as server:
        await server.request("thread/start")
        await server.request("turn/start", {
            "threadId": "thread-1",
            "input": [{"type": "text", "text": "fixture prompt"}],
        })
        while True:
            notification = await server.next_notification(timeout=2)
            if notification["method"] == "turn/completed":
                assert notification["params"]["turn"]["approvalDecisions"] == [
                    "decline", "decline",
                ]
                break


@pytest.mark.asyncio
async def test_jsonrpc_error_is_bounded_and_does_not_echo_server_message():
    async with CodexAppServer(command=fake_command()) as server:
        with pytest.raises(AppServerResponseError) as exc_info:
            await server.request("missing/method")
        assert exc_info.value.code == -32601
        assert "Method not found" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_malformed_json_fails_pending_request():
    server = CodexAppServer(command=fake_command("malformed"))
    async with server:
        with pytest.raises(AppServerProtocolError, match="malformed JSONL"):
            await server.request("test/malformed")


@pytest.mark.asyncio
async def test_unexpected_exit_fails_pending_request_without_stderr_content():
    server = CodexAppServer(command=fake_command("exit"))
    async with server:
        with pytest.raises(AppServerProcessError, match="status 7"):
            await server.request("test/exit")


@pytest.mark.asyncio
async def test_close_fails_pending_request_and_reaps_owned_process():
    server = CodexAppServer(command=fake_command("hang"))
    await server.start()
    request = asyncio.create_task(server.request("test/hang"))
    await asyncio.sleep(0.05)
    process = server._process

    await server.close()

    with pytest.raises(AppServerProcessError, match="client closed"):
        await request
    assert process.returncode is not None


@pytest.mark.asyncio
async def test_request_timeout_has_a_distinct_bounded_error():
    server = CodexAppServer(command=fake_command("hang"), request_timeout=0.05)
    async with server:
        with pytest.raises(AppServerTimeoutError, match="test/hang timed out"):
            await server.request("test/hang")
