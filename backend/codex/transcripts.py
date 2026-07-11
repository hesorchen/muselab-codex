"""Fast read-only projection of Codex-owned JSONL transcripts."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import json
import os
from pathlib import Path
import threading
from typing import Any


_CACHE_ENTRIES = 16
_CACHE_SOURCE_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class TranscriptSnapshot:
    """UI-compatible items parsed from one immutable file signature."""

    items: tuple[dict[str, Any], ...]
    source_bytes: int
    token_usage: dict[str, Any] | None = None
    settings: dict[str, Any] | None = None


@dataclass(frozen=True)
class _CacheEntry:
    path: Path
    mtime_ns: int
    snapshot: TranscriptSnapshot


class CodexTranscriptStore:
    """Locate and cache Codex rollout JSONL without becoming a second store.

    Codex remains the sole writer and source of truth.  This class only keeps
    a bounded in-process projection, invalidated by the source file's path,
    nanosecond mtime, and size.
    """

    def __init__(self, codex_home: Path | None = None):
        configured = codex_home or Path(
            os.environ.get("CODEX_HOME") or Path.home() / ".codex"
        )
        self.sessions_root = Path(configured).expanduser().resolve() / "sessions"
        self._paths: dict[str, Path] = {}
        self._cache: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._lock = threading.Lock()

    async def read(self, thread_id: str) -> TranscriptSnapshot | None:
        clean_id = _thread_id(thread_id)
        return await asyncio.to_thread(self._read_sync, clean_id)

    def invalidate(self, thread_id: str) -> None:
        clean_id = _thread_id(thread_id)
        with self._lock:
            self._cache.pop(clean_id, None)

    def _read_sync(self, thread_id: str) -> TranscriptSnapshot | None:
        path = self._locate(thread_id)
        if path is None:
            return None
        try:
            stat = path.stat()
        except OSError:
            self._forget(thread_id, path)
            return None

        with self._lock:
            cached = self._cache.get(thread_id)
            if (
                cached is not None
                and cached.path == path
                and cached.mtime_ns == stat.st_mtime_ns
                and cached.snapshot.source_bytes == stat.st_size
            ):
                self._cache.move_to_end(thread_id)
                return cached.snapshot

        snapshot = _parse_transcript(path, stat.st_size)
        if snapshot is None:
            return None
        with self._lock:
            self._cache[thread_id] = _CacheEntry(path, stat.st_mtime_ns, snapshot)
            self._cache.move_to_end(thread_id)
            while len(self._cache) > 1 and (
                len(self._cache) > _CACHE_ENTRIES
                or sum(entry.snapshot.source_bytes for entry in self._cache.values())
                > _CACHE_SOURCE_BYTES
            ):
                self._cache.popitem(last=False)
        return snapshot

    def _locate(self, thread_id: str) -> Path | None:
        with self._lock:
            cached = self._paths.get(thread_id)
        if cached is not None and cached.is_file():
            return cached
        if not self.sessions_root.is_dir():
            return None
        matches = list(self.sessions_root.rglob(f"*-{thread_id}.jsonl"))
        if not matches:
            return None
        # A moved/copied CODEX_HOME can contain duplicate rollout names.  The
        # most recently modified file is the active transcript.
        try:
            path = max(matches, key=lambda item: item.stat().st_mtime_ns)
        except OSError:
            return None
        with self._lock:
            self._paths[thread_id] = path
        return path

    def _forget(self, thread_id: str, path: Path) -> None:
        with self._lock:
            if self._paths.get(thread_id) == path:
                self._paths.pop(thread_id, None)
            self._cache.pop(thread_id, None)


def _parse_transcript(path: Path, source_bytes: int) -> TranscriptSnapshot | None:
    items: list[dict[str, Any]] = []
    tool_names: dict[str, str] = {}
    token_usage: dict[str, Any] | None = None
    settings: dict[str, Any] | None = None
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    # Codex may be appending the last line while a browser
                    # reloads.  Earlier complete records remain usable.
                    continue
                if not isinstance(record, dict):
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue
                record_type = record.get("type")
                if record_type == "response_item":
                    _append_response_item(items, payload, tool_names)
                elif record_type == "turn_context":
                    settings = _turn_settings(payload)
                elif (
                    record_type == "event_msg"
                    and payload.get("type") == "context_compacted"
                ):
                    items.append({"type": "contextCompaction"})
                elif record_type == "event_msg" and payload.get("type") == "token_count":
                    parsed_usage = _token_usage(payload.get("info"))
                    if parsed_usage is not None:
                        token_usage = parsed_usage
    except OSError:
        return None
    return TranscriptSnapshot(tuple(items), source_bytes, token_usage, settings)


def _token_usage(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None

    def breakdown(raw: Any) -> dict[str, Any]:
        source = raw if isinstance(raw, dict) else {}
        return {
            "totalTokens": source.get("total_tokens", 0),
            "inputTokens": source.get("input_tokens", 0),
            "cachedInputTokens": source.get("cached_input_tokens", 0),
            "outputTokens": source.get("output_tokens", 0),
            "reasoningOutputTokens": source.get("reasoning_output_tokens", 0),
        }

    last = value.get("last_token_usage")
    total = value.get("total_token_usage")
    if not isinstance(last, dict) or not isinstance(total, dict):
        return None
    return {
        "last": breakdown(last),
        "total": breakdown(total),
        "modelContextWindow": value.get("model_context_window", 0),
    }


def _turn_settings(payload: dict[str, Any]) -> dict[str, Any]:
    approval = str(payload.get("approval_policy") or "")
    sandbox = payload.get("sandbox_policy")
    sandbox_type = str(sandbox.get("type") or "") if isinstance(sandbox, dict) else ""
    collaboration = payload.get("collaboration_mode")
    mode = str(collaboration.get("mode") or "") \
        if isinstance(collaboration, dict) else ""
    if mode == "plan":
        permission = "plan"
    elif approval == "never" and sandbox_type == "danger-full-access":
        permission = "bypassPermissions"
    elif approval == "untrusted":
        permission = "acceptEdits"
    else:
        permission = "default"
    return {
        "model": str(payload.get("model") or ""),
        "effort": str(payload.get("effort") or ""),
        "permission": permission,
    }


def _append_response_item(
    items: list[dict[str, Any]],
    payload: dict[str, Any],
    tool_names: dict[str, str],
) -> None:
    item_type = payload.get("type")
    if item_type == "message":
        role = payload.get("role")
        if role not in {"user", "assistant"}:
            return
        content = payload.get("content")
        if not isinstance(content, list):
            return
        text = "".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict)
            and part.get("type") in {"input_text", "output_text"}
        )
        metadata = payload.get("internal_chat_message_metadata_passthrough")
        turn_id = metadata.get("turn_id") if isinstance(metadata, dict) else None
        if role == "user":
            item: dict[str, Any] = {
                "type": "userMessage",
                "content": [{"type": "text", "text": text}],
            }
            if isinstance(turn_id, str):
                item["id"] = turn_id
            items.append(item)
        else:
            items.append({"type": "agentMessage", "text": text})
        return
    if item_type in {"custom_tool_call", "function_call"}:
        call_id = _call_id(payload)
        if not call_id:
            return
        raw_name = str(payload.get("name") or "Tool")
        raw_input = payload.get(
            "input" if item_type == "custom_tool_call" else "arguments")
        input_value = _tool_input(raw_input)
        name, summary = _tool_identity(raw_name, input_value)
        tool_names[call_id] = name
        items.append({
            "type": "toolUse",
            "id": call_id,
            "name": name,
            "summary": summary,
            "input": input_value,
        })
        return
    if item_type in {"custom_tool_call_output", "function_call_output"}:
        call_id = _call_id(payload)
        if not call_id:
            return
        text = _tool_output_text(payload.get("output"))
        items.append({
            "type": "toolResult",
            "id": call_id,
            "toolName": tool_names.get(call_id, "Tool"),
            "preview": text[:500],
            "text": text[:50_000],
            "truncated": len(text) > 50_000,
            "isError": False,
        })
        return
    if item_type != "reasoning":
        return
    parts = payload.get("summary") or payload.get("content") or []
    if not isinstance(parts, list):
        return
    text_parts = [
        str(part.get("text") or "")
        for part in parts
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    ]
    if text_parts:
        items.append({"type": "reasoning", "summary": text_parts})


def _call_id(payload: dict[str, Any]) -> str:
    value = payload.get("call_id") or payload.get("id")
    return value if isinstance(value, str) else ""


def _tool_input(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, str):
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        decoded = None
    if isinstance(decoded, dict):
        return decoded
    return {"code": value}


def _tool_identity(raw_name: str, value: dict[str, Any]) -> tuple[str, str]:
    code = value.get("code")
    if isinstance(code, str):
        if "tools.apply_patch" in code:
            return "ApplyPatch", "Apply code changes"
        if "tools.exec_command" in code:
            return "Exec", "Run a shell command"
        return "Exec", "Run tool orchestration"
    name = raw_name[:1].upper() + raw_name[1:] if raw_name else "Tool"
    return name, name


def _tool_output_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    return "".join(
        str(part.get("text") or "")
        for part in value
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    )


def _thread_id(value: str) -> str:
    clean = value.strip()
    if not clean or any(
        not (char.isascii() and (char.isalnum() or char in "_-"))
        for char in clean
    ):
        raise ValueError("invalid Codex thread id")
    return clean
