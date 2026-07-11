"""Bounded transcript reads that cannot monopolize the shared app-server."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any, Protocol

from .process import (
    AppServerProtocolError,
    AppServerResponseError,
    AppServerTimeoutError,
)
from .threads import CodexThreadService
from .transcripts import CodexTranscriptStore


class RestartableRuntime(Protocol):
    async def restart(self) -> Any: ...


class RestartableEvents(Protocol):
    async def start(self) -> None: ...


class CodexHistoryService:
    """Read full history with a timeout and metadata-only degradation.

    ``thread/read(includeTurns=true)`` has no stable pagination in the pinned
    protocol. A very large rollout can therefore occupy app-server for over a
    minute even when the HTTP caller asked only for ``tail=80``. Since one
    app-server serves every browser thread, that blocks model discovery,
    lists, and new turns too.

    The first slow read for a thread is bounded. On timeout we restart the
    shared process to cancel the still-running server operation, mark that
    thread degraded for this application process, and serve metadata without
    turns. Per-thread locks prevent concurrent browser startup requests from
    causing a restart storm.
    """

    def __init__(
        self,
        threads: CodexThreadService,
        runtime: RestartableRuntime,
        events: RestartableEvents,
        *,
        timeout: float = 8.0,
        transcripts: CodexTranscriptStore | None = None,
    ):
        if timeout <= 0:
            raise ValueError("history timeout must be positive")
        self.threads = threads
        self.runtime = runtime
        self.events = events
        self.timeout = timeout
        self.transcripts = transcripts or CodexTranscriptStore()
        self._degraded: set[str] = set()
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        # app-server is shared by every browser tab and handles transcript
        # reads on the same stdio connection.  Letting different threads issue
        # large ``thread/read`` requests concurrently makes their request
        # timeouts include time spent waiting behind one another.  The waiter
        # can then restart app-server while the first read is still healthy,
        # incorrectly degrading both sessions.  Serialize the expensive reads
        # so the timeout measures one transcript operation at a time.
        self._full_read_lock = asyncio.Lock()

    async def read(self, thread_id: str) -> dict[str, Any]:
        clean_id = thread_id.strip()
        if not clean_id:
            raise ValueError("thread id cannot be empty")
        async with self._locks[clean_id]:
            try:
                async with self._full_read_lock:
                    native = await self._read_native_items(clean_id)
            except AppServerTimeoutError:
                # A native item read is read-only. Prefer the local Codex-owned
                # JSONL projection over restarting the shared runtime merely
                # because a large/paginated history response was slow.
                native = None
            if native is not None:
                return native
            snapshot = await self.transcripts.read(clean_id)
            if snapshot is not None:
                thread = await self.threads.read(clean_id, include_turns=False)
                return {
                    **thread,
                    "turns": _project_turns(snapshot.items),
                    "_settings": dict(snapshot.settings or {}),
                }
            if clean_id in self._degraded:
                return await self.threads.read(clean_id, include_turns=False)
            try:
                async with self._full_read_lock:
                    return await self.threads.read(
                        clean_id,
                        include_turns=True,
                        timeout=self.timeout,
                    )
            except AppServerTimeoutError:
                self._degraded.add(clean_id)
                await self.runtime.restart()
                await self.events.start()
                return await self.threads.read(clean_id, include_turns=False)

    async def _read_native_items(self, thread_id: str) -> dict[str, Any] | None:
        """Prefer Codex's paginated item API; JSONL is compatibility-only."""
        requester = getattr(self.threads, "requester", None)
        if requester is None:
            return None
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        try:
            while True:
                params: dict[str, Any] = {
                    "threadId": thread_id,
                    "limit": 200,
                    "sortDirection": "asc",
                }
                if cursor:
                    params["cursor"] = cursor
                read_request = getattr(requester, "read_request", None)
                request = read_request if callable(read_request) else requester.request
                result = await request(
                    "thread/items/list", params, timeout=self.timeout)
                if not isinstance(result, dict) or not isinstance(result.get("data"), list):
                    raise AppServerProtocolError(
                        "thread/items/list returned an invalid result")
                page = result["data"]
                if not all(isinstance(item, dict) for item in page):
                    raise AppServerProtocolError(
                        "thread/items/list returned an invalid item")
                items.extend(page)
                next_cursor = result.get("nextCursor")
                if next_cursor is None:
                    break
                if not isinstance(next_cursor, str) or not next_cursor:
                    raise AppServerProtocolError(
                        "thread/items/list returned an invalid cursor")
                cursor = next_cursor
        except AppServerResponseError as exc:
            # 0.144.1 exposes this method under the experimental API. Keep a
            # bounded read-only rollout fallback for older installed CLIs.
            if exc.code in {-32600, -32601}:
                return None
            raise
        thread = await self.threads.read(thread_id, include_turns=False)
        return {**thread, "turns": _project_turns(tuple(items))}

    def degraded(self, thread_id: str) -> bool:
        return thread_id in self._degraded


def _project_turns(items: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    """Retain transcript turn boundaries instead of returning one fake turn."""
    turns: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    for item in items:
        if item.get("type") == "userMessage" and current:
            turns.append({"items": current})
            current = []
        current.append(dict(item))
    if current:
        turns.append({"items": current})
    return turns
