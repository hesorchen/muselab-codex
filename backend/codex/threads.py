"""Stable Codex thread operations, independent of FastAPI and UI shapes."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass
import json
import os
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
# ``model/list.serviceTiers`` is the protocol source of truth.  Codex 0.144.1
# advertises the user-facing Fast tier with the native id ``priority`` while
# older builds/config examples used ``fast``.  Accept both so existing sidecar
# state remains readable and the browser can persist the catalog-provided id.
_SERVICE_TIERS = frozenset({"", "fast", "priority"})
_WORKSPACE_FORBIDDEN = frozenset({
    Path("/"), Path("/home"), Path("/root"), Path("/etc"), Path("/usr"),
    Path("/var"), Path("/boot"),
})
_WORKSPACE_BROWSER_DENIED_ROOTS = (
    Path("/boot"), Path("/dev"), Path("/etc"), Path("/proc"), Path("/root"),
    Path("/run"), Path("/sys"), Path("/usr"), Path("/var"),
)
_WORKSPACE_PROJECT_MARKERS = (
    (".git", "Git"),
    ("AGENTS.md", "Codex"),
    ("pyproject.toml", "Python"),
    ("package.json", "Node.js"),
    ("Cargo.toml", "Rust"),
    ("go.mod", "Go"),
    (".vscode", "VS Code"),
)
_WORKSPACE_BROWSER_LIMIT = 300


def normalize_service_tier(value: str | None) -> str | None:
    """Normalize the service tiers exposed by this UI.

    ``None`` means the caller did not specify an override.  An empty string is
    intentionally different: it is the explicit Standard tier and must clear
    a previously selected Fast tier by becoming JSON ``null`` at the native
    protocol boundary.
    """
    if value is None:
        return None
    clean = value.strip()
    if clean not in _SERVICE_TIERS:
        raise ValueError("unknown service tier")
    return clean


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
        self._pending_path = (
            self.workspace / ".muselab-codex" / "pending-threads.json")
        self._workspaces_path = self.workspace / ".muselab-codex" / "workspaces.json"
        self._metadata_lock = threading.RLock()
        self._pinned = self._load_pinned()
        self._settings = self._load_settings()
        self._workspace_lock = threading.RLock()
        self._workspaces = self._load_workspaces()
        self._pending, self._materialized = self._load_pending()
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
        service_tier: str | None = None,
        cwd: str | Path | None = None,
    ) -> dict[str, Any]:
        self._sync_runtime_generation()
        clean_service_tier = normalize_service_tier(service_tier)
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
        if clean_service_tier is not None:
            params["serviceTier"] = clean_service_tier or None
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
        self._pending[thread["id"]] = _pending_snapshot(thread)
        self._materialized.discard(thread["id"])
        self._write_pending()
        if clean_service_tier is not None:
            self.remember_service_tier(thread["id"], clean_service_tier)
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
            result = await self._read_request(
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
            result = await self._read_request(
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
        service_tier: str | None = None,
        cwd: str | Path | None = None,
    ) -> dict[str, Any]:
        self._sync_runtime_generation()
        clean_id = _thread_id(thread_id)
        clean_service_tier = normalize_service_tier(service_tier)
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
        if clean_service_tier is not None:
            params["serviceTier"] = clean_service_tier or None
        try:
            result = await self.requester.request("thread/resume", params)
        except AppServerResponseError as exc:
            self._sync_runtime_generation()
            if exc.code == -32600 and clean_id in self._pending:
                thread = dict(self._pending[clean_id])
            else:
                raise
        else:
            thread = _thread_from_result("thread/resume", result)
        if clean_service_tier is not None:
            self.remember_service_tier(clean_id, clean_service_tier)
        return self._with_local_metadata(thread)

    def mark_materialized(self, thread_id: str) -> None:
        """Forget the empty-thread sidecar after Codex accepts its first turn.

        thread/list, thread/read, and even thread/resume may temporarily
        succeed for a pre-first-turn thread without creating its rollout.
        turn/start is the first reliable materialization boundary.
        """
        clean_id = _thread_id(thread_id)
        self._pending.pop(clean_id, None)
        self._materialized.add(clean_id)
        self._write_pending()
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
            self._pending[clean_id]["updatedAt"] = time.time()
            self._write_pending()
        self.invalidate_list_cache()

    async def delete(self, thread_id: str) -> None:
        self._sync_runtime_generation()
        clean_id = _thread_id(thread_id)
        try:
            result = await self.requester.request("thread/delete", {
                "threadId": clean_id,
            })
            _empty_result("thread/delete", result)
        except AppServerResponseError as exc:
            # Empty pre-first-turn threads may exist only in the local durable
            # sidecar after an app-server restart. Deleting such a known local
            # thread is still a successful lifecycle operation.
            if exc.code != -32600 or clean_id not in self._pending:
                raise
        self._pending.pop(clean_id, None)
        self._materialized.discard(clean_id)
        self._write_pending()
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
        service_tier: str | None = None,
        cwd: str | Path | None = None,
    ) -> dict[str, Any]:
        self._sync_runtime_generation()
        clean_service_tier = normalize_service_tier(service_tier)
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
        if clean_service_tier is not None:
            params["serviceTier"] = clean_service_tier or None
        result = await self.requester.request("thread/fork", params)
        self._sync_runtime_generation()
        thread = _thread_from_result("thread/fork", result)
        self._pending[thread["id"]] = _pending_snapshot(thread)
        self._materialized.discard(thread["id"])
        self._write_pending()
        if clean_service_tier is not None:
            self.remember_service_tier(thread["id"], clean_service_tier)
        self.invalidate_list_cache()
        return self._with_local_metadata(thread)

    def list_workspaces(self) -> list[WorkspaceEntry]:
        with self._workspace_lock:
            return [
                WorkspaceEntry(path=path, name=name, primary=path == str(self.workspace))
                for path, name in self._workspaces.items()
            ]

    def browse_workspace_directories(self, value: str | Path | None = None) -> dict[str, Any]:
        """List safe server-side folders for the authenticated folder picker.

        File APIs intentionally cannot escape a registered workspace.  Adding
        one is the single exception, so browsing is bounded to the user's home
        (when the primary workspace lives there) or to the parent of each
        already-registered root.  This exposes useful siblings/children while
        keeping system trees such as /etc, /proc, and /var out of the picker.
        """
        path = self._workspace_browser_path(value)
        with self._workspace_lock:
            registered = set(self._workspaces)

        try:
            candidates = sorted(path.iterdir(), key=lambda item: item.name.casefold())
        except OSError as exc:
            raise ValueError("directory cannot be read") from exc

        directories: list[dict[str, Any]] = []
        for candidate in candidates:
            if candidate.name.startswith("."):
                continue
            try:
                if not candidate.is_dir():
                    continue
                clean = candidate.resolve()
            except (OSError, RuntimeError):
                continue
            if not self._workspace_browser_allowed(clean):
                continue
            if not os.access(clean, os.R_OK | os.X_OK):
                continue
            directories.append({
                "path": str(clean),
                "name": candidate.name,
                "registered": str(clean) in registered,
                "selectable": self._workspace_selectable(clean),
                "project": self._workspace_project_hint(clean),
            })

        # Project-looking folders rise to the top, like an editor's recent /
        # workspace suggestions, while ordinary directories remain available.
        directories.sort(key=lambda item: (
            not bool(item["project"]), str(item["name"]).casefold()))
        truncated = len(directories) > _WORKSPACE_BROWSER_LIMIT
        directories = directories[:_WORKSPACE_BROWSER_LIMIT]
        parent = path.parent
        if parent == path or not self._workspace_browser_allowed(parent):
            parent_value = ""
        else:
            parent_value = str(parent)
        return {
            "path": str(path),
            "name": path.name or str(path),
            "parent": parent_value,
            "registered": str(path) in registered,
            "selectable": self._workspace_selectable(path),
            "directories": directories,
            "truncated": truncated,
        }

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
        if path in _WORKSPACE_FORBIDDEN:
            raise ValueError("workspace path is too broad or sensitive")
        if not path.exists() or not path.is_dir():
            raise ValueError("workspace path must be an existing directory")
        return path

    def _workspace_browser_roots(self) -> tuple[Path, ...]:
        home = Path.home().resolve()
        with self._workspace_lock:
            workspaces = tuple(Path(path) for path in self._workspaces)
        roots: list[Path] = []
        for workspace in workspaces:
            if workspace == home or workspace.is_relative_to(home):
                root = home
            else:
                parent = workspace.parent
                parent_is_sensitive = any(
                    parent == denied or parent.is_relative_to(denied)
                    for denied in _WORKSPACE_BROWSER_DENIED_ROOTS
                )
                root = workspace if (
                    parent in _WORKSPACE_FORBIDDEN or parent_is_sensitive
                ) else parent
            if root not in roots:
                roots.append(root)
        return tuple(roots)

    def _workspace_browser_allowed(self, path: Path) -> bool:
        try:
            clean = path.resolve()
        except (OSError, RuntimeError):
            return False
        roots = self._workspace_browser_roots()
        matches = [
            root for root in roots
            if clean == root or clean.is_relative_to(root)
        ]
        if not matches:
            return False
        sensitive = next((
            denied for denied in _WORKSPACE_BROWSER_DENIED_ROOTS
            if clean == denied or clean.is_relative_to(denied)
        ), None)
        if sensitive is None:
            return True
        # A workspace already registered inside a normally-sensitive tree
        # (for example /var/lib/my-project) remains browsable beneath that
        # exact root, but the picker can never walk upward into /var itself.
        home = Path.home().resolve()
        return any(root == home
                   or (root != sensitive and root.is_relative_to(sensitive))
                   for root in matches)

    def _workspace_browser_path(self, value: str | Path | None) -> Path:
        raw = str(value or "").strip()
        try:
            path = Path(raw).expanduser().resolve() if raw else self.workspace
        except (OSError, RuntimeError) as exc:
            raise ValueError("invalid directory path") from exc
        if not self._workspace_browser_allowed(path):
            raise ValueError("directory is outside selectable workspace roots")
        if not path.exists() or not path.is_dir():
            raise ValueError("directory must exist")
        if not os.access(path, os.R_OK | os.X_OK):
            raise ValueError("directory cannot be read")
        return path

    def _workspace_selectable(self, path: Path) -> bool:
        try:
            self._validated_workspace(path)
        except ValueError:
            return False
        return os.access(path, os.R_OK | os.X_OK)

    @staticmethod
    def _workspace_project_hint(path: Path) -> str:
        labels: list[str] = []
        for marker, label in _WORKSPACE_PROJECT_MARKERS:
            try:
                present = (path / marker).exists()
            except OSError:
                present = False
            if present:
                labels.append(label)
            if len(labels) >= 2:
                break
        return " · ".join(labels)

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

    def remember_service_tier(self, thread_id: str, service_tier: str) -> None:
        """Persist an override already accepted by start/resume/fork."""
        clean_id = _thread_id(thread_id)
        clean_tier = normalize_service_tier(service_tier)
        assert clean_tier is not None
        with self._metadata_lock:
            current = self._settings.get(clean_id, {})
            if ("service_tier" in current
                    and current.get("service_tier") == clean_tier):
                return
            updated = {
                key: dict(value) for key, value in self._settings.items()
            }
            updated.setdefault(clean_id, {})["service_tier"] = clean_tier
            self._write_metadata(self._pinned, updated)
            self._settings = updated
        self.invalidate_list_cache()

    async def set_service_tier(self, thread_id: str, service_tier: str) -> None:
        """Apply and persist the session's Standard/Fast service tier.

        Stable ``thread/list`` and ``thread/read`` responses currently omit the
        effective tier.  Keep the explicit choice in the same compatibility
        sidecar as Effort, and also send the native settings update.  Empty
        pre-first-turn threads may reject ``thread/settings/update`` with
        ``-32600``; ``turn/start.serviceTier`` applies the sidecar choice when
        that thread is materialized.
        """
        self._sync_runtime_generation()
        clean_id = _thread_id(thread_id)
        clean_tier = normalize_service_tier(service_tier)
        assert clean_tier is not None
        try:
            result = await self.requester.request("thread/settings/update", {
                "threadId": clean_id,
                "serviceTier": clean_tier or None,
            })
            if not isinstance(result, dict):
                raise AppServerProtocolError(
                    "thread/settings/update returned an invalid result")
        except AppServerResponseError as exc:
            self._sync_runtime_generation()
            if exc.code != -32600 or clean_id not in self._pending:
                raise
        self.remember_service_tier(clean_id, clean_tier)

    def service_tier(self, thread_id: str) -> str | None:
        """Return an explicit service-tier override, including Standard ``""``."""
        clean_id = _thread_id(thread_id)
        with self._metadata_lock:
            settings = self._settings.get(clean_id, {})
            if "service_tier" not in settings:
                return None
            value = settings.get("service_tier")
        return value if isinstance(value, str) else None

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

    async def _read_request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> Any:
        read_request = getattr(self.requester, "read_request", None)
        request = read_request if callable(read_request) else self.requester.request
        return await request(method, params, timeout=timeout)

    def _load_pending(self) -> tuple[dict[str, dict[str, Any]], set[str]]:
        try:
            payload = json.loads(self._pending_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}, set()
        if not isinstance(payload, dict) or payload.get("version") != 1:
            return {}, set()
        pending: dict[str, dict[str, Any]] = {}
        raw_pending = payload.get("pending")
        if isinstance(raw_pending, list):
            for raw in raw_pending:
                if not isinstance(raw, dict):
                    continue
                thread_id = raw.get("id")
                if isinstance(thread_id, str) and thread_id.strip():
                    pending[thread_id.strip()] = _pending_snapshot(raw)
        raw_materialized = payload.get("materialized")
        materialized = {
            value.strip() for value in raw_materialized or []
            if isinstance(value, str) and value.strip()
        } if isinstance(raw_materialized, list) else set()
        materialized.difference_update(pending)
        return pending, materialized

    def _write_pending(self) -> None:
        if not self._pending and not self._materialized:
            self._pending_path.unlink(missing_ok=True)
            return
        atomic_write_text(
            self._pending_path,
            json.dumps({
                "version": 1,
                "pending": list(self._pending.values()),
                "materialized": sorted(self._materialized),
            }, ensure_ascii=False, separators=(",", ":")),
        )

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
            try:
                service_tier = normalize_service_tier(raw.get("service_tier"))
            except (AttributeError, ValueError):
                service_tier = None
            if service_tier is not None:
                item["service_tier"] = service_tier
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
        if isinstance(thread_id, str):
            decorated["materialized"] = thread_id not in self._pending
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
        health = health_fn()
        generation = getattr(health, "generation", health.restart_count)
        previous = getattr(self, "_runtime_generation", generation)
        if generation != previous:
            self.invalidate_list_cache()
        self._runtime_generation = generation


def _thread_id(value: str) -> str:
    clean = value.strip()
    if not clean:
        raise ValueError("thread id cannot be empty")
    return clean


def _pending_snapshot(thread: dict[str, Any]) -> dict[str, Any]:
    """Persist only empty-thread metadata, never transcript/prompt payloads."""
    thread_id = _thread_id(str(thread.get("id") or ""))
    snapshot: dict[str, Any] = {"id": thread_id, "turns": []}
    for key in (
        "name", "preview", "model", "modelProvider", "cwd", "status",
        "createdAt", "updatedAt", "parentThreadId",
    ):
        value = thread.get(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            snapshot[key] = value
    return snapshot


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
