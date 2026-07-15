"""Browser stress checks for long chat rendering.

These tests deliberately run against the real frontend bundle and Alpine DOM,
but keep the model/provider path deterministic by injecting controlled session
state or a fake EventSource stream. They cover the long-history and long-stream
regression classes that static lint cannot see.
"""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("playwright.sync_api",
                    reason="install with: uv add --group dev pytest-playwright")
from playwright.sync_api import Page, TimeoutError, expect  # noqa: E402


SEL_LOGIN = ".login"
SEL_LOGIN_INPUT = '.login input[type="password"]'
SEL_TABS = ".chat-tabs-list"
SEL_MOBILE_TAB = ".mobile-tab-bar button"


def _login(page: Page, base: str, token: str) -> None:
    page.goto(base, wait_until="domcontentloaded")
    page.wait_for_selector(f"{SEL_LOGIN}, {SEL_TABS}", state="visible", timeout=5000)
    if page.locator(SEL_LOGIN).is_visible():
        page.fill(SEL_LOGIN_INPUT, token)
        page.keyboard.press("Enter")
    expect(page.locator(SEL_TABS)).to_be_visible(timeout=5000)
    page.wait_for_function(
        """() => {
          const app = document.querySelector("#app")?._x_dataStack?.[0];
          return app && app.authed === true && app.appReady === true
            && app._sessionsInitialized === true && app.currentId
            && app.openTabIds.includes(app.currentId) && app.sessions.length > 0;
        }"""
    )


def _capture_browser_errors(page: Page) -> list[str]:
    errors: list[str] = []

    def on_console(msg):
        if msg.type in {"error", "warning"}:
            text = msg.text
            # The app intentionally logs failed optional backend probes during
            # isolated e2e setup; pageerror and muse-capture are still fatal.
            if text.startswith("Failed to load resource:"):
                return
            if "[muse-capture]" in text or msg.type == "error":
                errors.append(f"console.{msg.type}: {text}")

    page.on("console", on_console)
    page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
    return errors


def _assert_no_browser_errors(page: Page, errors: list[str]) -> None:
    muse_errors = page.evaluate("() => (window.__museErrors__ || []).map(e => e.message)")
    assert not errors, "\n".join(errors)
    assert not muse_errors, f"window.__museErrors__ not empty: {muse_errors}"


def _app_eval(page: Page, body: str, arg=None):
    return page.evaluate(
        """([body, arg]) => {
            const app = document.querySelector("#app")._x_dataStack[0];
            return (new Function("app", "arg", body))(app, arg);
        }""",
        [body, arg],
    )


def _make_mixed_messages(total: int, prefix: str) -> list[dict]:
    messages: list[dict] = []
    for i in range(total):
        marker = f"{prefix}_{i:03d}"
        kind = i % 8
        if kind in {0, 4}:
            messages.append({
                "role": "user",
                "text": f"{marker} user prompt " + ("mobile tail paging " * 5),
                "ts": 1_700_000_000 + i,
                "uuid": f"{prefix}-u-{i}",
            })
        elif kind in {1, 5, 7}:
            text = f"{marker} assistant reply " + ("rendered markdown paragraph " * 8)
            messages.append({
                "role": "assistant",
                "text": text,
                "html": f"<p>{text}</p>",
                "ts": 1_700_000_000 + i,
                "uuid": f"{prefix}-a-{i}",
            })
        elif kind == 2:
            messages.append({
                "role": "tool_use",
                "name": "Bash",
                "summary": f"{marker} inspect fixture",
                "input": {"command": f"printf {marker}"},
                "text": f"{marker} tool use",
                "ts": 1_700_000_000 + i,
                "uuid": f"{prefix}-tu-{i}",
            })
        else:
            messages.append({
                "role": "tool_result",
                "tool_name": "Bash",
                "preview": f"{marker} ok",
                "text": f"{marker} tool result\n" + ("stdout line\n" * 3),
                "truncated": False,
                "is_error": False,
                "ts": 1_700_000_000 + i,
                "uuid": f"{prefix}-tr-{i}",
            })
    if messages:
        marker = f"{prefix}_{total - 1:03d}"
        text = f"{marker} latest assistant reply " + ("rendered markdown paragraph " * 8)
        messages[-1] = {
            "role": "assistant",
            "text": text,
            "html": f"<p>{text}</p>",
            "ts": 1_700_000_000 + total,
            "uuid": f"{prefix}-latest",
        }
    return messages


def _route_windowed_session(page: Page, sid: str, messages: list[dict]) -> list[dict]:
    requests: list[dict] = []

    def handle(route):
        url = route.request.url
        qs = parse_qs(urlparse(url).query)
        total = len(messages)
        offset = 0
        window = messages
        if "tail" in qs:
            tail = int(qs["tail"][0])
            offset = max(0, total - tail)
            window = messages[offset:]
        elif "offset" in qs and "limit" in qs:
            offset = int(qs["offset"][0])
            limit = int(qs["limit"][0])
            window = messages[offset:offset + limit]
        requests.append({
            "url": url,
            "tail": int(qs["tail"][0]) if "tail" in qs else None,
            "offset": offset,
            "limit": int(qs["limit"][0]) if "limit" in qs else None,
            "count": len(window),
        })
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "id": sid,
                "name": "Perf windowed session",
                "model": "e2e-model",
                "permission": "bypassPermissions",
                "thinking": True,
                "messages": window,
                "offset": offset,
                "total": total,
                "has_more": offset > 0,
            }),
        )

    page.route(f"**/api/chat/sessions/{sid}?*", handle)
    return requests


def _install_fake_event_source(page: Page) -> None:
    page.add_init_script(
        """
        (() => {
          const streams = [];
          class FakeEventSource extends EventTarget {
            constructor(url) {
              super();
              this.url = url;
              this.readyState = 0;
              streams.push(this);
              setTimeout(() => {
                this.readyState = 1;
                if (this.onopen) this.onopen(new Event("open"));
                this.dispatchEvent(new Event("open"));
              }, 0);
            }
            close() { this.readyState = 2; this.closed = true; }
          }
          window.EventSource = FakeEventSource;
          window.__fakeStreams = streams;
          window.__emitSse = (type, payload) => {
            const es = streams[streams.length - 1];
            if (!es) throw new Error("no fake EventSource");
            es.dispatchEvent(new MessageEvent(type, {
              data: typeof payload === "string" ? payload : JSON.stringify(payload || {}),
            }));
          };
        })();
        """
    )


def _visible_pane_with_text_snapshot(page: Page, text: str):
    return page.evaluate(
        """expected => {
          const panes = Array.from(document.querySelectorAll(".msg-pane"))
            .filter(p => getComputedStyle(p).display !== "none");
          const pane = panes.find(p => p.textContent.includes(expected)) || null;
          // A grid pane also owns persistent compacting / first-token
          // placeholders with the `.msg` class. Only transcript rows carry
          // data-uuid, so count those when asserting keyed history rendering.
          const msgs = pane ? Array.from(pane.querySelectorAll(".msg[data-uuid]")) : [];
          return {
            visiblePaneCount: panes.length,
            msgCount: msgs.length,
            text: pane ? pane.textContent : "",
          };
        }""",
        text,
    )


def _bootstrap_session_for_real_load(page: Page, sid: str, name: str) -> None:
    _app_eval(
        page,
        """
        app.refreshSessions = async () => {};
        app._fetchTabUsage = async () => {};
        app._checkActiveTurn = () => {};
        app._scheduleIdlePreload = () => {};
        app.appReady = true;
        app.availableModels = [{
          model: "e2e-model", label: "E2E model", group: "e2e",
          supports_thinking: true,
        }];
        app.sessions = [{ id: arg.sid, name: arg.name, updated_at: Date.now() / 1000,
          model: "e2e-model", permission: "bypassPermissions", thinking: true }];
        app.openTabIds = [arg.sid];
        app.tabState = {};
        app.currentId = arg.sid;
        app._residentTabIds = [arg.sid];
        app.mobileTab = "chat";
        app.messagesReady = true;
        app.messagesLoading = false;
        app._activateTabState(arg.sid);
        app._promoteResident(arg.sid);
        return true;
        """,
        {"sid": sid, "name": name},
    )


def test_mobile_long_history_switching_does_not_blank(page: Page, backend_url, auth_token):
    """Switch repeatedly between long resident chat panes on a mobile viewport."""
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": 390, "height": 844})
    _login(page, backend_url, auth_token)

    _app_eval(
        page,
        """
        const now = Date.now();
        app.refreshSessions = async () => {};
        app._fetchTabUsage = async () => {};
        const sessionIds = Array.from({ length: 6 }, (_, i) => `perf-history-${i}`);
        app.sessions = sessionIds.map((id, idx) => ({
          id, name: `Perf history ${idx}`, updated_at: now / 1000 - idx,
          model: "e2e-model", permission: "bypassPermissions", thinking: true,
        }));
        app.openTabIds = sessionIds.slice();
        app._MAX_RESIDENT_PANES = 4;
        // Match real boot: mount only the landing pane, then let switches fill
        // the bounded LRU. Mounting four synthetic 90-row panes in one test
        // tick measures fixture setup rather than switch responsiveness.
        app._residentTabIds = [sessionIds[0]];
        app.tabState = {};
        for (const [idx, id] of sessionIds.entries()) {
          const st = app._blankTabState();
          st._loaded = true;
          st.messages = [];
          for (let i = 0; i < 90; i++) {
            st.messages.push({
              role: i % 2 === 0 ? "user" : "assistant",
              text: `history ${idx}:${i} `.repeat(18),
              html: i % 2 === 0 ? "" : `<p>history ${idx}:${i} ${"tail ".repeat(18)}</p>`,
              ts: now + i,
              _k: `${id}-${i}`,
              _noAnim: true,
            });
          }
          app.tabState[id] = st;
        }
        app.currentId = sessionIds[0];
        app.messagesReady = true;
        app.messagesLoading = false;
        app.mobileTab = "chat";
        app._activateTabState(app.currentId);
        app._promoteResident(app.currentId);
        app.$nextTick(() => app.scrollToBottom(true));
        return true;
        """,
    )

    page.wait_for_function(
        """() => {
          const panes = Array.from(document.querySelectorAll(".msg-pane"))
            .filter(p => getComputedStyle(p).display !== "none");
          return panes.length === 1
            && panes[0].querySelectorAll(".msg[data-uuid]").length === 90;
        }""",
        timeout=5000,
    )

    for sid in [f"perf-history-{i}" for i in [1, 2, 3, 4, 5, 0, 5]]:
        _app_eval(
            page,
            """
            app.currentId = arg;
            app.messagesReady = true;
            app.messagesLoading = false;
            app._activateTabState(arg);
            app._promoteResident(arg);
            app.$nextTick(() => app.scrollToBottom(true));
            """,
            sid,
        )
        expected_tail = f"history {sid.rsplit('-', 1)[1]}:89"
        try:
            page.wait_for_function(
                """expected => {
                  const panes = Array.from(document.querySelectorAll(".msg-pane"))
                    .filter(p => getComputedStyle(p).display !== "none");
                  return panes.some(p => p.textContent.includes(expected)
                    && p.querySelectorAll(".msg[data-uuid]").length === 90);
                }""",
                arg=expected_tail,
                timeout=5000,
            )
        except TimeoutError as exc:
            diag = page.evaluate(
                """() => {
                  const app = document.querySelector("#app")._x_dataStack[0];
                  return {
                    currentId: app.currentId,
                    resident: app.residentPaneIds(),
                    openTabIds: app.openTabIds,
                    messagesLength: app.messages.length,
                    visiblePanes: Array.from(document.querySelectorAll(".msg-pane"))
                      .filter(p => getComputedStyle(p).display !== "none")
                      .map(p => ({ count: p.querySelectorAll(".msg[data-uuid]").length,
                                   text: p.textContent.slice(0, 400) })),
                  };
                }"""
            )
            raise AssertionError(f"target tail not visible: {expected_tail}; diag={diag}") from exc
        snap = _visible_pane_with_text_snapshot(page, expected_tail)
        assert snap["msgCount"] == 90
        assert expected_tail in snap["text"]
        assert page.locator(".msg-pane").count() <= 4
        assert _app_eval(page, "return app.residentPaneIds().length;") <= 4

    _assert_no_browser_errors(page, errors)


def test_recent_session_switch_reuses_dom_and_restores_scroll(
    page: Page, backend_url, auth_token
):
    """A warm A → B → A switch keeps keyed DOM and the user's reading spot."""
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": 390, "height": 844})
    _login(page, backend_url, auth_token)

    ids = ["perf-warm-a", "perf-warm-b"]
    _app_eval(
        page,
        """
        const now = Date.now();
        app.refreshSessions = async () => {};
        app._fetchTabUsage = async () => {};
        app._checkActiveTurn = () => {};
        app._scheduleIdlePreload = () => {};
        app.sessions = arg.map((id, idx) => ({
          id, name: `Warm ${idx}`, updated_at: now / 1000 - idx,
          model: "e2e-model", permission: "bypassPermissions", thinking: true,
        }));
        app.openTabIds = arg.slice();
        app.tabState = {};
        for (const [idx, id] of arg.entries()) {
          const st = app._blankTabState();
          st._loaded = true;
          st.messages = Array.from({ length: 36 }, (_, i) => ({
            role: i % 2 === 0 ? "user" : "assistant",
            text: `warm ${idx}:${i} ${"reading-position ".repeat(20)}`,
            html: i % 2 === 0 ? "" : `<p>warm ${idx}:${i} ${"reply ".repeat(24)}</p>`,
            ts: now + i, _k: `${id}-${i}`, _noAnim: true,
          }));
          app.tabState[id] = st;
        }
        app.currentId = arg[0];
        app._residentTabIds = [arg[0]];
        app.messagesReady = true;
        app.messagesLoading = false;
        app.mobileTab = "chat";
        app._activateTabState(arg[0]);
        return true;
        """,
        ids,
    )
    page.wait_for_function(
        """() => document.querySelectorAll(
          '.chat-grid-pane[data-pane-id="perf-warm-a"] .msg[data-uuid]'
        ).length === 36""",
        timeout=10000,
    )

    saved = _app_eval(
        page,
        """
        const body = document.querySelector('.chat-body');
        const max = Math.max(1, body.scrollHeight - body.clientHeight);
        body.scrollTop = Math.floor(max * 0.4);
        app._userScrollIntent(arg);
        app.onChatScroll(arg, { currentTarget: body });
        window.__warmPaneA = document.querySelector(
          `.chat-grid-pane[data-pane-id="${arg}"]`);
        return { top: body.scrollTop, atBottom: app.tabState[arg].atBottom };
        """,
        ids[0],
    )
    assert saved["top"] > 0
    assert saved["atBottom"] is False

    _app_eval(page, "return app.activateTab(arg);", ids[1])
    page.wait_for_function(
        """() => document.querySelector('.chat-body')?.textContent.includes('warm 1:35')""",
        timeout=10000,
    )
    warm_switch_ms = _app_eval(
        page,
        """
        const start = performance.now();
        return app.activateTab(arg).then(() => new Promise(resolve => {
          requestAnimationFrame(() => requestAnimationFrame(
            () => resolve(performance.now() - start)));
        }));
        """,
        ids[0],
    )
    page.wait_for_function(
        """() => document.querySelector('.chat-body')?.textContent.includes('warm 0:35')""",
        timeout=10000,
    )
    # Give any settle loop started by B enough time to prove it no longer owns
    # the shared single-view scroller after the rapid switch back to A.
    page.wait_for_timeout(150)
    restored = _app_eval(
        page,
        """
        const pane = document.querySelector(
          `.chat-grid-pane[data-pane-id="${arg}"]`);
        const body = document.querySelector('.chat-body');
        return {
          sameNode: pane === window.__warmPaneA,
          top: body.scrollTop,
          atBottom: app.tabState[arg].atBottom,
          mounted: app.mountedChatPaneIds().length,
        };
        """,
        ids[0],
    )
    assert restored["sameNode"] is True
    assert restored["atBottom"] is False
    assert abs(restored["top"] - saved["top"]) < 12
    assert restored["mounted"] <= 4
    # Broad CI guard: this is intentionally about catching a multi-second
    # directive-tree rebuild, not micro-benchmarking browser scheduling.
    assert warm_switch_ms < 1000, warm_switch_ms
    scoped_tool_match = _app_eval(
        page,
        """
        return arg.every((sid, idx) => {
          const st = app.tabState[sid];
          const tool = { role: 'tool_use', id: `tool-${idx}`, name: 'WebFetch' };
          const result = { role: 'tool_result', id: `tool-${idx}`, text: 'ok' };
          st.messages.push(tool, result);
          const resultIdx = st.messages.length - 1;
          const expected = st.messages[resultIdx - 1];
          return app.findToolUseFor(st.messages[resultIdx], resultIdx, sid) === expected
            && app.findToolUseFor(st.messages[resultIdx], resultIdx, sid) === expected;
        });
        """,
        ids,
    )
    assert scoped_tool_match is True
    _assert_no_browser_errors(page, errors)


def test_mobile_windowed_load_session_pages_older_history(page: Page, backend_url, auth_token):
    """Drive real loadSession/tail and loadEarlierMessages server paging."""
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": 390, "height": 844})
    sid = "perf-windowed-history"
    messages = _make_mixed_messages(180, "WINDOW_MSG")
    requests = _route_windowed_session(page, sid, messages)
    _login(page, backend_url, auth_token)
    _bootstrap_session_for_real_load(page, sid, "Perf windowed history")

    _app_eval(page, "return app.loadSession(arg);", sid)
    page.wait_for_function(
        """() => {
          const app = document.querySelector("#app")._x_dataStack[0];
          return app.messagesReady === true
            && app.messagesLoading === false
            && app.messages.some(m => (m.text || "").includes("WINDOW_MSG_179"));
        }""",
        timeout=10000,
    )
    expect(page.locator(".msg-pane:visible .msg.assistant:visible").last).to_contain_text(
        "WINDOW_MSG_179", timeout=10000
    )

    state = _app_eval(
        page,
        """
        const st = app._ensureTabState(arg);
        return {
          messages: st.messages.length,
          earlier: st._earlierMessages.length,
          loadedOffset: st._loadedOffset,
          total: st._total,
          hasMore: st._hasMoreHistory,
          resident: app.residentPaneIds().length,
          ready: app.messagesReady,
          bodyText: document.querySelector(".chat-body")?.textContent || "",
        };
        """,
        sid,
    )
    assert requests and requests[0]["tail"] == 75
    assert state["messages"] < 75
    assert state["messages"] <= 75
    assert state["loadedOffset"] == 105
    assert state["total"] == 180
    assert state["hasMore"] is True
    assert state["resident"] <= 4
    assert "WINDOW_MSG_179" in state["bodyText"]
    assert "WINDOW_MSG_000" not in state["bodyText"]
    assert page.locator(".msg-pane").count() <= 4
    assert page.locator(".msg-pane:visible .msg").count() <= 75

    # Drain the tail-local stash, then force the server-backed older window.
    # Mobile intentionally uses a smaller load-more batch to keep each tap
    # responsive, so avoid coupling this regression check to an exact tap count.
    for _ in range(10):
        _app_eval(page, "return app.loadEarlierMessages(arg);", sid)
        page.wait_for_timeout(50)
        if _app_eval(
            page,
            """return app.messages.some(m => (m.text || "").includes("WINDOW_MSG_100"));""",
        ):
            break

    page.wait_for_function(
        """() => {
          const app = document.querySelector("#app")._x_dataStack[0];
          return app.messagesReady === true
            && app.messages.some(m => (m.text || "").includes("WINDOW_MSG_100"));
        }""",
        timeout=10000,
    )
    final_state = _app_eval(
        page,
        """
        const st = app._ensureTabState(arg);
        return {
          messages: st.messages.length,
          earlier: st._earlierMessages.length,
          loadedOffset: st._loadedOffset,
          total: st._total,
          hasMore: st._hasMoreHistory,
          ready: app.messagesReady,
          visibleText: Array.from(document.querySelectorAll(".msg-pane"))
            .filter(p => getComputedStyle(p).display !== "none")
            .map(p => p.textContent).join("\\n"),
          bodyHeight: document.querySelector(".chat-body")?.getBoundingClientRect().height || 0,
        };
        """,
        sid,
    )
    assert any(req["offset"] == 0 and req["limit"] == 105 for req in requests), requests
    assert final_state["loadedOffset"] == 0
    assert final_state["total"] == 180
    assert final_state["ready"] is True
    assert final_state["bodyHeight"] > 100
    assert "WINDOW_MSG_100" in final_state["visibleText"]
    latest_after_load_earlier = _app_eval(
        page,
        """
        return {
          latestInMessages: app.messages.some(m => (m.text || "").includes("WINDOW_MSG_179")),
          latestInDom: document.querySelector(".chat-body")?.textContent.includes("WINDOW_MSG_179"),
          ready: app.messagesReady,
        };
        """,
    )
    assert latest_after_load_earlier == {
        "latestInMessages": True,
        "latestInDom": True,
        "ready": True,
    }
    assert page.locator(".msg-pane").count() <= 4
    assert page.locator(".msg-pane:visible .msg").count() <= 100

    _assert_no_browser_errors(page, errors)


def test_load_session_reconnects_active_turn_and_renders_live_assistant(
    page: Page, backend_url, auth_token
):
    """Real loadSession() calls _checkActiveTurn(), which reconnects SSE live."""
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": 390, "height": 844})
    _install_fake_event_source(page)

    sid = "perf-active-reconnect"
    active_requests: list[str] = []
    ticket_requests: list[dict] = []
    messages = [
        {
            "role": "user",
            "text": "ACTIVE_RECONNECT_USER original prompt still running",
            "ts": 1_700_010_000,
            "uuid": "active-user",
        },
    ]
    _route_windowed_session(page, sid, messages)
    page.route(
        f"**/api/chat/sessions/{sid}/active",
        lambda route: (
            active_requests.append(route.request.url),
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "active": True,
                    "turn_id": "active-reconnect-turn",
                    "started_at": 1_700_010_001,
                    # Deliberately inconsistent with the browser's current
                    # epoch: reconnect timing must use this relative age, not
                    # subtract server started_at from the phone clock.
                    "elapsed_seconds": 7.25,
                }),
            ),
        )[-1],
    )

    def handle_stream_ticket(route):
        try:
            body = route.request.post_data_json
        except Exception:
            body = {}
        ticket_requests.append(body)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ticket": "active-reconnect-ticket"}),
        )

    page.route("**/api/chat/stream/start", handle_stream_ticket)
    _login(page, backend_url, auth_token)

    _app_eval(
        page,
        """
        app.refreshSessions = async () => {};
        app._fetchTabUsage = async () => {};
        app._scheduleIdlePreload = () => {};
        app.appReady = true;
        app.availableModels = [{
          model: "e2e-model", label: "E2E model", group: "e2e",
          supports_thinking: true,
        }];
        app.sessions = [{ id: arg, name: "Perf active reconnect",
          updated_at: Date.now() / 1000, model: "e2e-model",
          permission: "bypassPermissions", thinking: true }];
        app.openTabIds = [arg];
        app.tabState = {};
        app.currentId = arg;
        app._residentTabIds = [arg];
        app.mobileTab = "chat";
        app.messagesReady = true;
        app.messagesLoading = false;
        app._activateTabState(arg);
        app._promoteResident(arg);
        return true;
        """,
        sid,
    )

    _app_eval(page, "return app.loadSession(arg);", sid)
    page.wait_for_function("() => window.__fakeStreams && window.__fakeStreams.length === 1")
    page.wait_for_function(
        """() => {
          const app = document.querySelector("#app")._x_dataStack[0];
          return app.streaming === true && app.messagesReady === true
            && app.messages.some(m => (m.text || "").includes("ACTIVE_RECONNECT_USER"));
        }""",
        timeout=10000,
    )
    assert active_requests, "loadSession did not call /active"
    assert ticket_requests and ticket_requests[-1]["prompt"] == ""
    assert ticket_requests[-1]["session_id"] == sid
    reconnect_timing = _app_eval(
        page,
        """
        const st = app.tabState[arg];
        return { root: app.streamElapsed, pane: st.streamElapsed };
        """,
        sid,
    )
    assert 7 <= reconnect_timing["root"] < 15, reconnect_timing
    assert 7 <= reconnect_timing["pane"] < 15, reconnect_timing

    page.evaluate(
        """() => {
          window.__emitSse("text", { text: "ACTIVE_RECONNECT_LIVE_VISIBLE" });
        }"""
    )
    page.wait_for_function(
        """() => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const body = document.querySelector(".chat-body")?.textContent || "";
          const last = app.messages[app.messages.length - 1];
          return app.streaming === true
            && app.messagesReady === true
            && last && last.role === "assistant"
            && last.text.includes("ACTIVE_RECONNECT_LIVE_VISIBLE")
            && body.includes("ACTIVE_RECONNECT_LIVE_VISIBLE");
        }""",
        timeout=10000,
    )

    page.evaluate(
        """() => {
          window.__emitSse("done", {
            turn_id: "active-reconnect-turn",
            total_cost_usd: 0.001,
            elapsed_ms: 8250,
            completed_at: 1700010010.25,
            session_usage: { context_used_pct: 5, context_used: 500, context_limit: 100000 },
          });
        }"""
    )
    page.wait_for_function(
        """() => document.querySelector("#app")._x_dataStack[0].streaming === false""",
        timeout=10000,
    )
    expect(page.locator(".msg-pane:visible .msg.assistant:visible").last).to_contain_text(
        "ACTIVE_RECONNECT_LIVE_VISIBLE", timeout=5000
    )
    completed_timing = _app_eval(
        page,
        """
        const tail = [...app.tabState[arg].messages]
          .reverse().find(m => m.role !== 'user');
        return { elapsed: tail.elapsed, ts: tail.ts };
        """,
        sid,
    )
    assert abs(completed_timing["elapsed"] - 8.25) < 0.001
    assert completed_timing["ts"] == 1_700_010_010_250
    assert _app_eval(page, "return app.messagesReady === true && !app.messagesLoading;") is True
    _assert_no_browser_errors(page, errors)


def test_open_session_reconcile_catches_revision_arriving_during_refresh(
    page: Page, backend_url, auth_token
):
    """A list revision observed during a quiet load must trigger a catch-up.

    This is the exact "new content appears only after manual reload" race: the
    ETag advances to revision 300 while revision 200 is still loading. A later
    304 has no body, so losing that signal leaves the transcript stale forever.
    """
    page.set_viewport_size({"width": 390, "height": 844})
    _login(page, backend_url, auth_token)
    page.wait_for_function(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const st = app.tabState[app.currentId];
          return st && !st._reconcilePromise && !st.streaming && !st.es;
        }""",
        timeout=10000,
    )
    sid = page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          clearInterval(app._sessionsSyncTimer);
          clearInterval(app._presenceTimer);
          const sid = app.currentId;
          const st = app._ensureTabState(sid);
          st._loaded = true;
          st._seenUpdated = 100;
          st._reconcileTargetUpdated = 100;
          st._pendingExternalUpdate = false;
          st._reconcilePromise = null;
          st.atBottom = false;
          st.messages.splice(0, st.messages.length, {
            role: 'assistant', text: 'AUTO_SYNC_100',
            html: '<p>AUTO_SYNC_100</p>', _k: sid + '-old',
          });
          app.messages = st.messages;
          app.sessions = app.sessions.map(s => s.id === sid
            ? { ...s, active: false, updated_at: 100 }
            : s);
          app.__serverRevision = 100;
          app.__reconcileLoads = [];
          app.__reconcileResolvers = [];
          app.loadSession = async (id) => {
            const revision = app.__serverRevision;
            app.__reconcileLoads.push(revision);
            return await new Promise(resolve => {
              app.__reconcileResolvers.push(() => {
                st.messages.splice(0, st.messages.length, {
                  role: 'assistant', text: 'AUTO_SYNC_' + revision,
                  html: '<p>AUTO_SYNC_' + revision + '</p>',
                  _k: id + '-' + revision,
                });
                st._seenUpdated = revision;
                resolve(true);
              });
            });
          };
          app._checkActiveTurn = async () => {};
          app.__pushRevision = revision => {
            app.__serverRevision = revision;
            app._applySessionList(app.sessions.map(s => s.id === sid
              ? { ...s, active: false, updated_at: revision }
              : s));
          };
          return sid;
        }"""
    )

    page.evaluate(
        """() => document.querySelector('#app')._x_dataStack[0].__pushRevision(200)"""
    )
    page.wait_for_function(
        """() => document.querySelector('#app')._x_dataStack[0]
          .__reconcileLoads.length === 1"""
    )
    page.evaluate(
        """() => document.querySelector('#app')._x_dataStack[0].__pushRevision(300)"""
    )
    page.evaluate(
        """() => document.querySelector('#app')._x_dataStack[0]
          .__reconcileResolvers.shift()()"""
    )
    page.wait_for_function(
        """() => document.querySelector('#app')._x_dataStack[0]
          .__reconcileLoads.length === 2""",
        timeout=5000,
    )
    page.evaluate(
        """() => document.querySelector('#app')._x_dataStack[0]
          .__reconcileResolvers.shift()()"""
    )
    page.wait_for_function(
        """([id]) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const st = app.tabState[id];
          return st && !st._reconcilePromise && !st._pendingExternalUpdate
            && st._seenUpdated === 300
            && st.messages.some(m => m.text === 'AUTO_SYNC_300');
        }""",
        arg=[sid],
        timeout=5000,
    )

    # Simulate iOS dropping the terminal SSE while the server metadata moves
    # active → idle. The stale local transport must be retired and replaced by
    # the canonical revision without a manual page reload.
    page.evaluate(
        """([id]) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const st = app.tabState[id];
          app.__staleClosed = false;
          st.es = { close: () => { app.__staleClosed = true; } };
          st.streaming = true;
          st._streamStartedAt = Date.now() - 10_000;
          app.es = st.es;
          app.streaming = true;
          app.sessions = app.sessions.map(s => s.id === id
            ? { ...s, active: true, updated_at: 300 }
            : s);
          app.__pushRevision(400);
        }""",
        arg=[sid],
    )
    page.wait_for_function(
        """() => document.querySelector('#app')._x_dataStack[0]
          .__reconcileLoads.length === 3""",
        timeout=5000,
    )
    page.evaluate(
        """() => document.querySelector('#app')._x_dataStack[0]
          .__reconcileResolvers.shift()()"""
    )
    page.wait_for_function(
        """([id]) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const st = app.tabState[id];
          return app.__staleClosed && !app.streaming && !st.streaming && !st.es
            && st._seenUpdated === 400
            && st.messages.some(m => m.text === 'AUTO_SYNC_400');
        }""",
        arg=[sid],
        timeout=5000,
    )

    state = page.evaluate(
        """async ([id]) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const realFetch = window.fetch;
          app._sessionListTimeoutMs = 40;
          window.fetch = (url, options = {}) => {
            if (String(url).startsWith('/api/chat/sessions?')) {
              return new Promise((resolve, reject) => {
                const rejectAbort = () => reject(
                  new DOMException('Aborted', 'AbortError'));
                if (options.signal?.aborted) rejectAbort();
                else options.signal?.addEventListener('abort', rejectAbort,
                                                       { once: true });
              });
            }
            return realFetch.call(window, url, options);
          };
          app._sessionListPullPromise = null;
          const started = performance.now();
          const result = await app._pullSessionList(false);
          const elapsed = performance.now() - started;
          const released = app._sessionListPullPromise === null;
          window.fetch = realFetch;
          return {
            id,
            result,
            elapsed,
            released,
            loads: app.__reconcileLoads.slice(),
            text: app.tabState[id].messages[0]?.text,
          };
        }""",
        arg=[sid],
    )
    assert state["loads"] == [200, 300, 400]
    assert state["text"] == "AUTO_SYNC_400"
    assert state["result"] is False
    assert state["released"] is True
    assert state["elapsed"] < 1000


def test_mobile_pwa_tabs_preview_rotation_keep_chat_usable(page: Page, backend_url, auth_token):
    """Mobile files/preview/chat switching and rotation keep long chat usable."""
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": 390, "height": 844})
    sid = "perf-mobile-pwa"
    messages = _make_mixed_messages(170, "PWA_MSG")
    messages[-1] = {
        "role": "assistant",
        "text": "PWA_LATEST_ASSISTANT visible after rotation " + ("tail " * 80),
        "html": "<p>PWA_LATEST_ASSISTANT visible after rotation tail tail tail</p>",
        "ts": 1_700_001_000,
        "uuid": "pwa-latest",
    }
    _route_windowed_session(page, sid, messages)
    def handle_preview_read(route):
        qs = parse_qs(urlparse(route.request.url).query)
        if qs.get("path", [""])[0] != "reports/perf-preview.md":
            route.continue_()
            return
        route.fulfill(
            status=200,
            content_type="text/markdown",
            body="# Perf preview\n\nThis markdown file is opened through real openFile().\n\n"
                 + "\n".join(f"- preview line {i}" for i in range(40)),
        )

    page.route("**/api/files/read?*", handle_preview_read)
    _login(page, backend_url, auth_token)
    _bootstrap_session_for_real_load(page, sid, "Perf mobile PWA")
    _app_eval(page, "return app.loadSession(arg);", sid)
    page.wait_for_function(
        """() => {
          const app = document.querySelector("#app")._x_dataStack[0];
          return app.messagesReady === true
            && document.body.textContent.includes("PWA_LATEST_ASSISTANT");
        }""",
        timeout=10000,
    )
    _app_eval(
        page,
        """
        app.__resumeCounts = { health: 0, sessions: 0 };
        app._pingHealth = async () => { app.__resumeCounts.health += 1; };
        app.refreshSessions = async () => { app.__resumeCounts.sessions += 1; };
        return true;
        """,
    )

    _app_eval(
        page,
        """
        return app.openFile({
          path: "reports/perf-preview.md",
          name: "perf-preview.md",
          is_dir: false,
        }, { preview: false });
        """,
    )
    page.wait_for_function(
        """() => {
          const app = document.querySelector("#app")._x_dataStack[0];
          return app.mobileTab === "preview"
            && app.previewMode === "md"
            && app.rawText.includes("Perf preview")
            && document.body.textContent.includes("Perf preview");
        }""",
        timeout=10000,
    )

    page.locator(SEL_MOBILE_TAB).nth(0).click()
    page.wait_for_function(
        """() => document.querySelector("#app")._x_dataStack[0].mobileTab === "files" """,
        timeout=5000,
    )
    page.locator(SEL_MOBILE_TAB).nth(1).click()
    page.wait_for_function(
        """() => document.querySelector("#app")._x_dataStack[0].mobileTab === "preview" """,
        timeout=5000,
    )
    page.set_viewport_size({"width": 844, "height": 390})
    page.wait_for_timeout(150)
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_timeout(150)
    page.evaluate(
        """() => {
          Object.defineProperty(document, "visibilityState", {
            value: "hidden", configurable: true,
          });
          document.dispatchEvent(new Event("visibilitychange"));
          Object.defineProperty(document, "visibilityState", {
            value: "visible", configurable: true,
          });
          document.dispatchEvent(new Event("visibilitychange"));
          window.dispatchEvent(new Event("focus"));
        }"""
    )
    page.wait_for_function(
        """() => {
          const c = document.querySelector("#app")._x_dataStack[0].__resumeCounts;
          return c && c.health >= 1 && c.sessions >= 1;
        }""",
        timeout=5000,
    )
    page.locator(SEL_MOBILE_TAB).nth(2).click()
    page.wait_for_function(
        """() => document.querySelector("#app")._x_dataStack[0].mobileTab === "chat" """,
        timeout=5000,
    )
    _app_eval(page, "app.scrollToBottom(true); return true;")
    page.wait_for_function(
        """() => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const body = document.querySelector(".chat-body");
          return app.messagesReady === true
            && body && body.textContent.includes("PWA_LATEST_ASSISTANT")
            && Math.abs((body.scrollHeight - body.clientHeight) - body.scrollTop) < 48;
        }""",
        timeout=10000,
    )

    layout = page.evaluate(
        """() => {
          const input = document.querySelector(".chat-input-textarea");
          const toolbar = document.querySelector(".chat-toolbar");
          const latest = Array.from(document.querySelectorAll(".msg-pane"))
            .filter(p => getComputedStyle(p).display !== "none")
            .flatMap(p => Array.from(p.querySelectorAll(".msg.assistant")))
            .find(el => el.textContent.includes("PWA_LATEST_ASSISTANT"));
          const rect = el => {
            const r = el.getBoundingClientRect();
            return { top: r.top, bottom: r.bottom, left: r.left, right: r.right,
                     width: r.width, height: r.height };
          };
          return {
            ready: document.querySelector("#app")._x_dataStack[0].messagesReady,
            mobileTab: document.querySelector("#app")._x_dataStack[0].mobileTab,
            input: rect(input),
            toolbar: rect(toolbar),
            latest: rect(latest),
            viewport: { width: innerWidth, height: innerHeight },
          };
        }"""
    )
    assert layout["ready"] is True
    assert layout["mobileTab"] == "chat"
    resume_counts = _app_eval(page, "return app.__resumeCounts;")
    assert resume_counts["health"] >= 1
    assert resume_counts["sessions"] >= 1
    for key in ("input", "toolbar"):
        box = layout[key]
        assert box["height"] > 0
        assert 0 <= box["top"] < layout["viewport"]["height"]
        assert 0 < box["bottom"] <= layout["viewport"]["height"]
        assert 0 <= box["left"] < layout["viewport"]["width"]
        assert 0 < box["right"] <= layout["viewport"]["width"]
    assert layout["input"]["bottom"] <= layout["toolbar"]["top"] + 2
    assert layout["latest"]["height"] > 0
    assert page.locator(".msg-pane").count() <= 4
    assert _app_eval(page, "return app.messagesReady === true && !app.messagesLoading;") is True

    _assert_no_browser_errors(page, errors)


def test_mobile_keyboard_close_without_viewport_event_restores_full_layout(
    page: Page, backend_url, auth_token
):
    """A dropped iOS visualViewport close event must not leave a bottom band.

    The textarea deliberately stays focused, matching Enter-to-send in the
    installed PWA. We change the live viewport height back without dispatching
    resize/scroll; the keyboard-only watchdog must observe it, clear the stale
    inset, restore the tab bar, and reset any root viewport pan.
    """
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": 390, "height": 844})
    _login(page, backend_url, auth_token)
    _app_eval(page, "app.mobileTab = 'chat'; return true;")

    page.evaluate(
        """() => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const input = document.querySelector(".chat-input-textarea");
          // The isolated E2E backend may have no model catalog, which leaves
          // the real composer disabled. Enable this DOM control so the test
          // can exercise the focus-within keyboard layout itself.
          input.disabled = false;
          input.focus();
          const vv = window.visualViewport;
          window.__testVvHeight = innerHeight - 140;
          window.__composerScrollIntoViewCalls = 0;
          input.scrollIntoView = () => { window.__composerScrollIntoViewCalls += 1; };
          Object.defineProperty(vv, "height", {
            configurable: true,
            get: () => window.__testVvHeight,
          });
          window.__nativeScrollTo = window.scrollTo;
          window.__rootResetCalls = 0;
          window.scrollTo = (...args) => {
            window.__rootResetCalls += 1;
            return window.__nativeScrollTo.apply(window, args);
          };
          app._syncMobileKeyboardViewport();
        }"""
    )
    page.wait_for_function(
        """() => {
          const layout = document.querySelector(".layout").getBoundingClientRect();
          return document.activeElement === document.querySelector(".chat-input-textarea")
            && document.body.classList.contains("kb-open")
            && layout.bottom <= innerHeight - 130;
        }""",
        timeout=2000,
    )
    state = page.evaluate(
        """() => {
          const input = document.querySelector(".chat-input-textarea");
          const layout = document.querySelector(".layout").getBoundingClientRect();
          return {
            focused: document.activeElement === input,
            bottom: layout.bottom,
            viewport: innerHeight,
            kbOpen: document.body.classList.contains("kb-open"),
          };
        }"""
    )
    assert state["focused"] is True
    assert state["kbOpen"] is True
    assert state["bottom"] <= state["viewport"] - 130

    # The geometry becomes current, but Safari drops the corresponding event.
    page.evaluate("() => { window.__testVvHeight = innerHeight; }")
    page.wait_for_function(
        """() => {
          const layout = document.querySelector(".layout").getBoundingClientRect();
          const tab = document.querySelector(".mobile-tab-bar");
          const tabRect = tab.getBoundingClientRect();
          return document.activeElement === document.querySelector(".chat-input-textarea")
            && !document.body.classList.contains("kb-open")
            && getComputedStyle(document.documentElement)
                 .getPropertyValue("--kb-inset").trim() === "0px"
            && Math.abs(layout.top) < 2
            && Math.abs(layout.bottom - innerHeight) < 2
            && getComputedStyle(tab).display === "flex"
            && Math.abs(tabRect.bottom - innerHeight) < 2
            && window.__rootResetCalls > 0;
        }""",
        timeout=3000,
    )
    assert page.evaluate("() => window.__composerScrollIntoViewCalls") == 0
    page.evaluate(
        """() => {
          delete window.visualViewport.height;
          window.scrollTo = window.__nativeScrollTo;
          delete window.__nativeScrollTo;
          delete window.__testVvHeight;
        }"""
    )
    _assert_no_browser_errors(page, errors)


def test_mobile_first_token_pending_stays_beside_prompt(
    page: Page, backend_url, auth_token
):
    """The pending assistant row stays in-pane instead of after a full-height grid."""
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": 390, "height": 844})
    _install_fake_event_source(page)
    page.route(
        "**/api/chat/stream/start",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"ticket":"first-token-ticket"}',
        ),
    )
    _login(page, backend_url, auth_token)

    sid = "perf-first-token"
    _app_eval(
        page,
        """
        const now = Date.now();
        app.refreshSessions = async () => {};
        app._fetchTabUsage = async () => {};
        app.availableModels = [{
          model: "e2e-model", label: "E2E model", group: "e2e",
          supports_thinking: true,
        }];
        app.model = "e2e-model";
        app.defaultModel = "e2e-model";
        app.sessions = [{ id: arg, name: "First token", updated_at: now / 1000,
          model: "e2e-model", permission: "bypassPermissions", thinking: true }];
        app.openTabIds = [arg];
        app.tabState = {};
        app.tabState[arg] = app._blankTabState();
        app.currentId = arg;
        app._residentTabIds = [arg];
        app._activateTabState(arg);
        app.messagesReady = true;
        app.messagesLoading = false;
        app.mobileTab = "chat";
        app.input = "FIRST_TOKEN_PENDING_PROMPT";
        app.atBottom = true;
        return true;
        """,
        sid,
    )
    _app_eval(page, "app.send(); return true;")
    page.wait_for_function(
        """() => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const pane = Array.from(document.querySelectorAll('.chat-grid-pane'))
            .find(el => getComputedStyle(el).display !== 'none');
          const pending = pane?.querySelector('.first-token-pending');
          return app.streaming === true && pending
            && getComputedStyle(pending).display !== "none";
        }""",
        timeout=10000,
    )
    page.wait_for_timeout(250)

    layout = page.evaluate(
        """() => {
          const pane = Array.from(document.querySelectorAll('.chat-grid-pane'))
            .find(el => getComputedStyle(el).display !== 'none');
          const msgPane = pane.querySelector('.msg-pane');
          const prompt = Array.from(msgPane.querySelectorAll('.msg.user'))
            .find(el => el.textContent.includes('FIRST_TOKEN_PENDING_PROMPT'));
          const pending = msgPane.querySelector('.first-token-pending');
          const promptRect = prompt.getBoundingClientRect();
          const pendingRect = pending.getBoundingClientRect();
          return {
            gap: pendingRect.top - promptRect.bottom,
            pendingInPane: pending.closest('.msg-pane') === msgPane,
            directBodyPending: document.querySelectorAll(
              '.chat-body > .first-token-pending').length,
          };
        }"""
    )
    assert layout["pendingInPane"] is True
    assert layout["directBodyPending"] == 0
    assert 0 <= layout["gap"] <= 32, layout

    page.evaluate(
        """() => window.__emitSse("done", {
          total_cost_usd: 0,
          session_usage: { context_used_pct: 0, context_used: 0, context_limit: 100000 },
        })"""
    )
    page.wait_for_function(
        "() => document.querySelector('#app')._x_dataStack[0].streaming === false",
        timeout=10000,
    )
    _assert_no_browser_errors(page, errors)


def test_120kb_mixed_sse_stream_renders_final_assistant_html(
    page: Page, backend_url, auth_token
):
    """Drive the real send()/SSE handlers with a long mixed event stream."""
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": 390, "height": 844})
    _install_fake_event_source(page)
    page.route(
        "**/api/chat/stream/start",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"ticket":"e2e-ticket"}',
        ),
    )
    _login(page, backend_url, auth_token)

    _app_eval(
        page,
        """
        const sid = "perf-stream";
        const now = Date.now();
        app.refreshSessions = async () => {};
        app._fetchTabUsage = async () => {};
        app.availableModels = [{
          model: "e2e-model", label: "E2E model", group: "e2e",
          supports_thinking: true,
        }];
        app.model = "e2e-model";
        app.defaultModel = "e2e-model";
        app.sessions = [{ id: sid, name: "Perf stream", updated_at: now / 1000,
          model: "e2e-model", permission: "bypassPermissions", thinking: true }];
        app.openTabIds = [sid];
        app.tabState = {};
        app.tabState[sid] = app._blankTabState();
        app.currentId = sid;
        app._residentTabIds = [sid];
        app._activateTabState(sid);
        app.messagesReady = true;
        app.messagesLoading = false;
        app.mobileTab = "chat";
        app.input = "stream a long deterministic answer";
        app.atBottom = true;
        return true;
        """,
    )
    _app_eval(page, "app.send(); return true;")
    page.wait_for_function("() => window.__fakeStreams && window.__fakeStreams.length === 1")

    _app_eval(
        page,
        """
        app.__streamScrollCalls = 0;
        const originalScrollToBottom = app.scrollToBottom.bind(app);
        app.scrollToBottom = (...args) => {
          app.__streamScrollCalls += 1;
          return originalScrollToBottom(...args);
        };
        return true;
        """,
    )
    page.evaluate(
        """() => {
          for (let i = 0; i < 120; i++) {
            window.__emitSse("thinking", { text: `burst-${i} ` });
          }
        }"""
    )
    page.wait_for_timeout(180)
    scroll_calls = _app_eval(page, "return app.__streamScrollCalls;")
    assert scroll_calls <= 4, f"thinking burst scheduled {scroll_calls} layout scrolls"

    page.evaluate(
        """() => {
          window.__emitSse("thinking", { text: "planning ".repeat(80) });
          window.__emitSse("tool_use", {
            id: "toolu_perf_1", name: "Bash", summary: "generate fixture",
            input: { command: "printf long-stream" },
          });
          window.__emitSse("tool_result", {
            id: "toolu_perf_1", tool_name: "Bash", preview: "ok",
            text: "result ".repeat(300), truncated: false, is_error: false,
            bash: { stdout: "ok", stderr: "", exit_code: 0 },
          });
          window.__emitSse("text", { text: "MID_STREAM_VISIBLE_1 " + "alpha ".repeat(80) });
        }"""
    )
    page.wait_for_function(
        """() => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const body = document.querySelector(".chat-body")?.textContent || "";
          const last = app.messages[app.messages.length - 1];
          return app.streaming === true
            && last && last.role === "assistant"
            && last.html.includes("MID_STREAM_VISIBLE_1")
            && body.includes("MID_STREAM_VISIBLE_1");
        }""",
        timeout=10000,
    )
    mid_1 = _app_eval(
        page,
        """
        const last = app.messages[app.messages.length - 1];
        return {
          streaming: app.streaming,
          textLength: last.text.length,
          htmlLength: last.html.length,
        };
        """,
    )
    assert mid_1["streaming"] is True

    page.evaluate(
        """() => {
          window.__emitSse("text", { text: "MID_STREAM_VISIBLE_2 " + "beta ".repeat(120) });
        }"""
    )
    page.wait_for_function(
        """prev => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const body = document.querySelector(".chat-body")?.textContent || "";
          const last = app.messages[app.messages.length - 1];
          return app.streaming === true
            && last && last.role === "assistant"
            && last.text.length > prev.textLength
            && last.html.length >= prev.htmlLength
            && last.html.includes("MID_STREAM_VISIBLE_2")
            && body.includes("MID_STREAM_VISIBLE_2");
        }""",
        arg=mid_1,
        timeout=10000,
    )

    page.evaluate(
        """() => {
          const finalText = "FINAL_ASSISTANT_HTML_COMPLETE " + "long-stream-token ".repeat(7200);
          window.__emitSse("thinking", { text: "checking ".repeat(60) });
          window.__emitSse("tool_use", {
            id: "toolu_perf_2", name: "Read", summary: "inspect file",
            input: { file_path: "fixture.txt" },
          });
          window.__emitSse("tool_result", {
            id: "toolu_perf_2", tool_name: "Read", preview: "line 1",
            text: "1: fixture\\n".repeat(1000), truncated: false, is_error: false,
          });
          window.__emitSse("text", { text: "second assistant segment before todos. " });
          window.__emitSse("tool_use", {
            id: "toolu_perf_3", name: "TodoWrite", summary: "update plan",
            todos: [
              { content: "stream", status: "completed" },
              { content: "render", status: "in_progress" },
            ],
          });
          window.__emitSse("tool_result", {
            id: "toolu_perf_3", tool_name: "TodoWrite", preview: "updated",
            text: "todos updated", truncated: false, is_error: false,
          });
          window.__emitSse("text", { text: finalText });
          window.__emitSse("done", {
            total_cost_usd: 0.001,
            session_usage: { context_used_pct: 10, context_used: 1000, context_limit: 100000 },
          });
        }"""
    )

    page.wait_for_function(
        """() => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const last = app.messages[app.messages.length - 1];
          return app.streaming === false
            && last && last.role === "assistant"
            && last.text.length >= 120000
            && last.text.includes("FINAL_ASSISTANT_HTML_COMPLETE")
            && last.html.includes("FINAL_ASSISTANT_HTML_COMPLETE");
        }""",
        timeout=10000,
    )
    expect(page.locator(".msg-pane:visible .msg.assistant:visible").last).to_contain_text(
        "FINAL_ASSISTANT_HTML_COMPLETE", timeout=5000
    )
    assert page.locator(".msg-pane:visible .msg").count() <= 50
    assert _app_eval(page, "return app.messages.length;") <= 50
    assert _app_eval(
        page,
        """
        const roles = app.messages.map(m => m.role);
        const last = app.messages[app.messages.length - 1];
        return roles.includes("thinking")
          && roles.includes("tool_use")
          && roles.includes("tool_result")
          && last.role === "assistant"
          && last.text.length >= 120000
          && last.html.length > 0;
        """,
    )

    _assert_no_browser_errors(page, errors)
