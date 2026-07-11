"""Application-scoped lifecycle for the Codex app-server process."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from .process import (
    AppServerError,
    AppServerProcessError,
    AppServerTimeoutError,
    CodexAppServer,
)


RuntimeState = Literal["stopped", "starting", "ready", "failed", "closing"]
ServerFactory = Callable[[], CodexAppServer]


@dataclass(frozen=True)
class RuntimeHealth:
    state: RuntimeState
    running: bool
    restart_count: int
    error: str | None


class CodexRuntime:
    """Own a replaceable app-server client for one application process.

    Failed requests are never retried automatically because a mutating method
    may have reached app-server before the connection failed. The next request
    starts a fresh process and callers can explicitly resume their thread.
    """

    def __init__(self, server_factory: ServerFactory = CodexAppServer):
        self._server_factory = server_factory
        self._server: CodexAppServer | None = None
        self._state: RuntimeState = "stopped"
        self._error: str | None = None
        self._restart_count = 0
        self._started_once = False
        self._lock = asyncio.Lock()

    @property
    def server(self) -> CodexAppServer | None:
        return self._server

    def health(self) -> RuntimeHealth:
        running = self._server is not None and self._server.running
        state = self._state
        error = self._error
        if state == "ready" and not running:
            state = "failed"
            error = error or "app-server is not running"
        return RuntimeHealth(
            state=state,
            running=running,
            restart_count=self._restart_count,
            error=error,
        )

    async def start(self) -> CodexAppServer:
        async with self._lock:
            if self._state == "closing":
                raise AppServerProcessError("Codex runtime is closing")
            if self._server is not None and self._server.running:
                return self._server
            return await self._start_locked(
                is_restart=self._started_once or self._server is not None)

    async def ensure_ready(self) -> CodexAppServer:
        return await self.start()

    async def restart(self) -> CodexAppServer:
        async with self._lock:
            await self._close_server_locked()
            return await self._start_locked(is_restart=True)

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        server = await self.ensure_ready()
        try:
            return await server.request(method, params, timeout=timeout)
        except (AppServerProcessError, AppServerTimeoutError) as exc:
            async with self._lock:
                if self._server is server:
                    # A live process that stopped answering JSON-RPC is not
                    # healthy.  Keeping it around makes every later request
                    # wait for the full timeout while /api/health continues
                    # to claim "ready".  Discard this generation; the next
                    # request starts a fresh app-server but the timed-out
                    # operation itself is never retried (it may have been a
                    # mutation that reached Codex before the stall).
                    await self._close_server_locked()
                    self._state = "failed"
                    self._error = str(exc)
            raise

    async def read_request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        """Issue an idempotent read without discarding the shared runtime.

        The transport removes a timed-out request from its pending map, so a
        late response is safely ignored. Read-heavy UI fallbacks can therefore
        use local data without interrupting unrelated active turns.
        """
        server = await self.ensure_ready()
        return await server.request(method, params, timeout=timeout)

    async def close(self) -> None:
        async with self._lock:
            self._state = "closing"
            await self._close_server_locked()
            self._state = "stopped"
            self._error = None

    async def _start_locked(self, *, is_restart: bool) -> CodexAppServer:
        await self._close_server_locked()
        self._state = "starting"
        self._error = None
        server = self._server_factory()
        self._server = server
        try:
            await server.start()
        except AppServerError as exc:
            self._state = "failed"
            self._error = str(exc)
            raise
        except Exception as exc:
            self._state = "failed"
            self._error = type(exc).__name__
            raise AppServerProcessError("unable to initialize Codex runtime") from exc
        if is_restart:
            self._restart_count += 1
        self._started_once = True
        self._state = "ready"
        return server

    async def _close_server_locked(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            await server.close()
