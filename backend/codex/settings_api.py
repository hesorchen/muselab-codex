"""Codex-native runtime diagnostics for the settings screen."""

from __future__ import annotations

import asyncio
import subprocess
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..auth import require_token
from ..settings import CODEX_BIN
from .process import AppServerError


router = APIRouter(prefix="/api/settings", tags=["settings"])


class ProviderEnabledIn(BaseModel):
    enabled: bool


@router.get("", dependencies=[Depends(require_token)])
async def get_settings(request: Request) -> dict[str, Any]:
    """Return the native model surface without credential or routing controls."""
    try:
        result = await request.app.state.codex_runtime.request("model/list", {
            "limit": 100,
            "includeHidden": False,
        })
    except AppServerError:
        result = {}
    models = result.get("data") if isinstance(result, dict) else []
    default = next((item.get("model", "") for item in models
                    if isinstance(item, dict) and item.get("isDefault")), "")
    return {
        "runtime": "codex",
        "providers": [],
        "defaults": {"model": default, "permission": "default"},
        "params": {},
    }


@router.get("/providers", dependencies=[Depends(require_token)])
async def list_providers(request: Request) -> dict[str, Any]:
    try:
        return await request.app.state.codex_providers.list()
    except (AppServerError, ValueError) as exc:
        raise HTTPException(502, str(exc)) from exc


@router.patch("/providers/{provider_id}", dependencies=[Depends(require_token)])
async def configure_provider(
    request: Request, provider_id: str, body: ProviderEnabledIn,
) -> dict[str, Any]:
    try:
        return await request.app.state.codex_providers.set_enabled(provider_id, body.enabled)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except AppServerError as exc:
        raise HTTPException(502, str(exc)) from exc


@router.get("/versions", dependencies=[Depends(require_token)])
async def get_versions(request: Request) -> dict[str, Any]:
    """Expose the installed app-server CLI version; upgrades stay operator-owned."""
    version = await asyncio.to_thread(_codex_version)
    server = getattr(request.app.state.codex_runtime, "server", None)
    remote_url = getattr(server, "remote_url", "")
    return {
        "runtime": "codex",
        "current": {"codex_cli": version},
        "latest": {},
        "upgrade_available": False,
        "shared_runtime": bool(remote_url),
        "cli_remote_url": remote_url,
        "cli_resume_command": f"codex resume --remote {remote_url}" if remote_url else "",
    }


def _codex_version() -> str:
    try:
        completed = subprocess.run(
            [CODEX_BIN, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""
