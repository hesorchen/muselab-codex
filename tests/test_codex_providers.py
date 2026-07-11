"""Contract tests for the curated Codex-native provider registry."""

import pytest

from backend.codex.providers import CodexProviderService, model_for_provider, provider_for_model


class Requester:
    def __init__(self):
        self.providers = {}
        self.calls = []

    async def request(self, method, params=None, *, timeout=None):
        self.calls.append((method, params, timeout))
        if method == "config/read":
            return {"config": {"model_providers": self.providers}}
        if method == "config/value/write":
            provider_id = params["keyPath"].split(".")[1]
            if params["value"] is None:
                self.providers.pop(provider_id, None)
            else:
                self.providers[provider_id] = params["value"]
            return {"status": "ok"}
        raise AssertionError(method)


@pytest.mark.asyncio
async def test_native_provider_enablement_is_written_to_codex_config(tmp_path):
    requester = Requester()
    service = CodexProviderService(requester, tmp_path)

    listed = await service.list()
    assert [item["id"] for item in listed["providers"]] == ["minimax", "qwen", "mimo"]
    assert not any(item["enabled"] for item in listed["providers"])

    updated = await service.set_enabled("mimo", True)
    assert updated["providers"][2]["enabled"] is True
    assert requester.providers["mimo"] == {
        "name": "Xiaomi MiMo",
        "base_url": "https://api.xiaomimimo.com/v1",
        "env_key": "XIAOMI_MIMO_API_KEY",
        "wire_api": "responses",
    }
    assert service.thread_config("mimo") == {"web_search": "disabled"}
    assert service.model_entries({"mimo"})[0]["model"] == "mimo-v2.5-pro"


def test_model_registry_roundtrip():
    assert provider_for_model("qwen3.7-plus").id == "qwen"
    assert provider_for_model("not-registered") is None
    assert model_for_provider("minimax") == "minimax-m2.7"
    assert model_for_provider("unknown") == ""
