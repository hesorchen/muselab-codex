"""Codex app-server Skills discovery and configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .process import AppServerProtocolError
from .threads import Requester


class CodexSkillsService:
    """Expose workspace-scoped skills without scanning obsolete runtime paths."""

    def __init__(self, requester: Requester, workspace: Path):
        self.requester = requester
        self.workspace = Path(workspace).resolve()

    async def list(self, *, force_reload: bool = False) -> dict[str, Any]:
        result = await self.requester.request("skills/list", {
            "cwds": [str(self.workspace)],
            "forceReload": force_reload,
        })
        if not isinstance(result, dict) or not isinstance(result.get("data"), list):
            raise AppServerProtocolError("skills/list returned an invalid result")
        skills: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        seen_paths: set[str] = set()
        for entry in result["data"]:
            if not isinstance(entry, dict):
                raise AppServerProtocolError("skills/list returned an invalid entry")
            raw_skills = entry.get("skills")
            raw_errors = entry.get("errors")
            if not isinstance(raw_skills, list) or not isinstance(raw_errors, list):
                raise AppServerProtocolError("skills/list returned an invalid entry")
            for raw in raw_skills:
                skill = _skill(raw)
                if skill["path"] in seen_paths:
                    continue
                seen_paths.add(skill["path"])
                skills.append(skill)
            for raw in raw_errors:
                if not isinstance(raw, dict):
                    continue
                path = raw.get("path")
                message = raw.get("message")
                if isinstance(path, str) and isinstance(message, str):
                    errors.append({"path": path, "message": message})
        skills.sort(key=lambda skill: (skill["name"].casefold(), skill["path"]))
        return {"skills": skills, "errors": errors}

    async def set_enabled(self, path: str, enabled: bool) -> dict[str, Any]:
        clean_path = path.strip()
        if not clean_path:
            raise ValueError("skill path cannot be empty")
        listed = await self.list()
        known = next(
            (skill for skill in listed["skills"] if skill["path"] == clean_path),
            None,
        )
        if known is None:
            raise ValueError("skill path is not present in the current Codex skill list")
        result = await self.requester.request("skills/config/write", {
            "path": clean_path,
            "enabled": enabled,
        })
        if not isinstance(result, dict) or not isinstance(
            result.get("effectiveEnabled"), bool
        ):
            raise AppServerProtocolError(
                "skills/config/write returned an invalid result")
        refreshed = await self.list(force_reload=True)
        updated = next(
            (skill for skill in refreshed["skills"] if skill["path"] == clean_path),
            dict(known),
        )
        updated["enabled"] = result["effectiveEnabled"]
        updated["disabled"] = not result["effectiveEnabled"]
        return {"ok": True, "skill": updated}


def _skill(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise AppServerProtocolError("skills/list returned an invalid skill")
    required = {
        "name": str,
        "description": str,
        "path": str,
        "scope": str,
        "enabled": bool,
    }
    for key, expected in required.items():
        if not isinstance(raw.get(key), expected):
            raise AppServerProtocolError(
                f"skills/list returned a skill with invalid {key}")
    interface = raw.get("interface")
    interface = interface if isinstance(interface, dict) else {}
    description = (
        interface.get("shortDescription")
        or raw.get("shortDescription")
        or raw["description"]
    )
    return {
        "name": raw["name"],
        "display_name": interface.get("displayName") or raw["name"],
        "description": str(description),
        "scope": raw["scope"],
        "source": raw["scope"],
        "path": raw["path"],
        "enabled": raw["enabled"],
        "disabled": not raw["enabled"],
        "default_prompt": interface.get("defaultPrompt") or "",
    }
