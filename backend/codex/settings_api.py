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
    runtime = request.app.state.codex_runtime
    read_request = getattr(runtime, "read_request", None)
    read = read_request if callable(read_request) else runtime.request
    try:
        result = await read("model/list", {
            "limit": 100,
            "includeHidden": False,
        })
    except AppServerError:
        result = {}
    models = result.get("data") if isinstance(result, dict) else []
    default = next((item.get("model", "") for item in models
                    if isinstance(item, dict) and item.get("isDefault")), "")
    try:
        workspace = str(request.app.state.codex_threads.workspace)
        config_result = await read("config/read", {
            "cwd": workspace,
            "includeLayers": False,
        })
    except (AppServerError, AttributeError):
        config_result = {}
    config = config_result.get("config") if isinstance(config_result, dict) else {}
    effective_permission = _effective_permission(
        config if isinstance(config, dict) else {})
    return {
        "runtime": "codex",
        "providers": [],
        "defaults": {
            "model": default,
            # ``default`` remains the protocol value: omit per-thread/turn
            # overrides and inherit Codex.  Expose the resolved legacy mode
            # separately so clients can show what that inheritance means
            # instead of the misleading label "Codex default".
            "permission": "default",
            "effective_permission": effective_permission,
        },
        "params": {},
    }


def _effective_permission(config: dict[str, Any]) -> str:
    approval = config.get("approval_policy")
    sandbox = config.get("sandbox_mode")
    if approval == "never" and sandbox == "danger-full-access":
        return "bypassPermissions"
    if sandbox == "read-only":
        return "plan"
    if sandbox == "workspace-write":
        return "workspace"
    return "default"


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
