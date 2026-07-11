"""Codex-native MCP inventory and config tests."""

from contextlib import asynccontextmanager

import pytest

from backend.codex import (
    AppServerProtocolError,
    AppServerTimeoutError,
    CodexMcpService,
)


class Requester:
    def __init__(self):
        self.calls = []
        self.servers = {
            "local": {
                "command": "npx",
                "args": ["-y", "server"],
                "env": {"SECRET": "not-returned"},
                "enabled": True,
            },
            "remote": {
                "url": "https://mcp.example.test/mcp",
                "http_headers": {"Authorization": "not-returned"},
                "enabled": False,
            },
        }

    async def request(self, method, params=None, *, timeout=None):
        self.calls.append((method, params, timeout))
        if method == "config/read":
            return {
                "config": {"mcp_servers": self.servers},
                "origins": {},
                "layers": [{
                    "name": {"type": "user", "file": "/tmp/config.toml"},
                    "version": "one",
                    "config": {"mcp_servers": self.servers},
                }],
            }
        if method == "mcpServerStatus/list":
            return {
                "data": [{
                    "name": "local",
                    "authStatus": "unsupported",
                    "tools": {
                        "read": {
                            "name": "read",
                            "title": "Read",
                            "description": "Read a fixture",
                            "inputSchema": {},
                        },
                    },
                    "resources": [{"name": "one", "uri": "fixture://one"}],
                    "resourceTemplates": [],
                    "serverInfo": {"name": "local", "version": "1.0.0"},
                }],
                "nextCursor": None,
            }
        if method == "config/value/write":
            parts = params["keyPath"].split(".")
            if len(parts) == 2:
                if params["value"] is None:
                    self.servers.pop(parts[1], None)
                else:
                    self.servers[parts[1]] = params["value"]
            else:
                self.servers[parts[1]][parts[2]] = params["value"]
            return {"status": "ok", "version": "two", "filePath": "/tmp/config.toml"}
        if method == "config/mcpServer/reload":
            return {}
        if method == "mcpServer/oauth/login":
            return {"authorizationUrl": "https://auth.example.test/start"}
        raise AssertionError(method)


@pytest.mark.asyncio
async def test_list_merges_safe_config_and_native_inventory(tmp_path):
    result = await CodexMcpService(Requester(), tmp_path).list()

    assert [server["name"] for server in result["servers"]] == ["local", "remote"]
    local = result["servers"][0]
    assert local["tool_count"] == 1
    assert local["resource_count"] == 1
    assert local["editable"] is True
    assert local["has_environment"] is True
    assert "env" not in local
    remote = result["servers"][1]
    assert remote["transport"] == "http"
    assert remote["disabled"] is True
    assert remote["has_http_headers"] is True
    assert "http_headers" not in remote


@pytest.mark.asyncio
async def test_mutations_use_config_write_and_reload(tmp_path):
    requester = Requester()
    service = CodexMcpService(requester, tmp_path)

    await service.set_enabled("local", False)
    assert requester.servers["local"]["enabled"] is False
    await service.delete("local")
    assert "local" not in requester.servers
    await service.add(
        "new-server",
        transport="http",
        url="https://new.example.test/mcp",
        bearer_token_env_var="NEW_MCP_TOKEN",
    )
    assert requester.servers["new-server"] == {
        "url": "https://new.example.test/mcp",
        "enabled": True,
        "bearer_token_env_var": "NEW_MCP_TOKEN",
    }
    write_calls = [call for call in requester.calls if call[0] == "config/value/write"]
    assert write_calls[0][1] == {
        "keyPath": "mcp_servers.local.enabled",
        "value": False,
        "mergeStrategy": "replace",
    }


@pytest.mark.asyncio
async def test_non_user_server_is_read_only(tmp_path):
    requester = Requester()
    original = requester.request

    async def project_request(method, params=None, *, timeout=None):
        result = await original(method, params, timeout=timeout)
        if method == "config/read":
            result["layers"][0]["name"] = {
                "type": "project", "dotCodexFolder": str(tmp_path / ".codex"),
            }
        return result

    requester.request = project_request
    service = CodexMcpService(requester, tmp_path)

    result = await service.list()
    assert result["servers"][0]["scope"] == "project"
    assert result["servers"][0]["editable"] is False
    with pytest.raises(ValueError, match="not owned"):
        await service.delete("local")


@pytest.mark.asyncio
async def test_validation_and_oauth(tmp_path):
    requester = Requester()
    service = CodexMcpService(requester, tmp_path)

    with pytest.raises(ValueError, match="letters"):
        await service.add("bad.name", transport="stdio", command="npx")
    with pytest.raises(ValueError, match="credentials"):
        await service.add(
            "bad-url", transport="http", url="https://user:secret@example.test/mcp")
    with pytest.raises(ValueError, match="environment variable"):
        await service.add(
            "bad-env", transport="http", url="https://example.test/mcp",
            bearer_token_env_var="BAD-NAME")
    with pytest.raises(ValueError, match="does not support OAuth"):
        await service.oauth_login("local")

    requester.servers["remote"]["enabled"] = True
    original = requester.request

    async def oauth_request(method, params=None, *, timeout=None):
        if method == "mcpServerStatus/list":
            return {
                "data": [{
                    "name": "remote",
                    "authStatus": "notLoggedIn",
                    "tools": {},
                    "resources": [],
                    "resourceTemplates": [],
                }],
                "nextCursor": None,
            }
        return await original(method, params, timeout=timeout)

    requester.request = oauth_request
    assert await service.oauth_login("remote") == {
        "authorization_url": "https://auth.example.test/start",
    }


@pytest.mark.asyncio
async def test_invalid_status_shape_degrades_inventory(tmp_path):
    class InvalidRequester(Requester):
        async def request(self, method, params=None, *, timeout=None):
            if method == "mcpServerStatus/list":
                return {"data": "bad"}
            return await super().request(method, params, timeout=timeout)

    result = await CodexMcpService(InvalidRequester(), tmp_path).list()
    assert result["inventory_error"] == "unavailable"
    assert [server["name"] for server in result["servers"]] == ["local", "remote"]


@pytest.mark.asyncio
async def test_rejects_unsafe_oauth_url(tmp_path):
    requester = Requester()
    requester.servers["remote"]["enabled"] = True
    original = requester.request

    async def unsafe_request(method, params=None, *, timeout=None):
        if method == "mcpServerStatus/list":
            return {
                "data": [{
                    "name": "remote",
                    "authStatus": "notLoggedIn",
                    "tools": {},
                    "resources": [],
                    "resourceTemplates": [],
                }],
                "nextCursor": None,
            }
        if method == "mcpServer/oauth/login":
            return {"authorizationUrl": "javascript:alert(1)"}
        return await original(method, params, timeout=timeout)

    requester.request = unsafe_request
    with pytest.raises(AppServerProtocolError, match="unsafe authorization URL"):
        await CodexMcpService(requester, tmp_path).oauth_login("remote")


@pytest.mark.asyncio
async def test_isolated_inventory_timeout_degrades_without_using_main_requester(tmp_path):
    main = Requester()

    class TimeoutRequester:
        async def request(self, method, params=None, *, timeout=None):
            assert method == "mcpServerStatus/list"
            assert timeout == 15.0
            raise AppServerTimeoutError("fixture timeout")

    @asynccontextmanager
    async def isolated():
        yield TimeoutRequester()

    result = await CodexMcpService(
        main,
        tmp_path,
        status_requester_factory=isolated,
    ).list()

    assert result["inventory_error"] == "unavailable"
    assert [server["name"] for server in result["servers"]] == ["local", "remote"]
    assert not any(call[0] == "mcpServerStatus/list" for call in main.calls)
