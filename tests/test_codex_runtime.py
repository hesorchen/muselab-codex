"""Lifecycle tests for the application-scoped Codex runtime."""

import asyncio
import sys
from pathlib import Path

import pytest

from backend.codex import (
    AppServerProcessError,
    AppServerTimeoutError,
    CodexAppServer,
    CodexRuntime,
)


FAKE_SERVER = Path(__file__).parent / "fixtures" / "fake_codex_app_server.py"


def fake_server(scenario: str = "normal") -> CodexAppServer:
    return CodexAppServer(command=(sys.executable, str(FAKE_SERVER), scenario))


@pytest.mark.asyncio
async def test_runtime_start_health_and_close():
    runtime = CodexRuntime(fake_server)
    await runtime.start()

    assert runtime.health().state == "ready"
    assert runtime.health().running is True
    assert runtime.health().restart_count == 0

    await runtime.close()
    assert runtime.health().state == "stopped"
    assert runtime.health().running is False


@pytest.mark.asyncio
async def test_concurrent_start_creates_one_process():
    created = 0

    def factory():
        nonlocal created
        created += 1
        return fake_server()

    runtime = CodexRuntime(factory)
    servers = await asyncio.gather(*(runtime.start() for _ in range(5)))
    try:
        assert created == 1
        assert all(server is servers[0] for server in servers)
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_next_request_restarts_after_process_failure_without_retrying_failure():
    scenarios = iter(("exit", "normal"))
    runtime = CodexRuntime(lambda: fake_server(next(scenarios)))
    await runtime.start()

    with pytest.raises(AppServerProcessError, match="status 7"):
        await runtime.request("test/exit")
    assert runtime.health().state == "failed"

    result = await runtime.request("thread/start", {"cwd": "/tmp/fixture"})
    try:
        assert result["thread"]["id"] == "thread-1"
        assert runtime.health().state == "ready"
        assert runtime.health().restart_count == 1
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_timeout_discards_live_but_unresponsive_process_before_next_request():
    scenarios = iter(("hang", "normal"))
    runtime = CodexRuntime(lambda: CodexAppServer(
        command=(sys.executable, str(FAKE_SERVER), next(scenarios)),
        request_timeout=0.05,
    ))
    await runtime.start()

    with pytest.raises(AppServerTimeoutError, match="timed out"):
        await runtime.request("thread/list", {"cwd": "/tmp/fixture"})
    assert runtime.health().state == "failed"
    assert runtime.health().running is False

    result = await runtime.request("thread/start", {"cwd": "/tmp/fixture"})
    try:
        assert result["thread"]["id"] == "thread-1"
        assert runtime.health().state == "ready"
        assert runtime.health().restart_count == 1
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_start_failure_is_bounded_and_health_is_failed():
    runtime = CodexRuntime(lambda: CodexAppServer(
        command=("/definitely-missing-muselab-codex-binary",),
    ))

    with pytest.raises(AppServerProcessError, match="unable to start"):
        await runtime.start()
    assert runtime.health().state == "failed"
    assert runtime.health().running is False
    await runtime.close()


@pytest.mark.asyncio
async def test_read_timeout_does_not_discard_shared_runtime():
    runtime = CodexRuntime(lambda: CodexAppServer(
        command=(sys.executable, str(FAKE_SERVER), "hang"),
        request_timeout=0.05,
    ))
    await runtime.start()
    try:
        with pytest.raises(AppServerTimeoutError):
            await runtime.read_request("thread/items/list")
        assert runtime.health().state == "ready"
        assert runtime.health().running is True
    finally:
        await runtime.close()
