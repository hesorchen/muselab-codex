"""Context-meter normalization and persistence tests."""

from datetime import datetime, timedelta, timezone

import pytest

from backend.codex import CodexUsageService


TOKEN_USAGE = {
    "total": {
        "totalTokens": 120,
        "inputTokens": 90,
        "cachedInputTokens": 40,
        "outputTokens": 30,
        "reasoningOutputTokens": 10,
    },
    "last": {
        "totalTokens": 75,
        "inputTokens": 60,
        "cachedInputTokens": 25,
        "outputTokens": 15,
        "reasoningOutputTokens": 5,
    },
    "modelContextWindow": 300,
}


def test_usage_uses_last_for_context_and_total_for_accounting(tmp_path):
    service = CodexUsageService(tmp_path)

    usage = service.update("thread-1", TOKEN_USAGE)

    assert usage["total_tokens"] == 120
    assert usage["input_tokens"] == 90
    assert usage["context_used"] == 70
    assert usage["context_limit"] == 300
    assert usage["context_used_pct"] == 23.3

    breakdown = service.breakdown("thread-1")
    assert breakdown["totalTokens"] == 70
    assert sum(item["tokens"] for item in breakdown["categories"]) == 70


def test_usage_survives_service_restart_and_delete(tmp_path):
    CodexUsageService(tmp_path).update("thread-1", TOKEN_USAGE)

    restarted = CodexUsageService(tmp_path)
    assert restarted.get("thread-1", model="gpt-test")["context_used"] == 70
    assert restarted.get("thread-1", model="gpt-test")["model"] == "gpt-test"

    restarted.delete("thread-1")
    assert CodexUsageService(tmp_path).get("thread-1")["context_used"] == 0


def test_dashboard_aggregates_native_sidecars(tmp_path):
    service = CodexUsageService(tmp_path)
    service.update("thread-1", TOKEN_USAGE, model="gpt-test")

    dashboard = service.dashboard(days=3, tz_offset_minutes=480)

    assert dashboard["runtime"] == "codex"
    assert dashboard["window_days"] == 3
    assert len(dashboard["by_day"]) == 3
    assert dashboard["today"]["input_tokens"] == 50
    assert dashboard["last_7d"]["output_tokens"] == 30
    assert dashboard["all_time"]["cache_read_tokens"] == 40
    assert dashboard["by_model"] == [{
        "model": "gpt-test",
        "label": "gpt-test",
        "total_tokens": 120,
        "input_tokens": 50,
        "output_tokens": 30,
        "cache_read_tokens": 40,
        "cache_creation_tokens": 0,
        "cost": 0.0,
        "turns": 1,
    }]


def test_account_dashboard_uses_native_lifetime_and_daily_buckets(tmp_path):
    service = CodexUsageService(tmp_path)
    user_tz = timezone(timedelta(minutes=480))
    today = datetime.now(user_tz).date()
    dashboard = service.account_dashboard({
        "summary": {
            "lifetimeTokens": 9000,
            "currentStreakDays": 3,
            "peakDailyTokens": 700,
        },
        "dailyUsageBuckets": [
            {"startDate": (today - timedelta(days=1)).isoformat(), "tokens": 400},
            {"startDate": today.isoformat(), "tokens": 600},
        ],
    }, days=30, tz_offset_minutes=480)

    assert dashboard["authoritative"] is True
    assert dashboard["breakdown_available"] is False
    assert dashboard["all_time"]["total_tokens"] == 9000
    assert dashboard["today"]["total_tokens"] == 600
    assert dashboard["today_pending"] is False
    assert dashboard["last_7d"]["total_tokens"] == 1000
    assert dashboard["by_model"] == []


def test_account_dashboard_marks_missing_today_bucket_as_pending(tmp_path):
    service = CodexUsageService(tmp_path)
    user_tz = timezone(timedelta(minutes=480))
    today = datetime.now(user_tz).date()
    dashboard = service.account_dashboard({
        "summary": {"lifetimeTokens": 400},
        "dailyUsageBuckets": [{
            "startDate": (today - timedelta(days=1)).isoformat(),
            "tokens": 400,
        }],
    }, days=30, tz_offset_minutes=480)

    assert dashboard["today"]["total_tokens"] == 0
    assert dashboard["today_pending"] is True


def test_usage_root_symlink_cannot_escape_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    hidden = workspace / ".muselab-codex"
    outside = tmp_path / "outside"
    hidden.mkdir(parents=True)
    outside.mkdir()
    (hidden / "usage").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="inside the workspace"):
        CodexUsageService(workspace)
