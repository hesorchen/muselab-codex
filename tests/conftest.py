"""Shared fixtures for Codex-native HTTP tests."""

import asyncio
import sys
from pathlib import Path

import pytest


TEST_TOKEN = "test-token-1234567890abcdef-secure-min-32"


class _IdleRuntime:
    """Lifespan-safe runtime double for routes that do not call Codex."""

    def __init__(self):
        self.started = 0
        self.closed = 0

    async def start(self):
        self.started += 1
        return self

    async def ensure_ready(self):
        return self

    async def next_notification(self):
        await asyncio.Future()

    async def close(self):
        self.closed += 1

    def health(self):
        return type("Health", (), {
            "state": "ready", "running": True, "restart_count": 0, "error": None,
        })()


@pytest.fixture()
def temp_root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    root.mkdir()
    (root / "README.md").write_text("# Hello\n\nfirst paragraph here\n")
    (root / "notes").mkdir()
    (root / "notes" / "a.md").write_text("# A\nbody of a\n")
    (root / "notes" / "b.txt").write_text("plain b text\n")
    (root / "notes" / "deep").mkdir()
    (root / "notes" / "deep" / "c.py").write_text("def hello():\n    pass\n")
    (root / ".secret").write_text("hidden file")
    (root / ".env").write_text("FAKE=secret")
    return root


@pytest.fixture()
def app_module(monkeypatch, temp_root):
    monkeypatch.setenv("MUSELAB_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("MUSELAB_ROOT", str(temp_root))
    monkeypatch.setenv("MUSELAB_PORT", "9999")
    for name in list(sys.modules):
        if name == "backend" or name.startswith("backend."):
            del sys.modules[name]

    import backend.main as main_mod

    runtime = _IdleRuntime()
    monkeypatch.setattr(main_mod, "_create_codex_runtime", lambda _handler=None: runtime)
    monkeypatch.setattr(main_mod, "_detect_versions", lambda: {
        "muselab_version": "test", "sdk_version": None,
        "cli_version": "test", "python_version": "test",
    })
    return main_mod


@pytest.fixture()
def client(app_module):
    from fastapi.testclient import TestClient
    # uvloop may be installed through uvicorn[standard], but TestClient runs
    # its AnyIO portal in a worker thread. The default uvloop backend can hang
    # during portal startup/teardown in constrained CI environments.
    with TestClient(
        app_module.app,
        backend_options={"use_uvloop": False},
    ) as test_client:
        yield test_client


@pytest.fixture()
def auth():
    return {"X-Auth-Token": TEST_TOKEN}
