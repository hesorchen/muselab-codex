"""Codex-native Skills service tests."""

import pytest

from backend.codex import AppServerProtocolError, CodexSkillsService


class Requester:
    def __init__(self):
        self.calls = []
        self.enabled = True

    async def request(self, method, params=None, *, timeout=None):
        self.calls.append((method, params, timeout))
        if method == "skills/list":
            cwd = params["cwds"][0]
            return {"data": [{
                "cwd": cwd,
                "errors": [{"path": "/bad/SKILL.md", "message": "bad metadata"}],
                "skills": [{
                    "name": "example",
                    "description": "Long description",
                    "shortDescription": None,
                    "path": f"{cwd}/.codex/skills/example/SKILL.md",
                    "scope": "repo",
                    "enabled": self.enabled,
                    "interface": {
                        "displayName": "Example Skill",
                        "shortDescription": "Short description",
                        "defaultPrompt": "Use example to inspect this.",
                    },
                }],
            }]}
        if method == "skills/config/write":
            self.enabled = params["enabled"]
            return {"effectiveEnabled": self.enabled}
        raise AssertionError(method)


@pytest.mark.asyncio
async def test_list_maps_native_metadata_and_errors(tmp_path):
    requester = Requester()
    service = CodexSkillsService(requester, tmp_path)

    result = await service.list(force_reload=True)

    assert result["skills"] == [{
        "name": "example",
        "display_name": "Example Skill",
        "description": "Short description",
        "scope": "repo",
        "source": "repo",
        "path": f"{tmp_path}/.codex/skills/example/SKILL.md",
        "enabled": True,
        "disabled": False,
        "default_prompt": "Use example to inspect this.",
    }]
    assert result["errors"] == [{"path": "/bad/SKILL.md", "message": "bad metadata"}]
    assert requester.calls[0] == ("skills/list", {
        "cwds": [str(tmp_path.resolve())],
        "forceReload": True,
    }, None)


@pytest.mark.asyncio
async def test_set_enabled_only_accepts_known_native_path(tmp_path):
    requester = Requester()
    service = CodexSkillsService(requester, tmp_path)
    path = f"{tmp_path}/.codex/skills/example/SKILL.md"

    result = await service.set_enabled(path, False)

    assert result["skill"]["enabled"] is False
    assert result["skill"]["disabled"] is True
    assert requester.calls[1] == ("skills/config/write", {
        "path": path,
        "enabled": False,
    }, None)

    with pytest.raises(ValueError, match="not present"):
        await service.set_enabled("/unknown/SKILL.md", True)


@pytest.mark.asyncio
async def test_list_rejects_invalid_protocol_shape(tmp_path):
    class InvalidRequester:
        async def request(self, method, params=None, *, timeout=None):
            return {"data": [{"cwd": str(tmp_path), "skills": [], "errors": "bad"}]}

    with pytest.raises(AppServerProtocolError, match="invalid entry"):
        await CodexSkillsService(InvalidRequester(), tmp_path).list()
