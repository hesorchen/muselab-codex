"""Native background terminal lifecycle tests without a real Codex login."""

import asyncio

import pytest

from backend.codex.terminal import CodexTerminalService


class Subscription:
    def __init__(self):
        self.closed = False

    async def next(self):
        await asyncio.Future()

    async def close(self):
        self.closed = True


class Events:
    def __init__(self):
        self.subscription = Subscription()

    async def subscribe_connection(self):
        return self.subscription


class Runtime:
    def __init__(self):
        self.calls = []

    async def request(self, method, params=None, *, timeout=None):
        self.calls.append((method, params, timeout))
        if method == "command/exec":
            await asyncio.sleep(0)
            return {"exitCode": 0, "stdout": "done\n", "stderr": ""}
        if method in {"command/exec/write", "command/exec/terminate"}:
            return {}
        raise AssertionError(method)


@pytest.mark.asyncio
async def test_terminal_runs_native_command_inside_workspace(tmp_path):
    runtime = Runtime()
    events = Events()
    service = CodexTerminalService(runtime, events, tmp_path)

    started = await service.start(["echo", "done"])
    for _ in range(20):
        current = service.get(started["id"])
        if current["status"] != "running":
            break
        await asyncio.sleep(0.01)

    assert current["status"] == "completed"
    assert current["output"] == "done\n"
    assert runtime.calls[0][0] == "command/exec"
    assert runtime.calls[0][1]["command"] == ["echo", "done"]
    assert runtime.calls[0][1]["cwd"] == str(tmp_path.resolve())
    assert runtime.calls[0][1]["tty"] is True
    assert events.subscription.closed is True
    await service.close()


@pytest.mark.asyncio
async def test_terminal_rejects_cwd_outside_workspace(tmp_path):
    service = CodexTerminalService(Runtime(), Events(), tmp_path)
    with pytest.raises(ValueError, match="stay inside"):
        await service.start(["pwd"], cwd="../outside")
