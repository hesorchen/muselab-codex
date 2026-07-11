"""Native app-server PTY sessions for the optional background terminal."""

from __future__ import annotations

import asyncio
import base64
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .event_router import CodexEventRouter, EventSubscription
from .process import AppServerError
from .runtime import CodexRuntime


_MAX_PROCESSES = 4
_MAX_COMMAND_ARGS = 128
_MAX_COMMAND_BYTES = 16_384
_MAX_INPUT_BYTES = 65_536
_MAX_OUTPUT_BYTES = 1_000_000


@dataclass
class _TerminalProcess:
    id: str
    command: list[str]
    cwd: str
    started_at: float
    status: str = "running"
    exit_code: int | None = None
    error: str = ""
    output: str = ""
    output_truncated: bool = False
    subscription: EventSubscription | None = None
    task: asyncio.Task | None = None
    updated_at: float = field(default_factory=time.time)


class CodexTerminalService:
    """Manage bounded, connection-scoped ``command/exec`` processes.

    The app-server owns execution and sandboxing.  muselab only validates the
    workspace boundary, fans out streamed output, and keeps a small in-memory
    status view for the browser.  Processes intentionally do not survive an
    app-server restart because their protocol ``processId`` is connection scoped.
    """

    def __init__(self, runtime: CodexRuntime, events: CodexEventRouter, workspace: Path):
        self.runtime = runtime
        self.events = events
        self.workspace = Path(workspace).resolve()
        self._processes: dict[str, _TerminalProcess] = {}
        self._lock = asyncio.Lock()

    async def start(self, command: list[str], *, cwd: str = "") -> dict[str, Any]:
        clean_command = _command(command)
        clean_cwd = _cwd(self.workspace, cwd)
        async with self._lock:
            running = sum(process.status == "running" for process in self._processes.values())
            if running >= _MAX_PROCESSES:
                raise OverflowError("too many background terminal processes")
            process_id = f"terminal-{secrets.token_urlsafe(12)}"
            process = _TerminalProcess(
                id=process_id,
                command=clean_command,
                cwd=str(clean_cwd),
                started_at=time.time(),
            )
            process.subscription = await self.events.subscribe_connection()
            process.task = asyncio.create_task(self._run(process), name=process_id)
            self._processes[process_id] = process
        return self.get(process_id)

    def list(self) -> list[dict[str, Any]]:
        return [self._view(process) for process in sorted(
            self._processes.values(), key=lambda item: item.started_at, reverse=True)]

    def get(self, process_id: str) -> dict[str, Any]:
        process = self._processes.get(process_id)
        if process is None:
            raise KeyError(process_id)
        return self._view(process)

    async def write(self, process_id: str, data: str, *, close_stdin: bool = False) -> dict[str, Any]:
        process = self._running(process_id)
        encoded = data.encode("utf-8")
        if len(encoded) > _MAX_INPUT_BYTES:
            raise ValueError("terminal input is too large")
        params: dict[str, Any] = {"processId": process.id}
        if encoded:
            params["deltaBase64"] = base64.b64encode(encoded).decode("ascii")
        if close_stdin:
            params["closeStdin"] = True
        await self.runtime.request("command/exec/write", params, timeout=30)
        return self.get(process_id)

    async def terminate(self, process_id: str) -> dict[str, Any]:
        process = self._running(process_id)
        await self.runtime.request("command/exec/terminate", {"processId": process.id}, timeout=30)
        return self.get(process_id)

    async def close(self) -> None:
        processes = tuple(self._processes.values())
        for process in processes:
            if process.status == "running":
                try:
                    await self.runtime.request(
                        "command/exec/terminate", {"processId": process.id}, timeout=2)
                except AppServerError:
                    pass
            if process.task is not None and not process.task.done():
                process.task.cancel()
        await asyncio.gather(
            *(process.task for process in processes if process.task is not None),
            return_exceptions=True,
        )
        for process in processes:
            if process.subscription is not None:
                await process.subscription.close()

    async def _run(self, process: _TerminalProcess) -> None:
        subscription = process.subscription
        assert subscription is not None
        request = asyncio.create_task(self.runtime.request("command/exec", {
            "command": process.command,
            "cwd": process.cwd,
            "processId": process.id,
            "tty": True,
            "streamStdin": True,
            "streamStdoutStderr": True,
            "timeoutMs": 3_600_000,
        }, timeout=3_660))
        try:
            while not request.done():
                next_notification = asyncio.create_task(subscription.next())
                try:
                    done, _pending = await asyncio.wait(
                        {request, next_notification},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if request in done:
                        next_notification.cancel()
                        await asyncio.gather(next_notification, return_exceptions=True)
                        break
                    notification = next_notification.result()
                except BaseException:
                    if not next_notification.done():
                        next_notification.cancel()
                        await asyncio.gather(next_notification, return_exceptions=True)
                    raise
                self._consume_output(process, notification)
            result = await request
            if isinstance(result, dict):
                self._append(process, str(result.get("stdout") or ""))
                self._append(process, str(result.get("stderr") or ""))
                exit_code = result.get("exitCode")
                process.exit_code = exit_code if isinstance(exit_code, int) else None
            process.status = "completed" if process.exit_code in {None, 0} else "failed"
        except asyncio.CancelledError:
            process.status = "stopped"
            raise
        except AppServerError as exc:
            process.status = "failed"
            process.error = str(exc)
        finally:
            process.updated_at = time.time()
            await subscription.close()

    def _consume_output(self, process: _TerminalProcess, notification: dict[str, Any]) -> None:
        if notification.get("method") != "command/exec/outputDelta":
            return
        params = notification.get("params")
        if not isinstance(params, dict) or params.get("processId") != process.id:
            return
        encoded = params.get("deltaBase64")
        if not isinstance(encoded, str):
            return
        try:
            text = base64.b64decode(encoded, validate=True).decode("utf-8", errors="replace")
        except (ValueError, UnicodeDecodeError):
            return
        self._append(process, text)

    def _append(self, process: _TerminalProcess, text: str) -> None:
        if not text:
            return
        remaining = _MAX_OUTPUT_BYTES - len(process.output.encode("utf-8"))
        if remaining <= 0:
            process.output_truncated = True
            return
        chunk = text.encode("utf-8")[:remaining].decode("utf-8", errors="ignore")
        process.output += chunk
        process.output_truncated = process.output_truncated or len(chunk) < len(text)
        process.updated_at = time.time()

    def _running(self, process_id: str) -> _TerminalProcess:
        process = self._processes.get(process_id)
        if process is None:
            raise KeyError(process_id)
        if process.status != "running":
            raise ValueError("terminal process is not running")
        return process

    @staticmethod
    def _view(process: _TerminalProcess) -> dict[str, Any]:
        return {
            "id": process.id,
            "command": list(process.command),
            "cwd": process.cwd,
            "started_at": process.started_at,
            "updated_at": process.updated_at,
            "status": process.status,
            "exit_code": process.exit_code,
            "error": process.error,
            "output": process.output,
            "output_truncated": process.output_truncated,
        }


def _command(value: list[str]) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > _MAX_COMMAND_ARGS:
        raise ValueError("terminal command must contain 1-128 arguments")
    if not all(isinstance(part, str) and part for part in value):
        raise ValueError("terminal command arguments must be non-empty strings")
    if len("\0".join(value).encode("utf-8")) > _MAX_COMMAND_BYTES:
        raise ValueError("terminal command is too large")
    return list(value)


def _cwd(workspace: Path, value: str) -> Path:
    candidate = (workspace / value).resolve() if value else workspace
    try:
        candidate.relative_to(workspace)
    except ValueError:
        raise ValueError("terminal cwd must stay inside the workspace") from None
    if not candidate.is_dir():
        raise ValueError("terminal cwd must be an existing directory")
    return candidate
