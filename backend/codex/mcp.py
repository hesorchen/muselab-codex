"""Codex-native MCP inventory and user-config management."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .process import AppServerError, AppServerProtocolError
from .threads import Requester


_SERVER_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MAX_STATUS_PAGES = 100
_STATUS_TIMEOUT = 15.0
_STATUS_CACHE_TTL = 30.0

StatusRequesterFactory = Callable[[], AbstractAsyncContextManager[Requester]]


class CodexMcpService:
    """Use app-server as the sole MCP config and inventory authority."""

    def __init__(
        self,
        requester: Requester,
        workspace: Path,
        *,
        status_requester_factory: StatusRequesterFactory | None = None,
    ):
        self.requester = requester
        self.workspace = Path(workspace).resolve()
        self.status_requester_factory = status_requester_factory
        self._write_lock = asyncio.Lock()
        self._status_lock = asyncio.Lock()
        self._status_cache: tuple[float, list[dict[str, Any]], str | None] | None = None

    async def list(self, *, reload: bool = False) -> dict[str, Any]:
        if reload:
            await self.reload()
        config_result = await self._read_config()
        statuses, inventory_error = await self._status_snapshot()
        result = _merge_inventory(config_result, statuses)
        result["inventory_error"] = inventory_error
        return result

    async def add(
        self,
        name: str,
        *,
        transport: str,
        command: str = "",
        args: list[str] | None = None,
        url: str = "",
        bearer_token_env_var: str = "",
        enabled: bool = True,
    ) -> dict[str, Any]:
        clean_name = _server_name(name)
        spec = _server_spec(
            transport=transport,
            command=command,
            args=args or [],
            url=url,
            bearer_token_env_var=bearer_token_env_var,
            enabled=enabled,
        )
        async with self._write_lock:
            current = _merge_inventory(await self._read_config(), [])
            existing = next(
                (server for server in current["servers"] if server["name"] == clean_name),
                None,
            )
            if existing is not None:
                raise ValueError("MCP server name is already configured")
            await self._write(f"mcp_servers.{clean_name}", spec)
            await self.reload()
        return await self.list()

    async def set_enabled(self, name: str, enabled: bool) -> dict[str, Any]:
        clean_name = _server_name(name)
        async with self._write_lock:
            await self._require_user_server(clean_name)
            await self._write(f"mcp_servers.{clean_name}.enabled", enabled)
            await self.reload()
        return await self.list()

    async def delete(self, name: str) -> dict[str, Any]:
        clean_name = _server_name(name)
        async with self._write_lock:
            await self._require_user_server(clean_name)
            await self._write(f"mcp_servers.{clean_name}", None)
            await self.reload()
        return await self.list()

    async def oauth_login(self, name: str) -> dict[str, str]:
        clean_name = _server_name(name)
        inventory = await self.list()
        server = next(
            (item for item in inventory["servers"] if item["name"] == clean_name),
            None,
        )
        if server is None:
            raise ValueError("MCP server is not configured")
        if server["disabled"]:
            raise ValueError("MCP server is disabled")
        if server["auth_status"] == "unsupported":
            raise ValueError("MCP server does not support OAuth")
        result = await self.requester.request("mcpServer/oauth/login", {
            "name": clean_name,
        })
        if not isinstance(result, dict) or not isinstance(
            result.get("authorizationUrl"), str
        ) or not result["authorizationUrl"]:
            raise AppServerProtocolError(
                "mcpServer/oauth/login returned an invalid result")
        authorization_url = result["authorizationUrl"]
        parsed = urlsplit(authorization_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise AppServerProtocolError(
                "mcpServer/oauth/login returned an unsafe authorization URL")
        return {"authorization_url": authorization_url}

    async def reload(self) -> None:
        result = await self.requester.request("config/mcpServer/reload")
        if not isinstance(result, dict):
            raise AppServerProtocolError(
                "config/mcpServer/reload returned an invalid result")
        self._status_cache = None

    async def _read_config(self) -> dict[str, Any]:
        result = await self.requester.request("config/read", {
            "cwd": str(self.workspace),
            "includeLayers": True,
        })
        if not isinstance(result, dict) or not isinstance(result.get("config"), dict):
            raise AppServerProtocolError("config/read returned an invalid result")
        layers = result.get("layers")
        if layers is not None and not isinstance(layers, list):
            raise AppServerProtocolError("config/read returned invalid layers")
        return result

    async def _status_snapshot(self) -> tuple[list[dict[str, Any]], str | None]:
        now = time.monotonic()
        cached = self._status_cache
        if cached is not None and cached[0] > now:
            return cached[1], cached[2]
        async with self._status_lock:
            now = time.monotonic()
            cached = self._status_cache
            if cached is not None and cached[0] > now:
                return cached[1], cached[2]
            try:
                if self.status_requester_factory is None:
                    statuses = await self._list_statuses(self.requester)
                else:
                    async with self.status_requester_factory() as requester:
                        statuses = await self._list_statuses(requester)
                error = None
            except AppServerError:
                statuses = []
                error = "unavailable"
            self._status_cache = (
                time.monotonic() + _STATUS_CACHE_TTL,
                statuses,
                error,
            )
            return statuses, error

    async def _list_statuses(self, requester: Requester) -> list[dict[str, Any]]:
        statuses: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _page in range(_MAX_STATUS_PAGES):
            params: dict[str, Any] = {"detail": "full", "limit": 100}
            if cursor is not None:
                params["cursor"] = cursor
            result = await requester.request(
                "mcpServerStatus/list", params, timeout=_STATUS_TIMEOUT)
            if not isinstance(result, dict) or not isinstance(result.get("data"), list):
                raise AppServerProtocolError(
                    "mcpServerStatus/list returned an invalid result")
            for status in result["data"]:
                if not isinstance(status, dict) or not isinstance(status.get("name"), str):
                    raise AppServerProtocolError(
                        "mcpServerStatus/list returned an invalid server")
                statuses.append(status)
            next_cursor = result.get("nextCursor")
            if next_cursor is None:
                return statuses
            if not isinstance(next_cursor, str) or not next_cursor:
                raise AppServerProtocolError(
                    "mcpServerStatus/list returned an invalid cursor")
            if next_cursor in seen_cursors:
                raise AppServerProtocolError(
                    "mcpServerStatus/list repeated a pagination cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise AppServerProtocolError("mcpServerStatus/list exceeded pagination limit")

    async def _require_user_server(self, name: str) -> None:
        inventory = _merge_inventory(await self._read_config(), [])
        server = next(
            (item for item in inventory["servers"] if item["name"] == name),
            None,
        )
        if server is None:
            raise ValueError("MCP server is not configured")
        if not server["editable"]:
            raise ValueError("MCP server is not owned by the user Codex config")

    async def _write(self, key_path: str, value: Any) -> None:
        result = await self.requester.request("config/value/write", {
            "keyPath": key_path,
            "value": value,
            "mergeStrategy": "replace",
        })
        if not isinstance(result, dict) or result.get("status") != "ok":
            raise AppServerProtocolError(
                "config/value/write returned an invalid result")


def _merge_inventory(
    config_result: dict[str, Any],
    statuses: list[dict[str, Any]],
) -> dict[str, Any]:
    config = config_result.get("config") or {}
    raw_servers = config.get("mcp_servers") or {}
    if not isinstance(raw_servers, dict):
        raise AppServerProtocolError("config/read returned invalid MCP config")
    ownership = _server_ownership(config_result.get("layers") or [])
    status_by_name = {status["name"]: status for status in statuses}
    names = sorted(set(raw_servers) | set(status_by_name), key=str.casefold)
    servers = []
    for name in names:
        raw = raw_servers.get(name)
        raw = raw if isinstance(raw, dict) else {}
        status = status_by_name.get(name) or {}
        scope = ownership.get(name, "runtime" if name not in raw_servers else "codex")
        enabled = raw.get("enabled", True) is not False
        tools = _tools(status.get("tools"))
        auth_status = status.get("authStatus")
        if auth_status not in {"unsupported", "notLoggedIn", "bearerToken", "oAuth"}:
            auth_status = "unknown"
        server_info = status.get("serverInfo")
        server_info = _server_info(server_info)
        servers.append({
            "name": name,
            "transport": "http" if isinstance(raw.get("url"), str) else "stdio",
            "type": "http" if isinstance(raw.get("url"), str) else "stdio",
            "command": raw.get("command") if isinstance(raw.get("command"), str) else "",
            "args": [arg for arg in raw.get("args", []) if isinstance(arg, str)]
                    if isinstance(raw.get("args"), list) else [],
            "url": raw.get("url") if isinstance(raw.get("url"), str) else "",
            "bearer_token_env_var": raw.get("bearer_token_env_var")
                    if isinstance(raw.get("bearer_token_env_var"), str) else "",
            "has_environment": bool(raw.get("env")),
            "has_http_headers": bool(raw.get("http_headers")),
            "enabled": enabled,
            "disabled": not enabled,
            "auth_status": auth_status,
            "tools": tools,
            "tool_count": len(tools),
            "resource_count": _list_length(status.get("resources")),
            "resource_template_count": _list_length(status.get("resourceTemplates")),
            "server_info": server_info,
            "scope": scope,
            "source": scope,
            "editable": scope == "user",
        })
    return {"configured": bool(servers), "servers": servers}


def _server_ownership(layers: list[Any]) -> dict[str, str]:
    ownership: dict[str, str] = {}
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        layer_config = layer.get("config")
        layer_servers = layer_config.get("mcp_servers") if isinstance(layer_config, dict) else None
        source = layer.get("name")
        scope = source.get("type") if isinstance(source, dict) else None
        if not isinstance(layer_servers, dict) or not isinstance(scope, str):
            continue
        for name in layer_servers:
            if isinstance(name, str):
                ownership[name] = scope
    return ownership


def _tools(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, dict):
        return []
    tools = []
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        name = value.get("name") if isinstance(value.get("name"), str) else key
        tools.append({
            "name": name,
            "title": value.get("title") if isinstance(value.get("title"), str) else "",
            "description": value.get("description")
                    if isinstance(value.get("description"), str) else "",
        })
    tools.sort(key=lambda tool: tool["name"].casefold())
    return tools


def _server_info(raw: Any) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    return {
        key: raw[key]
        for key in ("name", "title", "version", "description", "websiteUrl")
        if isinstance(raw.get(key), str)
    }


def _server_name(name: str) -> str:
    clean_name = name.strip()
    if not _SERVER_NAME.fullmatch(clean_name):
        raise ValueError(
            "MCP server name must use 1-64 letters, numbers, underscores, or hyphens")
    return clean_name


def _server_spec(
    *,
    transport: str,
    command: str,
    args: list[str],
    url: str,
    bearer_token_env_var: str,
    enabled: bool,
) -> dict[str, Any]:
    if transport == "stdio":
        clean_command = command.strip()
        if not clean_command:
            raise ValueError("STDIO MCP command cannot be empty")
        if len(clean_command) > 4096 or len(args) > 256:
            raise ValueError("STDIO MCP command is too large")
        if any(not isinstance(arg, str) or len(arg) > 4096 for arg in args):
            raise ValueError("STDIO MCP arguments must be strings up to 4096 characters")
        return {"command": clean_command, "args": args, "enabled": enabled}
    if transport != "http":
        raise ValueError("MCP transport must be stdio or http")
    clean_url = url.strip()
    parsed = urlsplit(clean_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("HTTP MCP URL must use http or https")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("HTTP MCP URL must not contain credentials")
    spec: dict[str, Any] = {"url": clean_url, "enabled": enabled}
    clean_env = bearer_token_env_var.strip()
    if clean_env:
        if not _ENV_NAME.fullmatch(clean_env):
            raise ValueError("Bearer token environment variable name is invalid")
        spec["bearer_token_env_var"] = clean_env
    return spec


def _list_length(raw: Any) -> int:
    return len(raw) if isinstance(raw, list) else 0
