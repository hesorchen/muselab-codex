"""Verified OpenAI Responses providers for Codex-native threads."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .process import AppServerProtocolError
from .threads import Requester


@dataclass(frozen=True)
class NativeProvider:
    id: str
    name: str
    base_url: str
    env_key: str
    models: tuple[tuple[str, str], ...]
    disable_web_search: bool = True

    def config(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "base_url": self.base_url,
            "env_key": self.env_key,
            "wire_api": "responses",
        }


PROVIDERS: tuple[NativeProvider, ...] = (
    NativeProvider(
        "minimax", "MiniMax", "https://api.minimaxi.com/v1", "MINIMAX_API_KEY",
        (("minimax-m2.7", "MiniMax M2.7"),),
    ),
    NativeProvider(
        "qwen", "Qwen", "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "DASHSCOPE_API_KEY", (("qwen3.7-plus", "Qwen 3.7 Plus"),),
    ),
    NativeProvider(
        "mimo", "Xiaomi MiMo", "https://api.xiaomimimo.com/v1",
        "XIAOMI_MIMO_API_KEY", (("mimo-v2.5-pro", "MiMo V2.5 Pro"),),
    ),
)


class CodexProviderService:
    """Persist only verified provider definitions through app-server config."""

    def __init__(self, requester: Requester, workspace: Path):
        self.requester = requester
        self.workspace = Path(workspace).resolve()

    async def list(self) -> dict[str, Any]:
        config = await self._config()
        configured = config.get("model_providers", {})
        configured = configured if isinstance(configured, dict) else {}
        return {"providers": [self._public(provider, provider.id in configured)
                              for provider in PROVIDERS]}

    async def set_enabled(self, provider_id: str, enabled: bool) -> dict[str, Any]:
        provider = _provider(provider_id)
        value = provider.config() if enabled else None
        result = await self.requester.request("config/value/write", {
            "keyPath": f"model_providers.{provider.id}",
            "value": value,
            "mergeStrategy": "replace",
        })
        if not isinstance(result, dict) or result.get("status") != "ok":
            raise AppServerProtocolError("config/value/write returned an invalid result")
        return await self.list()

    def model_entries(self, enabled_ids: set[str]) -> list[dict[str, Any]]:
        entries = []
        for provider in PROVIDERS:
            if provider.id not in enabled_ids:
                continue
            entries.extend({
                "group": provider.name,
                "label": label,
                "model": model,
                "provider": provider.id,
                "supports_thinking": True,
                "supports_effort": False,
            } for model, label in provider.models)
        return entries

    def thread_config(self, provider_id: str) -> dict[str, Any]:
        provider = _provider(provider_id)
        return {"web_search": "disabled"} if provider.disable_web_search else {}

    async def _config(self) -> dict[str, Any]:
        result = await self.requester.request("config/read", {
            "cwd": str(self.workspace),
            "includeLayers": True,
        })
        if not isinstance(result, dict) or not isinstance(result.get("config"), dict):
            raise AppServerProtocolError("config/read returned an invalid result")
        return result["config"]

    @staticmethod
    def _public(provider: NativeProvider, enabled: bool) -> dict[str, Any]:
        return {
            "id": provider.id, "name": provider.name, "enabled": enabled,
            "env_key": provider.env_key,
            "models": [{"id": model, "label": label} for model, label in provider.models],
            "web_search": not provider.disable_web_search,
        }


def _provider(provider_id: str) -> NativeProvider:
    clean = provider_id.strip()
    for provider in PROVIDERS:
        if provider.id == clean:
            return provider
    raise ValueError("unknown Codex-native provider")


def provider_for_model(model: str) -> NativeProvider | None:
    """Return the registered provider for an exact model id, if any."""
    clean = model.strip()
    for provider in PROVIDERS:
        if any(candidate == clean for candidate, _label in provider.models):
            return provider
    return None


def model_for_provider(provider_id: str) -> str:
    """Return the sole curated model for a provider, or an empty value."""
    try:
        provider = _provider(provider_id)
    except ValueError:
        return ""
    return provider.models[0][0] if len(provider.models) == 1 else ""
