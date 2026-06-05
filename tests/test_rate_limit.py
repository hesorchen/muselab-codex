"""Tests for Pro/Max rate-limit capture (RateLimitEvent → store → endpoint).

The SDK pushes a RateLimitEvent whenever the limit state changes; muselab
records the latest RateLimitInfo per window and exposes a snapshot at
GET /api/chat/rate-limit (live deltas go over SSE). These tests exercise the
store + serializer + endpoint without spawning a CLI.
"""
import pytest

from tests.conftest import TEST_TOKEN


@pytest.fixture()
def chat_mod(app_module):
    from backend import chat as chat_mod
    chat_mod._rate_limit_state.clear()
    chat_mod._rate_limit_updated_at = 0.0
    yield chat_mod
    chat_mod._rate_limit_state.clear()
    chat_mod._rate_limit_updated_at = 0.0


class _Info:
    """Minimal stand-in for SDK RateLimitInfo (only the read fields)."""
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def test_snapshot_empty_initially(client, chat_mod):
    r = client.get("/api/chat/rate-limit", headers={"X-Auth-Token": TEST_TOKEN})
    assert r.status_code == 200
    body = r.json()
    assert body == {"windows": {}, "updated_at": 0.0}


def test_record_then_snapshot(client, chat_mod):
    chat_mod._record_rate_limit(_Info(
        status="allowed_warning", rate_limit_type="five_hour",
        utilization=0.82, resets_at=1234,
        overage_status=None, overage_resets_at=None,
        overage_disabled_reason=None))
    r = client.get("/api/chat/rate-limit", headers={"X-Auth-Token": TEST_TOKEN})
    body = r.json()
    assert set(body["windows"]) == {"five_hour"}
    w = body["windows"]["five_hour"]
    assert w["status"] == "allowed_warning"
    assert w["utilization"] == 0.82
    assert w["resets_at"] == 1234
    assert "updated_at" in w
    assert body["updated_at"] > 0


def test_windows_tracked_independently(client, chat_mod):
    chat_mod._record_rate_limit(_Info(
        status="allowed", rate_limit_type="five_hour", utilization=0.3,
        resets_at=1, overage_status=None, overage_resets_at=None,
        overage_disabled_reason=None))
    chat_mod._record_rate_limit(_Info(
        status="rejected", rate_limit_type="seven_day", utilization=1.0,
        resets_at=2, overage_status=None, overage_resets_at=None,
        overage_disabled_reason=None))
    body = client.get("/api/chat/rate-limit",
                      headers={"X-Auth-Token": TEST_TOKEN}).json()
    assert set(body["windows"]) == {"five_hour", "seven_day"}
    assert body["windows"]["seven_day"]["status"] == "rejected"


def test_same_window_overwrites(chat_mod):
    chat_mod._record_rate_limit(_Info(
        status="allowed", rate_limit_type="five_hour", utilization=0.3,
        resets_at=1, overage_status=None, overage_resets_at=None,
        overage_disabled_reason=None))
    chat_mod._record_rate_limit(_Info(
        status="allowed_warning", rate_limit_type="five_hour", utilization=0.9,
        resets_at=9, overage_status=None, overage_resets_at=None,
        overage_disabled_reason=None))
    assert len(chat_mod._rate_limit_state) == 1
    assert chat_mod._rate_limit_state["five_hour"]["utilization"] == 0.9


def test_untyped_window_bucketed(chat_mod):
    """rate_limit_type is Optional — an untyped event must still surface."""
    chat_mod._record_rate_limit(_Info(
        status="allowed", rate_limit_type=None, utilization=0.5,
        resets_at=1, overage_status=None, overage_resets_at=None,
        overage_disabled_reason=None))
    assert "_" in chat_mod._rate_limit_state


def test_payload_degrades_on_missing_fields(chat_mod):
    """A future/partial RateLimitInfo missing fields must not crash — every
    field is read via getattr-default (same discipline as Task* handlers)."""
    payload = chat_mod._rate_limit_payload(_Info(status="allowed"))
    assert payload["status"] == "allowed"
    assert payload["utilization"] is None
    assert payload["rate_limit_type"] is None
