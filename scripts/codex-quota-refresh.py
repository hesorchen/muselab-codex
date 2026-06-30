#!/usr/bin/env python3
"""Refresh and read the local Codex account quota snapshot.

Codex has no stable shell `codex --status` JSON API. The least fragile
machine-readable path today is:

1. Run a tiny non-interactive Codex turn so the CLI receives fresh
   `rate_limits` metadata from the backend.
2. Read only `rate_limits` lines from the local Codex JSONL session logs.

This consumes a small Codex request. It does not read Codex auth files.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def _find_codex() -> str | None:
    explicit = os.environ.get("CODEX_BIN", "").strip()
    if explicit:
        return explicit
    found = shutil.which("codex")
    if found:
        return found
    fallback = Path.home() / ".npm-global" / "bin" / "codex"
    if fallback.exists():
        return str(fallback)
    return None


def _rate_limit_type(key: str, window: dict[str, Any]) -> str:
    minutes = int(window.get("window_minutes") or 0)
    if minutes == 300:
        return "five_hour"
    if minutes == 10080:
        return "seven_day"
    if 28 * 24 * 60 <= minutes <= 31 * 24 * 60:
        return "monthly"
    return key


def _from_payload(payload: dict[str, Any], source: Path, ts: str | None) -> dict[str, Any] | None:
    raw = payload.get("rate_limits")
    if not isinstance(raw, dict):
        return None
    windows: dict[str, dict[str, Any]] = {}
    reached = raw.get("rate_limit_reached_type")
    for key in ("primary", "secondary"):
        w = raw.get(key)
        if not isinstance(w, dict):
            continue
        try:
            used_f = float(w.get("used_percent"))
        except (TypeError, ValueError):
            used_f = None
        kind = _rate_limit_type(key, w)
        status = "allowed"
        if reached and (reached == key or reached == kind):
            status = "rejected"
        elif used_f is not None and used_f >= 90:
            status = "allowed_warning"
        windows[key] = {
            "rate_limit_type": kind,
            "window_minutes": int(w.get("window_minutes") or 0),
            "resets_at": int(w.get("resets_at") or 0) or None,
            "used_percent": used_f,
            "remaining_percent": (
                round(max(0.0, 100.0 - used_f), 1) if used_f is not None else None
            ),
            "utilization": (used_f / 100.0 if used_f is not None else None),
            "status": status,
        }
    if not windows:
        return None
    updated_at = 0.0
    if ts:
        try:
            updated_at = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            updated_at = 0.0
    return {
        "ok": True,
        "source": "codex-cli-exec",
        "source_scope": "codex_cli_exec_rate_limits",
        "provider_authoritative": False,
        "source_file": str(source),
        "updated_at": updated_at,
        "timestamp": ts,
        "limit_id": raw.get("limit_id"),
        "limit_name": raw.get("limit_name"),
        "plan_type": raw.get("plan_type"),
        "rate_limit_reached_type": reached,
        "credits": raw.get("credits"),
        "individual_limit": raw.get("individual_limit"),
        "windows": windows,
    }


def _latest_rate_limits(max_files: int) -> dict[str, Any]:
    sessions_dir = _codex_home() / "sessions"
    if not sessions_dir.exists():
        return {"ok": False, "reason": "codex_sessions_missing", "windows": {}, "updated_at": 0}
    try:
        files = sorted(
            sessions_dir.rglob("*.jsonl"),
            key=lambda p: p.stat().st_mtime_ns,
            reverse=True,
        )
    except OSError as e:
        return {
            "ok": False,
            "reason": f"codex_sessions_unreadable: {e}",
            "windows": {},
            "updated_at": 0,
        }
    for path in files[:max(1, max_files)]:
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            if '"rate_limits"' not in line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            parsed = _from_payload(payload, path, event.get("timestamp"))
            if parsed:
                return parsed
    return {"ok": False, "reason": "codex_rate_limits_not_found", "windows": {}, "updated_at": 0}


def _refresh(timeout: int) -> dict[str, Any]:
    codex = _find_codex()
    if not codex:
        return {"ok": False, "reason": "codex_not_found"}
    prompt = os.environ.get("MUSELAB_CODEX_QUOTA_PROMPT", "Reply with OK only.")
    cmd = [
        codex,
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--ignore-user-config",
        "--ignore-rules",
        prompt,
    ]
    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=tempfile.gettempdir(),
            input="",
            text=True,
            capture_output=True,
            timeout=max(5, timeout),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "codex_exec_timeout", "elapsed_s": round(time.time() - started, 1)}
    usage = None
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
            usage = event["usage"]
    return {
        "ok": proc.returncode == 0,
        "reason": None if proc.returncode == 0 else "codex_exec_failed",
        "returncode": proc.returncode,
        "elapsed_s": round(time.time() - started, 1),
        "usage": usage,
        "stderr_tail": proc.stderr[-800:] if proc.returncode != 0 else "",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=int, default=int(os.environ.get("MUSELAB_CODEX_QUOTA_TIMEOUT", "25")))
    parser.add_argument("--max-files", type=int, default=int(os.environ.get("MUSELAB_CODEX_RATE_LIMIT_SCAN_FILES", "80")))
    parser.add_argument("--no-refresh", action="store_true")
    args = parser.parse_args()

    refresh_result = {"ok": True, "skipped": True} if args.no_refresh else _refresh(args.timeout)
    latest = _latest_rate_limits(args.max_files)
    latest["refresh"] = refresh_result
    if refresh_result.get("ok") and not refresh_result.get("skipped"):
        latest["refreshed_at"] = time.time()
    print(json.dumps(latest, ensure_ascii=False))
    return 0 if latest.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
