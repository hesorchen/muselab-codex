import json


def test_codex_rate_limit_reads_local_session_log(client, auth, tmp_path, monkeypatch):
    codex_home = tmp_path / "codex"
    sessions = codex_home / "sessions" / "2026" / "06" / "27"
    sessions.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    event = {
        "timestamp": "2026-06-27T11:08:30.585Z",
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": None,
            "rate_limits": {
                "limit_id": "codex",
                "plan_type": "plus",
                "primary": {
                    "used_percent": 46.0,
                    "window_minutes": 300,
                    "resets_at": 1782565701,
                },
                "secondary": {
                    "used_percent": 7.0,
                    "window_minutes": 43200,
                    "resets_at": 1783152501,
                },
                "credits": None,
                "individual_limit": None,
                "rate_limit_reached_type": None,
            },
        },
    }
    (sessions / "rollout.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")

    r = client.get("/api/chat/codex-rate-limit", headers=auth)
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["plan_type"] == "plus"
    assert d["provider_authoritative"] is False
    assert d["source_scope"] == "codex_cli_session_log"
    assert d["windows"]["primary"]["rate_limit_type"] == "five_hour"
    assert d["windows"]["primary"]["remaining_percent"] == 54.0
    assert d["windows"]["secondary"]["rate_limit_type"] == "monthly"
    assert d["windows"]["secondary"]["remaining_percent"] == 93.0


def test_codex_rate_limit_labels_weekly_window(client, auth, tmp_path, monkeypatch):
    codex_home = tmp_path / "codex"
    sessions = codex_home / "sessions" / "2026" / "06" / "30"
    sessions.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    event = {
        "timestamp": "2026-06-30T06:24:59.760Z",
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "rate_limits": {
                "limit_id": "codex",
                "plan_type": "prolite",
                "primary": {
                    "used_percent": 1.0,
                    "window_minutes": 300,
                    "resets_at": 1782809110,
                },
                "secondary": {
                    "used_percent": 0.0,
                    "window_minutes": 10080,
                    "resets_at": 1783395910,
                },
                "rate_limit_reached_type": None,
            },
        },
    }
    (sessions / "rollout.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")

    r = client.get("/api/chat/codex-rate-limit", headers=auth)
    assert r.status_code == 200
    d = r.json()
    assert d["windows"]["primary"]["rate_limit_type"] == "five_hour"
    assert d["windows"]["secondary"]["rate_limit_type"] == "seven_day"


def test_codex_rate_limit_refresh_uses_cli_script(client, auth, monkeypatch):
    from backend import chat as chat_mod

    payload = {
        "ok": True,
        "source": "codex-cli-exec",
        "source_scope": "codex_cli_exec_rate_limits",
        "provider_authoritative": False,
        "updated_at": 1782802582.322,
        "windows": {
            "primary": {
                "rate_limit_type": "five_hour",
                "used_percent": 8.0,
                "remaining_percent": 92.0,
            },
        },
        "refresh": {"ok": True, "elapsed_s": 7.4},
    }
    monkeypatch.setattr(chat_mod, "_refresh_codex_rate_limits", lambda: payload)

    r = client.get("/api/chat/codex-rate-limit?refresh=1", headers=auth)
    assert r.status_code == 200
    d = r.json()
    assert d["source"] == "codex-cli-exec"
    assert d["refresh"]["ok"] is True
    assert d["windows"]["primary"]["remaining_percent"] == 92.0
