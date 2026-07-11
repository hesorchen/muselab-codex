"""FastAPI lifespan wiring for the Codex runtime and thread service."""

from fastapi.testclient import TestClient


class FakeRuntime:
    def __init__(self):
        self.started = 0
        self.closed = 0

    async def start(self):
        self.started += 1
        return self

    async def ensure_ready(self):
        return self

    async def next_notification(self):
        await __import__("asyncio").Future()

    async def close(self):
        self.closed += 1

    async def request(self, method, params=None, *, timeout=None):
        raise AssertionError("lifecycle test must not issue protocol requests")


def test_fastapi_lifespan_starts_and_closes_injected_runtime(app_module, monkeypatch):
    runtime = FakeRuntime()
    monkeypatch.setattr(app_module, "_create_codex_runtime", lambda _handler=None: runtime)

    with TestClient(app_module.app) as client:
        assert runtime.started == 1
        assert client.app.state.codex_runtime is runtime
        assert client.app.state.codex_threads.requester is runtime
        assert client.app.state.codex_approvals.publisher is not None
        assert client.app.state.codex_user_input.publisher is not None
        assert client.app.state.codex_history.runtime is runtime

    assert runtime.closed == 1
