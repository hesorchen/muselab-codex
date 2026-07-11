"""Persistent per-thread token usage for the Codex-native context meter."""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


_THREAD_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")


class CodexUsageService:
    """Normalize app-server token usage and keep a small restart-safe sidecar."""

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).resolve()
        raw = self.workspace / ".muselab-codex" / "usage"
        raw.mkdir(parents=True, exist_ok=True)
        self.directory = raw.resolve()
        try:
            self.directory.relative_to(self.workspace)
        except ValueError:
            raise ValueError("usage directory must stay inside the workspace") from None
        self._cache: dict[str, dict[str, Any]] = {}

    def update(
        self, thread_id: str, token_usage: dict[str, Any], *, model: str = "",
    ) -> dict[str, Any]:
        clean_id = _thread_id(thread_id)
        sanitized = _sanitize(token_usage)
        normalized = _normalize(sanitized)
        record = {
            "token_usage": sanitized,
            "updated_at": time.time(),
        }
        if model.strip():
            record["model"] = model.strip()
        self._cache[clean_id] = record
        _atomic_write_json(self.directory / f"{clean_id}.json", record)
        return dict(normalized)

    def dashboard(self, *, days: int = 30, tz_offset_minutes: int = 0) -> dict[str, Any]:
        """Aggregate the restart-safe native sidecars for the usage dashboard.

        App-server reports cumulative usage per thread, so historical sidecars
        can only be assigned to their latest update date. This keeps the
        dashboard honest without reconstructing usage from private transcripts.
        """
        tz = timezone(timedelta(minutes=tz_offset_minutes))
        today = datetime.now(tz).date()
        by_date = {
            (today - timedelta(days=offset)).isoformat(): _dashboard_bucket()
            for offset in range(days - 1, -1, -1)
        }
        all_time = _dashboard_bucket()
        by_model: dict[str, dict[str, Any]] = {}

        for path in self.directory.glob("*.json"):
            record = _read_json(path)
            if record is None:
                continue
            raw = record.get("token_usage")
            if not isinstance(raw, dict):
                continue
            bucket = _dashboard_usage(_sanitize(raw))
            _add_dashboard_bucket(all_time, bucket)
            updated_at = record.get("updated_at")
            updated_date = _local_date(updated_at, tz)
            if updated_date is not None and updated_date.isoformat() in by_date:
                _add_dashboard_bucket(by_date[updated_date.isoformat()], bucket)

            model = str(record.get("model") or "Codex")
            model_bucket = by_model.setdefault(
                model, {"model": model, "label": model, **_dashboard_bucket()})
            _add_dashboard_bucket(model_bucket, bucket)

        def period(day_count: int) -> dict[str, Any]:
            result = _dashboard_bucket()
            start = today - timedelta(days=day_count - 1)
            for day_text, bucket in by_date.items():
                if date.fromisoformat(day_text) >= start:
                    _add_dashboard_bucket(result, bucket)
            return result

        return {
            "runtime": "codex",
            "authoritative": False,
            "breakdown_available": True,
            "window_days": days,
            "today": period(1),
            "last_7d": period(7),
            "last_30d": period(30),
            "all_time": all_time,
            "by_day": [
                {"date": day_text, **bucket} for day_text, bucket in by_date.items()
            ],
            "by_model": sorted(
                by_model.values(),
                key=lambda item: item["input_tokens"] + item["output_tokens"],
                reverse=True,
            ),
        }

    def account_dashboard(
        self,
        payload: dict[str, Any],
        *,
        days: int = 30,
        tz_offset_minutes: int = 0,
    ) -> dict[str, Any]:
        """Project native ``account/usage/read`` into the dashboard shape."""
        summary = payload.get("summary")
        buckets = payload.get("dailyUsageBuckets")
        if not isinstance(summary, dict) or not isinstance(buckets, list):
            raise ValueError("account/usage/read returned an invalid result")

        tz = timezone(timedelta(minutes=tz_offset_minutes))
        today = datetime.now(tz).date()
        parsed: dict[date, int] = {}
        for raw in buckets:
            if not isinstance(raw, dict) or not isinstance(raw.get("startDate"), str):
                continue
            try:
                day = date.fromisoformat(raw["startDate"][:10])
            except ValueError:
                continue
            parsed[day] = _nonnegative_int(raw.get("tokens"))

        available_days = min(days, max(1, len(parsed)))
        start = today - timedelta(days=available_days - 1)
        by_day = [
            {"date": day.isoformat(), **_native_dashboard_bucket(parsed.get(day, 0))}
            for day in (start + timedelta(days=offset) for offset in range(available_days))
        ]

        def period(day_count: int) -> dict[str, Any]:
            cutoff = today - timedelta(days=day_count - 1)
            return _native_dashboard_bucket(sum(
                tokens for day, tokens in parsed.items() if cutoff <= day <= today
            ))

        lifetime = _nonnegative_int(summary.get("lifetimeTokens"))
        return {
            "runtime": "codex",
            "authoritative": True,
            "breakdown_available": False,
            "window_days": available_days,
            "today": period(1),
            "last_7d": period(7),
            "last_30d": period(30),
            "all_time": _native_dashboard_bucket(lifetime),
            "by_day": by_day,
            "by_model": [],
            "account_summary": {
                key: summary.get(key)
                for key in (
                    "currentStreakDays",
                    "longestStreakDays",
                    "longestRunningTurnSec",
                    "peakDailyTokens",
                )
            },
        }

    def get(self, thread_id: str, *, model: str = "") -> dict[str, Any]:
        clean_id = _thread_id(thread_id)
        record = self._cache.get(clean_id)
        if record is None:
            record = _read_json(self.directory / f"{clean_id}.json")
            if record is not None:
                self._cache[clean_id] = record
        raw = record.get("token_usage") if isinstance(record, dict) else None
        result = _normalize(_sanitize(raw)) if isinstance(raw, dict) else _empty_usage()
        if isinstance(record, dict):
            updated_at = record.get("updated_at")
            if isinstance(updated_at, (int, float)) and not isinstance(updated_at, bool):
                result["last_turn_at"] = max(0.0, float(updated_at))
        result["model"] = model
        return result

    def breakdown(self, thread_id: str) -> dict[str, Any]:
        clean_id = _thread_id(thread_id)
        record = self._cache.get(clean_id)
        if record is None:
            record = _read_json(self.directory / f"{clean_id}.json")
            if record is not None:
                self._cache[clean_id] = record
        raw = record.get("token_usage") if isinstance(record, dict) else None
        if not isinstance(raw, dict):
            return {
                "totalTokens": 0,
                "maxTokens": 0,
                "percentage": 0.0,
                "categories": [],
            }
        last = _breakdown(raw.get("last") or raw.get("total"))
        total_tokens = max(
            0, last["totalTokens"] - last["reasoningOutputTokens"])
        max_tokens = _nonnegative_int(raw.get("modelContextWindow"))
        categories = _categories(last)
        return {
            "totalTokens": total_tokens,
            "maxTokens": max_tokens,
            "percentage": round(total_tokens / max_tokens * 100, 1) if max_tokens else 0.0,
            "categories": categories,
            "memoryFiles": [],
            "mcpTools": [],
            "agents": [],
        }

    def delete(self, thread_id: str) -> None:
        clean_id = _thread_id(thread_id)
        self._cache.pop(clean_id, None)
        (self.directory / f"{clean_id}.json").unlink(missing_ok=True)


def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
    total = _breakdown(raw.get("total"))
    last = _breakdown(raw.get("last") or raw.get("total"))
    # Match Codex CLI's ``tokens_in_context_window`` calculation: reasoning
    # output from prior turns is not sent back into the model context.
    context_used = max(0, last["totalTokens"] - last["reasoningOutputTokens"])
    context_limit = _nonnegative_int(raw.get("modelContextWindow"))
    return {
        "input_tokens": total["inputTokens"],
        "output_tokens": total["outputTokens"],
        "reasoning_output_tokens": total["reasoningOutputTokens"],
        "cache_read_tokens": total["cachedInputTokens"],
        "cache_creation_tokens": 0,
        "total_tokens": total["totalTokens"],
        "context_used": context_used,
        "context_limit": context_limit,
        "context_used_pct": (
            round(context_used / context_limit * 100, 1) if context_limit else 0.0
        ),
        "last_turn_at": time.time(),
    }


def _sanitize(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "total": _breakdown(raw.get("total")),
        "last": _breakdown(raw.get("last") or raw.get("total")),
        "modelContextWindow": _nonnegative_int(raw.get("modelContextWindow")),
    }


def _empty_usage() -> dict[str, Any]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "total_tokens": 0,
        "context_used": 0,
        "context_limit": 0,
        "context_used_pct": 0.0,
        "last_turn_at": 0.0,
    }


def _dashboard_bucket() -> dict[str, Any]:
    return {
        "total_tokens": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "cost": 0.0,
        "turns": 0,
    }


def _dashboard_usage(raw: dict[str, Any]) -> dict[str, Any]:
    total = _breakdown(raw.get("total"))
    input_tokens = max(0, total["inputTokens"] - total["cachedInputTokens"])
    return {
        "total_tokens": total["totalTokens"],
        "input_tokens": input_tokens,
        "output_tokens": total["outputTokens"],
        "cache_read_tokens": total["cachedInputTokens"],
        "cache_creation_tokens": 0,
        "cost": 0.0,
        "turns": 1,
    }


def _native_dashboard_bucket(tokens: int) -> dict[str, Any]:
    return {**_dashboard_bucket(), "total_tokens": max(0, tokens)}


def _add_dashboard_bucket(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key in _dashboard_bucket():
        target[key] += source[key]


def _local_date(value: Any, tz: timezone) -> date | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(value, tz).date()
    except (OSError, OverflowError, ValueError):
        return None


def _breakdown(value: Any) -> dict[str, int]:
    source = value if isinstance(value, dict) else {}
    return {
        key: _nonnegative_int(source.get(key))
        for key in (
            "totalTokens",
            "inputTokens",
            "cachedInputTokens",
            "outputTokens",
            "reasoningOutputTokens",
        )
    }


def _categories(last: dict[str, int]) -> list[dict[str, Any]]:
    values = (
        ("Input", max(0, last["inputTokens"] - last["cachedInputTokens"]), "#4f7cff"),
        ("Cached input", last["cachedInputTokens"], "#22a06b"),
        ("Output", max(0, last["outputTokens"] - last["reasoningOutputTokens"]), "#a855f7"),
    )
    return [
        {"name": name, "tokens": tokens, "color": color}
        for name, tokens, color in values
        if tokens > 0
    ]


def _nonnegative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _thread_id(value: str) -> str:
    clean = value.strip()
    if not _THREAD_RE.fullmatch(clean):
        raise ValueError("invalid thread id")
    return clean


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
