"""Browser-facing approval broker for app-server initiated requests."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from .process import ServerRequest


ApprovalPublisher = Callable[[str, dict[str, Any]], Awaitable[None]]
_SUPPORTED_METHODS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/permissions/requestApproval",
}
_DECISIONS = {
    "allow": "accept",
    "always": "acceptForSession",
    "deny": "decline",
    "cancel": "cancel",
}


class CodexApprovalBroker:
    """Suspend app-server approval callbacks until the browser decides."""

    def __init__(self, *, timeout: float = 300.0):
        self.timeout = timeout
        self.publisher: ApprovalPublisher | None = None
        self._pending: dict[tuple[str, str], asyncio.Future[str]] = {}

    async def handle(self, request: ServerRequest) -> dict[str, Any]:
        if request.method not in _SUPPORTED_METHODS:
            raise ValueError("unsupported app-server client request")
        thread_id = _required_string(request.params, "threadId")
        request_id = str(request.id)
        key = (thread_id, request_id)
        if key in self._pending:
            raise ValueError("duplicate app-server approval request")

        future = asyncio.get_running_loop().create_future()
        self._pending[key] = future
        try:
            if self.publisher is None:
                if request.method == "item/permissions/requestApproval":
                    return {"permissions": {}, "scope": "turn"}
                return {"decision": "decline"}
            await self.publisher(thread_id, _approval_event(request, request_id))
            try:
                decision = await asyncio.wait_for(asyncio.shield(future), self.timeout)
            except TimeoutError:
                decision = "decline"
            if request.method == "item/permissions/requestApproval":
                granted = request.params.get("permissions") if decision in {
                    "accept", "acceptForSession"
                } else {}
                return {
                    "permissions": granted if isinstance(granted, dict) else {},
                    "scope": "session" if decision == "acceptForSession" else "turn",
                }
            return {"decision": decision}
        finally:
            self._pending.pop(key, None)
            if not future.done():
                future.cancel()

    def submit(self, thread_id: str, request_id: str, decision: str) -> bool:
        native_decision = _DECISIONS.get(decision)
        if native_decision is None:
            raise ValueError("invalid approval decision")
        future = self._pending.get((thread_id, request_id))
        if future is None or future.done():
            return False
        future.set_result(native_decision)
        return True

    async def close(self) -> None:
        pending = tuple(self._pending.values())
        self._pending.clear()
        for future in pending:
            if not future.done():
                future.set_result("decline")


def _required_string(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"approval request is missing {key}")
    return value


def _approval_event(request: ServerRequest, request_id: str) -> dict[str, Any]:
    params = request.params
    if request.method == "item/commandExecution/requestApproval":
        summary = params.get("command") or params.get("reason") or "Command execution"
        tool = "Bash"
    elif request.method == "item/fileChange/requestApproval":
        summary = params.get("reason") or "Apply file changes"
        tool = "FileChange"
    else:
        summary = _permissions_summary(params)
        tool = "Permissions"
    return {
        "id": request_id,
        "tool": tool,
        "summary": str(summary),
    }


def _permissions_summary(params: dict[str, Any]) -> str:
    requested = params.get("permissions")
    if not isinstance(requested, dict):
        return str(params.get("reason") or "Additional permissions")
    parts = []
    network = requested.get("network")
    if isinstance(network, dict) and network.get("enabled") is True:
        parts.append("Network access")
    file_system = requested.get("fileSystem")
    entries = file_system.get("entries") if isinstance(file_system, dict) else None
    if isinstance(entries, list):
        for entry in entries[:8]:
            if not isinstance(entry, dict):
                continue
            path = entry.get("path")
            if not isinstance(path, dict):
                continue
            label = path.get("path") or path.get("pattern")
            if isinstance(label, str):
                parts.append(f"{entry.get('access', 'access')}: {label}")
    reason = params.get("reason")
    if isinstance(reason, str) and reason:
        parts.insert(0, reason)
    return "\n".join(parts) or "Additional filesystem permissions"
