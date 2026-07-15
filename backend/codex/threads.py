"""Stable Codex thread operations, independent of FastAPI and UI shapes."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass
import json
from pathlib import Path
import threading
import time
from typing import Any, Protocol

from ..settings import atomic_write_text
from .process import AppServerProtocolError, AppServerResponseError


_THREAD_LIST_TIMEOUT_SECONDS = 4.0
_THREAD_LIST_CACHE_SECONDS = 30.0
_THREAD_LIST_CACHE_MAX_PAGES = 32
_PERMISSION_MODES = frozenset({"default", "plan", "bypassPermissions"})


class Requester(Protocol):
    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Awaitable[Any]: ...


@dataclass(frozen=True)
class ThreadPage:
    data: list[dict[str, Any]]
    next_cursor: str | None


@dataclass(frozen=True)
class WorkspaceEntry:
    path: str
    name: str
    primary: bool = False


class CodexThreadService:
    """Workspace-scoped facade over the stable app-server thread methods."""

    def __init__(
        self,
        requester: Requester,
        workspace: Path,
        *,
        approval_policy: str | None = None,
        sandbox: str | None = None,
    ):
        self.requester = requester
        self.workspace = Path(workspace).resolve()
        self.approval_policy = approval_policy
        self.sandbox = sandbox
        self._metadata_path = self.workspace / ".muselab-codex" / "threads.json"
        self._workspaces_path = self.workspace / ".muselab-codex" / "workspaces.json"
        self._metadata_lock = threading.RLock()
        self._pinned = self._load_pinned()
        self._settings = self._load_settings()
        self._workspace_lock = threading.RLock()
        self._workspaces = self._load_workspaces()
        self._pending: dict[str, dict[str, Any]] = {}
        self._list_cache: dict[
            tuple[str | None, int, bool, str | None],
            tuple[float, ThreadPage],
        ] = {}
        self._list_lock = asyncio.Lock()

    async def start(
        self,
        *,
        name: str | None = None,
        model: str | None = None,
        model_provider: str | None = None,
        config: dict[str, Any] | None = None,
        cwd: str | Path | None = None,
    ) -> dict[str, Any]:
        self._sync_runtime_generation()
        params: dict[str, Any] = {
            "cwd": self.resolve_workspace(cwd),
            "ephemeral": False,
        }
        # Codex owns the permission defaults in config.toml.  Only send these
        # fields when the embedding caller explicitly requests an override;
        # otherwise app-server inherits the user's native Codex configuration.
        if self.approval_policy is not None:
            params["approvalPolicy"] = self.approval_policy
        if self.sandbox is not None:
            params["sandbox"] = self.sandbox
        if model:
            params["model"] = model
        if model_provider:
            params["modelProvider"] = model_provider
        if config:
            params["config"] = config
        result = await self.requester.request("thread/start", params)
        # The first request may lazily start/restart app-server and advance its
        # runtime generation.  Observe that change before registering this
        # empty thread in the pending sidecar; otherwise the next resume()
        # clears the just-created entry, then app-server rejects resume for a
        # pre-first-turn thread with -32600.
        self._sync_runtime_generation()
        thread = _thread_from_result("thread/start", result)
        if name is not None:
            await self.rename(thread["id"], name)
            thread = dict(thread)
            thread["name"] = name.strip()
        self._pending[thread["id"]] = dict(thread)
        self.invalidate_list_cache()
        return self._with_local_metadata(thread)

    async def list(
        self,
        *,
        cursor: str | None = None,
        limit: int = 100,
        archived: bool = False,
        search_term: str | None = None,
    ) -> ThreadPage:
        self._sync_runtime_generation()
        if limit < 1:
            raise ValueError("thread list limit must be positive")
        key = (cursor, limit, archived, search_term)
        cached = self._cached_page(key)
        if cached is not None:
            return cached
        params: dict[str, Any] = {
            "cwd": self._workspace_filter(),
            "limit": limit,
            "archived": archived,
            "sortKey": "updated_at",
            "sortDirection": "desc",
        }
        if cursor:
            params["cursor"] = cursor
        if search_term:
            params["searchTerm"] = search_term
        async with self._list_lock:
            # Multiple tabs commonly refresh together.  Let exactly one call
            # cross the stdio control plane and share its result with waiters.
            cached = self._cached_page(key)
            if cached is not None:
                return cached
            # thread/list is UI control-plane work and is normally sub-second.
            # Bound it well below the generic turn timeout so a wedged
            # generation is discarded quickly instead of leaving refresh
            # blank for a minute.
            result = await self.requester.request(
                "thread/list", params, timeout=_THREAD_LIST_TIMEOUT_SECONDS)
            self._sync_runtime_generation()
            if not isinstance(result, dict) or not isinstance(result.get("data"), list):
                raise AppServerProtocolError("thread/list returned an invalid result")
            data = result["data"]
            if not all(isinstance(thread, dict) for thread in data):
                raise AppServerProtocolError("thread/list returned an invalid thread")
            next_cursor = result.get("nextCursor")
            if next_cursor is not None and not isinstance(next_cursor, str):
                raise AppServerProtocolError("thread/list returned an invalid cursor")
            listed_ids = {thread.get("id") for thread in data}
            # A pre-first-turn thread can appear in thread/list while
            # thread/resume still rejects it with -32600.  Listing alone is
            # therefore not evidence that Codex has materialized the rollout;
            # keep the pending marker until read/resume succeeds.
            # Pending empty threads belong only on the first page. Repeating
            # them for every opaque app-server cursor would create duplicates.
            pending = [] if archived or cursor else [
                thread for thread in self._matching_pending(search_term)
                if thread.get("id") not in listed_ids
            ]
            combined = [self._with_local_metadata(thread) for thread in pending + data]
            combined.sort(key=lambda thread: thread.get("updatedAt", 0), reverse=True)
            page = ThreadPage(data=combined, next_cursor=next_cursor)
            self._list_cache[key] = (time.monotonic(), page)
            # Search terms and opaque cursors are part of the key. Without a
            # cap, rapidly changing the sidebar search can retain an arbitrary
            # number of page snapshots until the next explicit invalidation.
            # Dict order tracks insertion age, which is the right eviction
            # order because cache reads deliberately do not extend the TTL.
            while len(self._list_cache) > _THREAD_LIST_CACHE_MAX_PAGES:
                self._list_cache.pop(next(iter(self._list_cache)))
            return self._copy_page(page)

    async def read(
        self,
        thread_id: str,
        *,
        include_turns: bool = True,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        self._sync_runtime_generation()
        clean_id = _thread_id(thread_id)
        try:
            result = await self.requester.request(
                "thread/read",
                {
                    "threadId": clean_id,
                    "includeTurns": include_turns,
                },
                timeout=timeout,
            )
        except AppServerResponseError as exc:
            self._sync_runtime_generation()
            if exc.code == -32600 and clean_id in self._pending:
                return self._with_local_metadata(self._pending[clean_id])
            raise
        thread = _thread_from_result("thread/read", result)
        return self._with_local_metadata(thread)

    async def resume(
        self,
        thread_id: str,
        *,
        model: str | None = None,
        model_provider: str | None = None,
        config: dict[str, Any] | None = None,
        cwd: str | Path | None = None,
    ) -> dict[str, Any]:
        self._sync_runtime_generation()
        clean_id = _thread_id(thread_id)
        params: dict[str, Any] = {"threadId": clean_id}
        if cwd is not None:
            params["cwd"] = self.resolve_workspace(cwd)
        # ``thread/resume`` treats approvalPolicy and sandbox as overrides.
        # Omitting them preserves the policy persisted with the original
        # thread (including threads created by another Codex client).  Sending
        # this service's defaults here silently downgraded full-access threads
        # to workspace-write after an app-server restart.
        if model:
            params["model"] = model
        if model_provider:
            params["modelProvider"] = model_provider
        if config:
            params["config"] = config
        try:
            result = await self.requester.request("thread/resume", params)
        except AppServerResponseError as exc:
            self._sync_runtime_generation()
            if exc.code == -32600 and clean_id in self._pending:
                return self._with_local_metadata(self._pending[clean_id])
            raise
        thread = _thread_from_result("thread/resume", result)
        return self._with_local_metadata(thread)

    def mark_materialized(self, thread_id: str) -> None:
        """Forget the empty-thread sidecar after Codex accepts its first turn.

        thread/list, thread/read, and even thread/resume may temporarily
        succeed for a pre-first-turn thread without creating its rollout.
        turn/start is the first reliable materialization boundary.
        """
        self._pending.pop(_thread_id(thread_id), None)
        self.invalidate_list_cache()

    async def rename(self, thread_id: str, name: str) -> None:
        self._sync_runtime_generation()
        clean_id = _thread_id(thread_id)
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("thread name cannot be empty")
        result = await self.requester.request("thread/name/set", {
            "threadId": clean_id,
            "name": clean_name,
        })
        self._sync_runtime_generation()
        _empty_result("thread/name/set", result)
        if clean_id in self._pending:
            self._pending[clean_id]["name"] = clean_name
        self.invalidate_list_cache()

    async def delete(self, thread_id: str) -> None:
        self._sync_runtime_generation()
        clean_id = _thread_id(thread_id)
        result = await self.requester.request("thread/delete", {
            "threadId": clean_id,
        })
        _empty_result("thread/delete", result)
        self._pending.pop(clean_id, None)
        self._delete_local_metadata(clean_id)
        self.invalidate_list_cache()

    async def fork(
        self,
        thread_id: str,
        *,
        last_turn_id: str | None = None,
        model: str | None = None,
        model_provider: str | None = None,
        config: dict[str, Any] | None = None,
        cwd: str | Path | None = None,
    ) -> dict[str, Any]:
        self._sync_runtime_generation()
        params: dict[str, Any] = {
            "threadId": _thread_id(thread_id),
            "ephemeral": False,
        }
        if cwd is not None:
            params["cwd"] = self.resolve_workspace(cwd)
        if self.approval_policy is not None:
            params["approvalPolicy"] = self.approval_policy
        if self.sandbox is not None:
            params["sandbox"] = self.sandbox
        if last_turn_id:
            params["lastTurnId"] = last_turn_id
        if model:
            params["model"] = model
        if model_provider:
            params["modelProvider"] = model_provider
        if config:
            params["config"] = config
        result = await self.requester.request("thread/fork", params)
        self._sync_runtime_generation()
        thread = _thread_from_result("thread/fork", result)
        self._pending[thread["id"]] = dict(thread)
        self.invalidate_list_cache()
        return self._with_local_metadata(thread)

    def list_workspaces(self) -> list[WorkspaceEntry]:
        with self._workspace_lock:
            return [
                WorkspaceEntry(path=path, name=name, primary=path == str(self.workspace))
                for path, name in self._workspaces.items()
            ]

    def register_workspace(self, value: str | Path, name: str | None = None) -> WorkspaceEntry:
        path = self._validated_workspace(value)
        clean_name = (name or path.name or str(path)).strip()
        if not clean_name:
            raise ValueError("workspace name cannot be empty")
        with self._workspace_lock:
            updated = dict(self._workspaces)
            updated[str(path)] = clean_name
            self._save_workspaces(updated)
            self._workspaces = updated
        self.invalidate_list_cache()
        return WorkspaceEntry(
            path=str(path), name=clean_name, primary=path == self.workspace)

    def remove_workspace(self, value: str | Path) -> None:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.workspace / path
        path = path.resolve()
        if path == self.workspace:
            raise ValueError("primary workspace cannot be removed")
        with self._workspace_lock:
            if str(path) not in self._workspaces:
                raise ValueError("workspace is not registered")
            updated = dict(self._workspaces)
            del updated[str(path)]
            self._save_workspaces(updated)
            self._workspaces = updated
        self.invalidate_list_cache()

    def resolve_workspace(self, value: str | Path | None) -> str:
        if value is None or not str(value).strip():
            return str(self.workspace)
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.workspace / path
        clean = str(path.resolve())
        with self._workspace_lock:
            if clean not in self._workspaces:
                raise ValueError("workspace is not registered")
        return clean

    def contains_workspace(self, value: Any) -> bool:
        if not isinstance(value, str) or not value.strip():
            return False
        try:
            clean = str(Path(value).expanduser().resolve())
        except (OSError, RuntimeError):
            return False
        with self._workspace_lock:
            return clean in self._workspaces

    def _workspace_filter(self) -> str | list[str]:
        with self._workspace_lock:
            paths = list(self._workspaces)
        return paths[0] if len(paths) == 1 else paths

    def _validated_workspace(self, value: str | Path) -> Path:
        raw = str(value).strip()
        if not raw:
            raise ValueError("workspace path cannot be empty")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = self.workspace / path
        path = path.resolve()
        forbidden = {Path("/"), Path("/home"), Path("/root"), Path("/etc"),
                     Path("/usr"), Path("/var"), Path("/boot")}
        if path in forbidden:
            raise ValueError("workspace path is too broad or sensitive")
        if not path.exists() or not path.is_dir():
            raise ValueError("workspace path must be an existing directory")
        return path

    def _load_workspaces(self) -> dict[str, str]:
        workspaces = {str(self.workspace): self.workspace.name or str(self.workspace)}
        try:
            payload = json.loads(self._workspaces_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return workspaces
        values = payload.get("workspaces") if isinstance(payload, dict) else None
        if not isinstance(values, list):
            return workspaces
        for item in values:
            if not isinstance(item, dict):
                continue
            try:
                path = self._validated_workspace(str(item.get("path") or ""))
            except ValueError:
                continue
            name = str(item.get("name") or path.name or path).strip()
            workspaces[str(path)] = name
        return workspaces

    def _save_workspaces(self, workspaces: dict[str, str]) -> None:
        payload = {"workspaces": [
            {"path": path, "name": name}
            for path, name in workspaces.items()
        ]}
        atomic_write_text(
            self._workspaces_path,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )

    def set_pinned(self, thread_id: str, pinned: bool) -> None:
        """Persist application-owned thread presentation metadata.

        Pinning is a muselab UI affordance and is not part of the Codex
        app-server thread protocol. Keep it in a small workspace sidecar while
        leaving names, turns, and lifecycle state owned by Codex.
        """
        clean_id = _thread_id(thread_id)
        with self._metadata_lock:
            changed = clean_id not in self._pinned if pinned else clean_id in self._pinned
            if not changed:
                return
            updated = set(self._pinned)
            if pinned:
                updated.add(clean_id)
            else:
                updated.discard(clean_id)
            self._write_metadata(updated, self._settings)
            self._pinned = updated
        self.invalidate_list_cache()

    def set_effort(self, thread_id: str, effort: str) -> None:
        """Persist the browser's effective next-turn effort override.

        ``thread/resume.config`` accepts ``model_reasoning_effort`` but the
        stable thread/list and thread/read responses do not expose that config.
        Without this small compatibility sidecar, the next list poll reports
        an empty effort and the UI incorrectly falls back to ``auto`` before
        the configured turn has even started. An explicit empty string is
        retained because selecting auto must override a previous transcript's
        non-empty effort too.
        """
        clean_id = _thread_id(thread_id)
        clean_effort = effort.strip()
        with self._metadata_lock:
            current = self._settings.get(clean_id, {})
            if "effort" in current and current.get("effort") == clean_effort:
                return
            updated = {
                key: dict(value) for key, value in self._settings.items()
            }
            updated.setdefault(clean_id, {})["effort"] = clean_effort
            self._write_metadata(self._pinned, updated)
            self._settings = updated
        self.invalidate_list_cache()

    def set_permission(self, thread_id: str, permission: str) -> None:
        """Persist the browser's next-turn permission profile.

        Permission is a turn/start choice rather than a native thread field,
        so stable thread/list and thread/read responses cannot restore it on
        their own. Keep only the three profiles exposed by this UI in the
        workspace sidecar; the turn service still translates the selected
        value into native approval/sandbox overrides for each new turn.
        """
        clean_id = _thread_id(thread_id)
        clean_permission = permission.strip()
        if clean_permission not in _PERMISSION_MODES:
            raise ValueError("unknown permission mode")
        with self._metadata_lock:
            current = self._settings.get(clean_id, {})
            if current.get("permission") == clean_permission:
                return
            updated = {
                key: dict(value) for key, value in self._settings.items()
            }
            updated.setdefault(clean_id, {})["permission"] = clean_permission
            self._write_metadata(self._pinned, updated)
            self._settings = updated
        self.invalidate_list_cache()

    async def set_thinking(self, thread_id: str, enabled: bool) -> None:
        """Apply and persist the native reasoning-summary preference.

        Codex exposes this as ``thread/settings/update.summary`` rather than a
        legacy boolean.  ``none`` suppresses reasoning-summary events; ``auto``
        restores the normal model-selected summary.  A pre-first-turn thread
        may reject settings/update with -32600, so retain the preference in the
        sidecar and let ``turn/start`` apply it explicitly.
        """
        self._sync_runtime_generation()
        clean_id = _thread_id(thread_id)
        try:
            result = await self.requester.request("thread/settings/update", {
                "threadId": clean_id,
                "summary": "auto" if enabled else "none",
            })
            if not isinstance(result, dict):
                raise AppServerProtocolError(
                    "thread/settings/update returned an invalid result")
        except AppServerResponseError as exc:
            self._sync_runtime_generation()
            if exc.code != -32600 or clean_id not in self._pending:
                raise
        with self._metadata_lock:
            current = self._settings.get(clean_id, {})
            if current.get("thinking") is enabled:
                return
            updated = {
                key: dict(value) for key, value in self._settings.items()
            }
            updated.setdefault(clean_id, {})["thinking"] = enabled
            self._write_metadata(self._pinned, updated)
            self._settings = updated
        self.invalidate_list_cache()

    def reasoning_summary(self, thread_id: str) -> str | None:
        """Return an explicit turn/start summary override, if the user set one."""
        clean_id = _thread_id(thread_id)
        with self._metadata_lock:
            value = self._settings.get(clean_id, {}).get("thinking")
        if isinstance(value, bool):
            return "auto" if value else "none"
        return None

    async def children(self, parent_thread_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        """List materialized subagent threads belonging to one parent thread.

        The stable app-server protocol exposes ``parentThreadId`` on a Thread,
        but does not offer a parent filter on ``thread/list``.  Page through the
        workspace-scoped list rather than inferring children from tool output.
        """
        parent_id = _thread_id(parent_thread_id)
        cursor: str | None = None
        seen_cursors: set[str] = set()
        children: list[dict[str, Any]] = []
        while True:
            page = await self.list(cursor=cursor, limit=limit)
            children.extend(
                thread for thread in page.data
                if thread.get("parentThreadId") == parent_id
            )
            cursor = page.next_cursor
            if cursor is None:
                break
            if cursor in seen_cursors:
                raise AppServerProtocolError("thread/list returned a repeated cursor")
            seen_cursors.add(cursor)
        return children

    def _matching_pending(self, search_term: str | None) -> list[dict[str, Any]]:
        pending = [dict(thread) for thread in self._pending.values()]
        if not search_term:
            return pending
        needle = search_term.casefold()
        return [thread for thread in pending
                if needle in str(thread.get("name") or thread.get("preview") or "").casefold()]

    def _load_pinned(self) -> set[str]:
        try:
            payload = json.loads(self._metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return set()
        values = payload.get("pinned") if isinstance(payload, dict) else None
        if not isinstance(values, list):
            return set()
        return {value.strip() for value in values
                if isinstance(value, str) and value.strip()}

    def _load_settings(self) -> dict[str, dict[str, Any]]:
        try:
            payload = json.loads(self._metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        values = payload.get("settings") if isinstance(payload, dict) else None
        if not isinstance(values, dict):
            return {}
        settings: dict[str, dict[str, Any]] = {}
        for thread_id, raw in values.items():
            if not isinstance(thread_id, str) or not thread_id.strip():
                continue
            if not isinstance(raw, dict):
                continue
            item: dict[str, Any] = {}
            if isinstance(raw.get("effort"), str):
                item["effort"] = raw["effort"].strip()
            if isinstance(raw.get("thinking"), bool):
                item["thinking"] = raw["thinking"]
            if raw.get("permission") in _PERMISSION_MODES:
                item["permission"] = raw["permission"]
            if item:
                settings[thread_id.strip()] = item
        return settings

    def _write_metadata(
        self,
        pinned: set[str],
        settings: dict[str, dict[str, Any]],
    ) -> None:
        payload: dict[str, Any] = {"pinned": sorted(pinned)}
        if settings:
            payload["settings"] = settings
        atomic_write_text(
            self._metadata_path,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )

    def _delete_local_metadata(self, thread_id: str) -> None:
        with self._metadata_lock:
            if thread_id not in self._pinned and thread_id not in self._settings:
                return
            pinned = set(self._pinned)
            pinned.discard(thread_id)
            settings = {
                key: dict(value) for key, value in self._settings.items()
                if key != thread_id
            }
            self._write_metadata(pinned, settings)
            self._pinned = pinned
            self._settings = settings

    def _with_local_metadata(self, thread: dict[str, Any]) -> dict[str, Any]:
        decorated = dict(thread)
        thread_id = decorated.get("id")
        with self._metadata_lock:
            decorated["pinned"] = (
                isinstance(thread_id, str) and thread_id in self._pinned
            )
            local_settings = (
                dict(self._settings.get(thread_id, {}))
                if isinstance(thread_id, str) else {}
            )
        if local_settings:
            native_settings = decorated.get("_settings")
            merged = dict(native_settings) if isinstance(native_settings, dict) else {}
            merged.update(local_settings)
            decorated["_settings"] = merged
        return decorated

    def invalidate_list_cache(self) -> None:
        self._list_cache.clear()

    def _cached_page(
        self,
        key: tuple[str | None, int, bool, str | None],
    ) -> ThreadPage | None:
        cached = self._list_cache.get(key)
        if cached is None:
            return None
        cached_at, page = cached
        if time.monotonic() - cached_at >= _THREAD_LIST_CACHE_SECONDS:
            self._list_cache.pop(key, None)
            return None
        return self._copy_page(page)

    @staticmethod
    def _copy_page(page: ThreadPage) -> ThreadPage:
        return ThreadPage(
            data=[dict(thread) for thread in page.data],
            next_cursor=page.next_cursor,
        )

    def _sync_runtime_generation(self) -> None:
        health_fn = getattr(self.requester, "health", None)
        if not callable(health_fn):
            return
        restart_count = health_fn().restart_count
        previous = getattr(self, "_restart_count", restart_count)
        if restart_count != previous:
            self._pending.clear()
            self.invalidate_list_cache()
        self._restart_count = restart_count


def _thread_id(value: str) -> str:
    clean = value.strip()
    if not clean:
        raise ValueError("thread id cannot be empty")
    return clean


def _thread_from_result(method: str, result: Any) -> dict[str, Any]:
    if not isinstance(result, dict) or not isinstance(result.get("thread"), dict):
        raise AppServerProtocolError(f"{method} returned an invalid thread result")
    thread = result["thread"]
    if not isinstance(thread.get("id"), str) or not thread["id"]:
        raise AppServerProtocolError(f"{method} returned a thread without an id")
    return thread


def _empty_result(method: str, result: Any) -> None:
    if result != {}:
        raise AppServerProtocolError(f"{method} returned an invalid result")
