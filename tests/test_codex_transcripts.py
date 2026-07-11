"""Read-only Codex JSONL transcript projection."""

import json
import os
from pathlib import Path

import pytest

from backend.codex import CodexTranscriptStore


def _write(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_transcript_projects_messages_reasoning_and_compaction(tmp_path):
    path = tmp_path / "sessions/2026/07/10/rollout-now-thread-1.jsonl"
    _write(path, [
        {"type": "response_item", "payload": {
            "type": "message", "role": "developer",
            "content": [{"type": "input_text", "text": "hidden"}],
        }},
        {"type": "response_item", "payload": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "hello"}],
            "internal_chat_message_metadata_passthrough": {"turn_id": "turn-1"},
        }},
        {"type": "response_item", "payload": {
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": "considering"}],
        }},
        {"type": "response_item", "payload": {
            "type": "message", "role": "assistant", "phase": "final_answer",
            "content": [{"type": "output_text", "text": "world"}],
        }},
        {"type": "event_msg", "payload": {"type": "context_compacted"}},
    ])
    store = CodexTranscriptStore(tmp_path)

    snapshot = await store.read("thread-1")

    assert snapshot is not None
    assert list(snapshot.items) == [
        {"type": "userMessage", "id": "turn-1",
         "content": [{"type": "text", "text": "hello"}]},
        {"type": "reasoning", "summary": ["considering"]},
        {"type": "agentMessage", "text": "world"},
        {"type": "contextCompaction"},
    ]


@pytest.mark.asyncio
async def test_transcript_projects_latest_usage_and_effective_settings(tmp_path):
    path = tmp_path / "sessions/2026/07/10/rollout-now-thread-usage.jsonl"
    _write(path, [
        {"type": "turn_context", "payload": {
            "model": "gpt-test", "effort": "high",
            "approval_policy": "never",
            "sandbox_policy": {"type": "danger-full-access"},
            "collaboration_mode": {"mode": "default", "settings": {
                "model": "gpt-test", "reasoning_effort": "high",
            }},
        }},
        {"type": "event_msg", "payload": {
            "type": "token_count", "info": {
                "total_token_usage": {
                    "total_tokens": 1000, "input_tokens": 900,
                    "cached_input_tokens": 700, "output_tokens": 100,
                    "reasoning_output_tokens": 40,
                },
                "last_token_usage": {
                    "total_tokens": 400, "input_tokens": 350,
                    "cached_input_tokens": 300, "output_tokens": 50,
                    "reasoning_output_tokens": 20,
                },
                "model_context_window": 2000,
            },
        }},
    ])

    snapshot = await CodexTranscriptStore(tmp_path).read("thread-usage")

    assert snapshot is not None
    assert snapshot.settings == {
        "model": "gpt-test", "effort": "high",
        "permission": "bypassPermissions",
    }
    assert snapshot.token_usage == {
        "total": {
            "totalTokens": 1000, "inputTokens": 900,
            "cachedInputTokens": 700, "outputTokens": 100,
            "reasoningOutputTokens": 40,
        },
        "last": {
            "totalTokens": 400, "inputTokens": 350,
            "cachedInputTokens": 300, "outputTokens": 50,
            "reasoningOutputTokens": 20,
        },
        "modelContextWindow": 2000,
    }


@pytest.mark.asyncio
async def test_transcript_cache_invalidates_when_rollout_grows(tmp_path):
    path = tmp_path / "sessions/2026/07/10/rollout-now-thread-2.jsonl"
    first = {"type": "response_item", "payload": {
        "type": "message", "role": "user",
        "content": [{"type": "input_text", "text": "one"}],
    }}
    second = {"type": "response_item", "payload": {
        "type": "message", "role": "assistant",
        "content": [{"type": "output_text", "text": "two"}],
    }}
    _write(path, [first])
    store = CodexTranscriptStore(tmp_path)
    before = await store.read("thread-2")

    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(second) + "\n")
    os.utime(path, None)
    after = await store.read("thread-2")

    assert before is not None and len(before.items) == 1
    assert after is not None and len(after.items) == 2


@pytest.mark.asyncio
async def test_transcript_projects_tool_calls_outputs_and_patch_source(tmp_path):
    path = tmp_path / "sessions/2026/07/10/rollout-now-thread-tools.jsonl"
    patch_source = (
        'const patch = "*** Begin Patch\\n*** End Patch"; '
        "text(await tools.apply_patch(patch));"
    )
    _write(path, [
        {"type": "response_item", "payload": {
            "type": "custom_tool_call", "id": "item-1", "call_id": "call-1",
            "name": "exec", "status": "completed", "input": patch_source,
        }},
        {"type": "response_item", "payload": {
            "type": "custom_tool_call_output", "call_id": "call-1",
            "output": [{"type": "text", "text": "patch applied"}],
        }},
        {"type": "response_item", "payload": {
            "type": "function_call", "id": "item-2", "call_id": "call-2",
            "name": "wait", "arguments": '{"cell_id":"7"}',
        }},
        {"type": "response_item", "payload": {
            "type": "function_call_output", "call_id": "call-2",
            "output": [{"type": "text", "text": "done"}],
        }},
    ])
    store = CodexTranscriptStore(tmp_path)

    snapshot = await store.read("thread-tools")

    assert snapshot is not None
    assert list(snapshot.items) == [
        {
            "type": "toolUse", "id": "call-1", "name": "ApplyPatch",
            "summary": "Apply code changes", "input": {"code": patch_source},
        },
        {
            "type": "toolResult", "id": "call-1", "toolName": "ApplyPatch",
            "preview": "patch applied", "text": "patch applied",
            "truncated": False, "isError": False,
        },
        {
            "type": "toolUse", "id": "call-2", "name": "Wait",
            "summary": "Wait", "input": {"cell_id": "7"},
        },
        {
            "type": "toolResult", "id": "call-2", "toolName": "Wait",
            "preview": "done", "text": "done", "truncated": False,
            "isError": False,
        },
    ]


@pytest.mark.asyncio
async def test_transcript_rejects_glob_metacharacters(tmp_path):
    store = CodexTranscriptStore(tmp_path)
    with pytest.raises(ValueError, match="invalid Codex thread id"):
        await store.read("thread-*")
