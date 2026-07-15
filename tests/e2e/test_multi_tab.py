"""Browser-level smoke tests for the multi-tab chat UI.

These cover the regression classes that bit us during the 2026-05-17
multi-tab sprint and can ONLY be caught in a real browser:
- DOM event wiring (click, drag, contextmenu)
- Alpine x-effect / x-show / x-if reactivity races
- localStorage round-tripping (preview path, open tabs)
- document.title responding to streaming + session changes

Skipped by default. Enable with `RUN_E2E=1`. See tests/e2e/README.md."""
from __future__ import annotations
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api",
                    reason="install with: uv add --group dev pytest-playwright")
from playwright.sync_api import Page, expect  # noqa: E402


# Selectors mirror frontend/index.html. Centralised so a UI rename only
# breaks one place.
SEL_LOGIN = ".login"
SEL_LOGIN_INPUT = '.login input[type="password"]'
SEL_TABS = ".chat-tabs-list"
SEL_TAB = ".chat-tab"
SEL_TAB_ACTIVE = ".chat-tab.active"
SEL_TAB_NAME = ".chat-tab-name"
SEL_TAB_RENAME = ".chat-tab-rename-input"
SEL_TAB_CLOSE = ".chat-tab-close"
SEL_TAB_NEW = ".chat-tab-new"
SEL_SESSION_PANE = ".chat-session-pane:visible"


def _login(page: Page, base: str, token: str) -> None:
    page.goto(base)
    # Wait for either the login screen or (if a token is already stored)
    # the tab strip to appear.
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
    # A brand-new isolated backend first exposes an optimistic draft id while
    # Codex thread/start is still running. Most UI actions tolerate that, but
    # state/race tests need a stable native id or the adoption step can replace
    # the metadata they just configured underneath them.
    page.wait_for_function(
        """() => {
          const app = document.querySelector("#app")?._x_dataStack?.[0];
          return app && !app._creatingSession
            && !String(app.currentId || '').startsWith('draft-')
            && !app._sessionCreatePromises[app.currentId];
        }"""
    )


def test_new_and_switch_and_close_tabs(page: Page, backend_url, auth_token):
    """Open multiple chat tabs, switch between them, close one — verify the
    bar reflects each operation and no tab is silently lost."""
    _login(page, backend_url, auth_token)
    initial = page.locator(SEL_TAB).count()

    page.locator(SEL_TAB_NEW).click()
    expect(page.locator(SEL_TAB)).to_have_count(initial + 1)

    page.locator(SEL_TAB_NEW).click()
    expect(page.locator(SEL_TAB)).to_have_count(initial + 2)

    # Switch to the first tab.
    page.locator(SEL_TAB).first.click()
    expect(page.locator(SEL_TAB_ACTIVE)).to_have_count(1)

    # Close the active tab via its × button.
    page.locator(f"{SEL_TAB_ACTIVE} {SEL_TAB_CLOSE}").click()
    expect(page.locator(SEL_TAB)).to_have_count(initial + 1)


def test_effort_override_survives_list_poll_and_page_reload(
        page: Page, backend_url, auth_token):
    """A pending next-turn effort must not fall back to auto on refresh.

    Codex accepts the resume config but omits it from stable thread/list and
    thread/read payloads. This reproduces the real UI sequence: choose an
    effort, let the normal session poll run, then recreate the whole Alpine app.
    """
    _login(page, backend_url, auth_token)
    page.wait_for_function(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return app._supportsEffort(app.model)
            && app.effortChoices(app.model).some(opt => opt.value === 'medium');
        }""",
        timeout=5000,
    )

    effort = page.locator(".chat-toolbar-effort")
    expect(effort).to_be_visible()
    effort.select_option("medium")
    page.wait_for_function(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const session = app.sessions.find(s => s.id === app.currentId);
          return app.effort === 'medium' && session?.effort === 'medium';
        }""",
        timeout=5000,
    )

    page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          await app.refreshSessions();
        }"""
    )
    assert effort.input_value() == "medium"

    # Recreate frontend state; localStorage keeps only auth/preferences, so the
    # selected effort must come back from the server-side session metadata.
    _login(page, backend_url, auth_token)
    page.wait_for_function(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const session = app.sessions.find(s => s.id === app.currentId);
          return app.effort === 'medium' && session?.effort === 'medium';
        }""",
        timeout=5000,
    )
    expect(page.locator(".chat-toolbar-effort")).to_have_value("medium")


def test_fast_mode_survives_list_poll_and_page_reload(
        page: Page, backend_url, auth_token):
    """Fast is session metadata and must not regress during list reconciliation."""
    _login(page, backend_url, auth_token)
    supports_fast = page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return app._supportsFast(app.model);
        }"""
    )

    toggle = page.locator(".chat-toolbar-fast input")
    if not supports_fast:
        # Accounts/models without a catalog-advertised tier must never receive
        # a dead control. The persistence branch runs on Fast-enabled fixtures.
        expect(page.locator(".chat-toolbar-fast")).to_be_hidden()
        return
    expect(toggle).to_be_visible()
    toggle.check()
    page.wait_for_function(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const session = app.sessions.find(s => s.id === app.currentId);
          return app.fastModeEnabled && session?.service_tier === 'priority';
        }""",
        timeout=5000,
    )

    page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          await app.refreshSessions();
        }"""
    )
    expect(toggle).to_be_checked()

    _login(page, backend_url, auth_token)
    page.wait_for_function(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const session = app.sessions.find(s => s.id === app.currentId);
          return app.fastModeEnabled && session?.service_tier === 'priority';
        }""",
        timeout=5000,
    )
    expect(page.locator(".chat-toolbar-fast input")).to_be_checked()


def test_mobile_fast_mode_lives_in_settings_popover_without_toolbar_overflow(
        browser, backend_url, auth_token):
    context = browser.new_context(
        viewport={"width": 390, "height": 844},
        has_touch=True,
        is_mobile=True,
    )
    page = context.new_page()
    try:
        def advertise_fast(route):
            response = route.fetch()
            payload = response.json()
            for model in payload.get("models", []):
                model["supports_fast"] = True
                model["fast_service_tier"] = "priority"
                model["service_tiers"] = [{
                    "id": "priority", "name": "Fast", "description": "",
                }]
            route.fulfill(response=response, json=payload)

        page.route("**/api/chat/providers", advertise_fast)
        _login(page, backend_url, auth_token)
        page.wait_for_function(
            """() => {
              const app = document.querySelector('#app')._x_dataStack[0];
              return app._supportsFast(app.model);
            }"""
        )

        expect(page.locator(".chat-toolbar-fast")).to_be_hidden()
        gear = page.locator(".chat-toolbar-more-btn")
        expect(gear).to_be_visible()
        toolbar_size = page.locator(".chat-toolbar").evaluate(
            """el => ({
              scrollWidth: el.scrollWidth,
              clientWidth: el.clientWidth,
              children: [...el.children].map(child => ({
                className: child.className,
                display: getComputedStyle(child).display,
                width: child.getBoundingClientRect().width,
              })),
            })"""
        )
        assert toolbar_size["scrollWidth"] <= toolbar_size["clientWidth"] + 1, toolbar_size
        gear.click()
        expect(page.locator(".chat-toolbar-more-toggle").first).to_be_visible()
        expect(page.locator(".chat-toolbar-queue")).to_be_visible()
    finally:
        context.close()


def test_mobile_new_chat_keeps_entire_empty_state_scrollable(
        browser, backend_url, auth_token):
    """Tall Muse suggestions must grow downward, never overflow above scrollTop 0."""
    context = browser.new_context(
        viewport={"width": 390, "height": 844},
        has_touch=True,
        is_mobile=True,
    )
    page = context.new_page()
    try:
        _login(page, backend_url, auth_token)
        page.evaluate(
            """async () => {
              const app = document.querySelector('#app')._x_dataStack[0];
              await app.newSession();
              app.contextInfo = {
                ...app.contextInfo,
                _fetched: true,
                has_any_provider: true,
              };
            }"""
        )
        empty = page.locator(".chat-empty:visible")
        expect(empty).to_be_visible()
        cells = empty.locator(".muse-cell")
        expect(cells).to_have_count(9)
        page.locator(".chat-body").evaluate("el => { el.scrollTop = 0; }")
        geometry = page.evaluate(
            """() => {
              const body = document.querySelector('.chat-body');
              const empty = body.querySelector('.chat-empty:not([style*="display: none"])');
              const first = empty.querySelector('.muse-cell');
              const last = empty.querySelector('.muse-cell:last-child');
              const bodyBox = body.getBoundingClientRect();
              const firstBox = first.getBoundingClientRect();
              body.scrollTop = body.scrollHeight;
              const lastBox = last.getBoundingClientRect();
              const emptyBox = empty.getBoundingClientRect();
              return {
                firstTop: firstBox.top,
                viewportTop: bodyBox.top,
                lastBottom: lastBox.bottom,
                emptyBottom: emptyBox.bottom,
                viewportBottom: bodyBox.bottom,
                scrollHeight: body.scrollHeight,
                clientHeight: body.clientHeight,
              };
            }"""
        )
        assert geometry["firstTop"] >= geometry["viewportTop"] - 1, geometry
        assert geometry["lastBottom"] <= geometry["viewportBottom"] + 1, geometry
        # At scroll-bottom, the empty-state's own bottom should be just above
        # the chat body's padding edge. A second min-height:100% sibling used
        # to leave nearly a full viewport of blank space after the Muse cards.
        trailing_blank = geometry["viewportBottom"] - geometry["emptyBottom"]
        assert 0 <= trailing_blank <= 32, geometry
        assert geometry["scrollHeight"] > geometry["clientHeight"], geometry
    finally:
        context.close()


def test_cancelled_incompatible_model_switch_keeps_source_effort(
        page: Page, backend_url, auth_token):
    """Cancelling a fork must not PATCH the source session to auto."""
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const sid = app.currentId;
          const session = app.sessions.find(s => s.id === sid);
          const oldModel = session.model || app.model || 'source-model';
          session.model = oldModel;
          session.effort = 'max';
          session.message_count = 2;
          const st = app._ensureTabState(sid);
          st.messages.splice(0, st.messages.length,
            { role: 'user', text: 'question' },
            { role: 'assistant', text: 'answer' });
          app.effort = 'max';
          const target = 'e2e-low-only';
          app.availableModels.push({
            model: target, label: 'Low only', group: 'E2E',
            supports_effort: true, reasoning_efforts: ['low'],
          });
          const realFetch = window.fetch;
          let effortPatches = 0;
          window.fetch = (url, init = {}) => {
            if (String(url).includes(`/api/chat/sessions/${sid}`)
                && init.method === 'PATCH'
                && String(init.body || '').includes('effort')) {
              effortPatches += 1;
              return Promise.resolve(new Response('{}', {
                status: 200, headers: { 'Content-Type': 'application/json' },
              }));
            }
            return realFetch(url, init);
          };
          const realConfirm = app.confirm;
          app.confirm = async () => false;
          app.model = target;
          await app.onModelChange();
          app.confirm = realConfirm;
          window.fetch = realFetch;
          return {
            effortPatches,
            rootModel: app.model,
            rootEffort: app.effort,
            sessionModel: session.model,
            sessionEffort: session.effort,
            oldModel,
          };
        }"""
    )
    assert result["effortPatches"] == 0
    assert result["rootModel"] == result["oldModel"]
    assert result["sessionModel"] == result["oldModel"]
    assert result["rootEffort"] == "max"
    assert result["sessionEffort"] == "max"


def test_late_session_setting_and_context_responses_stay_with_owner(
        page: Page, backend_url, auth_token):
    """Slow A responses must never rewrite focused session B."""
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const a = app.currentId;
          const b = 'async-owner-b';
          const metaA = app.sessions.find(s => s.id === a);
          metaA.effort = 'high';
          app.sessions.push({ id: b, name: 'B', model: metaA.model,
            effort: 'low', permission: 'default', active: false,
            updated_at: Date.now() / 1000 });
          const stA = app._ensureTabState(a);
          const stB = app._ensureTabState(b);
          stA._loaded = true; stB._loaded = true;
          Object.assign(stA.sessionUsage, { context_used: 1, context_limit: 100 });
          Object.assign(stB.sessionUsage, { context_used: 22, context_limit: 200 });

          const realFetch = window.fetch;
          let resolveEffort;
          window.fetch = (url, init = {}) => {
            if (String(url).endsWith(`/api/chat/sessions/${a}`)
                && init.method === 'PATCH') {
              return new Promise(resolve => { resolveEffort = resolve; });
            }
            return realFetch(url, init);
          };
          app.effort = 'medium';
          const effortDone = app.onEffortChange();
          while (!resolveEffort) await new Promise(r => setTimeout(r, 0));
          app.currentId = b;
          app._activateTabState(b);
          app.effort = 'low';
          resolveEffort(new Response('{}', { status: 200 }));
          await effortDone;

          const realApi = app.api;
          let resolveContext;
          app.currentId = a;
          app._activateTabState(a);
          app.api = () => new Promise(resolve => { resolveContext = resolve; });
          const contextDone = app._refreshCtxMeter(a);
          while (!resolveContext) await new Promise(r => setTimeout(r, 0));
          app.currentId = b;
          app._activateTabState(b);
          app.effort = 'low';
          resolveContext({ ok: true, data: { totalTokens: 77, maxTokens: 100 } });
          await contextDone;
          app.api = realApi;
          window.fetch = realFetch;
          return {
            aEffort: metaA.effort,
            bEffort: app.sessions.find(s => s.id === b).effort,
            rootEffort: app.effort,
            aUsed: stA.sessionUsage.context_used,
            bUsed: stB.sessionUsage.context_used,
            rootUsed: app.sessionUsage.context_used,
          };
        }"""
    )
    assert result == {
        "aEffort": "medium",
        "bEffort": "low",
        "rootEffort": "low",
        "aUsed": 77,
        "bUsed": 22,
        "rootUsed": 22,
    }


def test_model_switch_is_single_flight_and_locks_session_controls(
        page: Page, backend_url, auth_token):
    """A native select double-change must not start two model transitions."""
    _login(page, backend_url, auth_token)
    targets = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          // Boot deliberately fetches the model catalog off the first-paint
          // path, and fetchStats may request it again after 300 ms. Let any
          // current request settle, then suppress later background refreshes
          // while this test owns availableModels; otherwise a fast CI runner
          // can replace the synthetic catalog halfway through the assertion.
          while (app._modelsFetchPromise) await app._modelsFetchPromise;
          const realFetchModels = app._fetchModels;
          app._fetchModels = async () => true;
          const sid = app.currentId;
          const session = app.sessions.find(s => s.id === sid);
          const st = app._ensureTabState(sid);
          st.messages.splice(0, st.messages.length);
          session.message_count = 0;
          session.active = false;
          session.model = 'e2e-source';
          app.model = 'e2e-source';
          app.availableModels = [
            { model: 'e2e-source', label: 'Source', group: 'E2E' },
            { model: 'e2e-target-a', label: 'Target A', group: 'E2E' },
            { model: 'e2e-target-b', label: 'Target B', group: 'E2E' },
          ];
          const realFetch = window.fetch;
          const realRefresh = app.refreshSessions;
          window.__modelPatchCalls = 0;
          window.fetch = (url, init = {}) => {
            if (String(url).endsWith(`/api/chat/sessions/${sid}`)
                && init.method === 'PATCH') {
              window.__modelPatchCalls += 1;
              return new Promise(resolve => { window.__resolveModelPatch = resolve; });
            }
            return realFetch(url, init);
          };
          app.refreshSessions = async () => true;
          window.__restoreModelTest = () => {
            window.fetch = realFetch;
            app.refreshSessions = realRefresh;
            app._fetchModels = realFetchModels;
          };
          app.model = 'e2e-target-a';
          window.__firstModelChange = app.onModelChange();
          return { sid };
        }"""
    )
    page.wait_for_function("() => typeof window.__resolveModelPatch === 'function'")
    expect(page.locator('select[x-model="model"]')).to_be_disabled()
    expect(page.locator(".chat-toolbar-effort")).to_be_disabled()
    stale_guard = page.evaluate(
        """([sid]) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const stale = app.sessions.map(s => s.id === sid
            ? { ...s, model: 'e2e-source', model_provider: '' }
            : s);
          app._applySessionList(stale);
          const detail = app._retainExpectedSessionSettings({
            ...app.sessions.find(s => s.id === sid),
            model: 'e2e-source', model_provider: '',
          });
          return {
            root: app.model,
            meta: app.sessions.find(s => s.id === sid).model,
            detail: detail.model,
          };
        }""",
        arg=[targets["sid"]],
    )
    assert stale_guard == {
        "root": "e2e-target-a",
        "meta": "e2e-target-a",
        "detail": "e2e-target-a",
    }

    pending_target = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          app.model = 'e2e-target-b';
          await app.onModelChange();
          return app.model;
        }"""
    )
    assert pending_target == "e2e-target-a"

    result = page.evaluate(
        """async ([sid]) => {
          window.__resolveModelPatch(new Response('{}', { status: 200 }));
          await window.__firstModelChange;
          const app = document.querySelector('#app')._x_dataStack[0];
          const st = app.tabState[sid];
          const answer = {
            calls: window.__modelPatchCalls,
            model: app.model,
            meta: app.sessions.find(s => s.id === sid).model,
            changing: st._modelChanging,
          };
          window.__restoreModelTest();
          return answer;
        }""",
        arg=[targets["sid"]],
    )
    assert result == {
        "calls": 1,
        "model": "e2e-target-a",
        "meta": "e2e-target-a",
        "changing": False,
    }
    expect(page.locator('select[x-model="model"]')).to_be_enabled()


def test_delayed_session_list_cannot_rewind_settings(
        page: Page, backend_url, auth_token):
    """A list response minted before PATCH must not restore effort=auto."""
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const sid = app.currentId;
          const original = app.sessions.find(s => s.id === sid);
          const base = {
            ...original, effort: '', permission: 'default', thinking: true,
          };
          app.sessions = [base];
          app.effort = '';
          app.permission = 'default';
          app.thinkingEnabled = true;
          const realReconcile = app._reconcileOpenSession;
          app._reconcileOpenSession = () => {};

          const realFetch = window.fetch;
          let resolvePatch;
          window.fetch = (url, init = {}) => {
            if (String(url).endsWith(`/api/chat/sessions/${sid}`)
                && init.method === 'PATCH') {
              return new Promise(resolve => { resolvePatch = resolve; });
            }
            return realFetch(url, init);
          };

          app.effort = 'max';
          const done = app.onEffortChange();
          while (!resolvePatch) await new Promise(r => setTimeout(r, 0));
          // This payload was computed before the PATCH and arrives while the
          // write is in flight. It must not touch either mirror.
          app._applySessionList([{ ...base, effort: '' }]);
          const during = {
            root: app.effort,
            meta: app.sessions[0].effort,
          };

          resolvePatch(new Response('{}', { status: 200 }));
          await done;
          // A second delayed delivery after the ACK is equally stale.
          app._applySessionList([{ ...base, effort: '' }]);
          const afterAck = {
            root: app.effort,
            meta: app.sessions[0].effort,
          };

          // The matching server echo releases the sticky guard. A later
          // cross-device permission/thinking change should then update the
          // focused controls even though transcript updated_at did not move.
          app._applySessionList([{ ...base, effort: 'max' }]);
          const st = app.tabState[sid];
          const guardReleased = st._effortExpected === null;
          app._applySessionList([{
            ...base, effort: 'max', permission: 'plan', thinking: false,
          }]);

          window.fetch = realFetch;
          app._reconcileOpenSession = realReconcile;
          return {
            during, afterAck, guardReleased,
            permission: app.permission,
            thinking: app.thinkingEnabled,
          };
        }"""
    )
    assert result == {
        "during": {"root": "max", "meta": "max"},
        "afterAck": {"root": "max", "meta": "max"},
        "guardReleased": True,
        "permission": "plan",
        "thinking": False,
    }


def test_fast_mode_patch_is_catalog_gated_and_stale_list_safe(
        page: Page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const sid = app.currentId;
          const original = app.sessions.find(s => s.id === sid);
          const base = { ...original, service_tier: '' };
          app.sessions = [base];
          app.fastModeEnabled = false;
          app.availableModels = [{
            model: app.model, label: app.model || 'Fast fixture', group: 'E2E',
            supports_fast: true,
            fast_service_tier: 'priority',
            service_tiers: [{ id: 'priority', name: 'Fast', description: '' }],
          }, ...app.availableModels.filter(item => item.model !== app.model)];
          const realReconcile = app._reconcileOpenSession;
          app._reconcileOpenSession = () => {};
          const realFetch = window.fetch;
          let resolvePatch;
          window.fetch = (url, init = {}) => {
            if (String(url).endsWith(`/api/chat/sessions/${sid}`)
                && init.method === 'PATCH') {
              return new Promise(resolve => { resolvePatch = resolve; });
            }
            return realFetch(url, init);
          };

          app.fastModeEnabled = true;
          const done = app.onFastModeChange();
          while (!resolvePatch) await new Promise(resolve => setTimeout(resolve, 0));
          app._applySessionList([{ ...base, service_tier: '' }]);
          const during = {
            root: app.fastModeEnabled,
            tier: app.sessions[0].service_tier,
          };
          resolvePatch(new Response('{}', { status: 200 }));
          await done;
          app._applySessionList([{ ...base, service_tier: '' }]);
          const afterAck = {
            root: app.fastModeEnabled,
            tier: app.sessions[0].service_tier,
          };
          app._applySessionList([{
            ...base, model: '', service_tier: 'priority',
          }]);
          const released = app.tabState[sid]._serviceTierExpected === null;
          const omittedModelKeptFast = app.fastModeEnabled;

          window.fetch = realFetch;
          app._reconcileOpenSession = realReconcile;
          return { during, afterAck, released, omittedModelKeptFast };
        }"""
    )
    assert result == {
        "during": {"root": True, "tier": "priority"},
        "afterAck": {"root": True, "tier": "priority"},
        "released": True,
        "omittedModelKeptFast": True,
    }


def test_queue_drops_stale_reads_and_failed_edit_keeps_message(
        page: Page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const sid = app.currentId;
          const st = app._ensureTabState(sid);
          const realFetch = window.fetch;
          const resolvers = [];
          window.fetch = (url, init = {}) => {
            if (String(url).endsWith(`/api/chat/sessions/${sid}/queue`)
                && (!init.method || init.method === 'GET')) {
              return new Promise(resolve => resolvers.push(resolve));
            }
            return realFetch(url, init);
          };
          const oldRead = app._syncQueueFromServer(sid);
          const newRead = app._syncQueueFromServer(sid);
          while (resolvers.length < 2) await new Promise(r => setTimeout(r, 0));
          resolvers[1](new Response(JSON.stringify({
            items: [{ id: 'new', text: 'newest', image_ids: '' }], paused: false,
          }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
          await newRead;
          resolvers[0](new Response(JSON.stringify({
            items: [{ id: 'old', text: 'stale', image_ids: '' }], paused: false,
          }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
          await oldRead;
          const afterRace = st.pendingQueue.map(q => q.id);

          st.pendingQueue = [{ id: 'keep', text: 'do not lose', image_ids: '',
            images: [], docs: [], expiredCount: 0 }];
          app.input = '';
          window.fetch = (url, init = {}) => {
            if (String(url).includes(`/api/chat/sessions/${sid}/queue/keep`)
                && init.method === 'DELETE') {
              return Promise.resolve(new Response('failed', { status: 500 }));
            }
            return realFetch(url, init);
          };
          const realSync = app._syncQueueFromServer;
          app._syncQueueFromServer = async () => {};
          const edited = await app.editPendingQueueItem(sid, 0);
          app._syncQueueFromServer = realSync;
          window.fetch = realFetch;
          return { afterRace, edited, input: app.input,
                   queue: st.pendingQueue.map(q => q.id) };
        }"""
    )
    assert result == {
        "afterRace": ["new"],
        "edited": False,
        "input": "",
        "queue": ["keep"],
    }


def test_identical_queued_prompts_render_once_per_turn_id(
        page: Page, backend_url, auth_token):
    """Two queued `continue` prompts are distinct turns, not duplicates."""
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const sid = app.currentId;
          const st = app._ensureTabState(sid);
          st.streaming = false;
          st.messages.splice(0, st.messages.length,
            { role: 'user', text: '继续', _turn_id: 'turn-1' });
          st.pendingQueue = [{ id: 'q2', text: '继续' }];
          st._queuePaused = false;

          const realFetch = window.fetch;
          const realSync = app._syncQueueFromServer;
          const realSend = app.send;
          const attached = [];
          app._syncQueueFromServer = async () => {};
          app.send = opts => { attached.push(opts); };
          window.fetch = (url, init = {}) => {
            if (String(url).endsWith(`/api/chat/sessions/${sid}/active`)) {
              return Promise.resolve(new Response(JSON.stringify({
                active: true,
                turn_id: 'turn-2',
                user_text: '继续',
                user_images: [],
                user_docs: [],
                elapsed_seconds: 1.25,
              }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
            }
            return realFetch(url, init);
          };

          await app._attachToServerTurn(sid, 1, 'turn-1');
          // A second attach probe for the same active turn must not duplicate
          // the row we just injected.
          await app._attachToServerTurn(sid, 1, 'turn-1');
          await new Promise(resolve => setTimeout(resolve, 0));
          const answer = {
            texts: st.messages.filter(m => m.role === 'user').map(m => m.text),
            turnIds: st.messages.filter(m => m.role === 'user').map(m => m._turn_id),
            attached: attached.length,
          };
          window.fetch = realFetch;
          app._syncQueueFromServer = realSync;
          app.send = realSend;
          return answer;
        }"""
    )
    assert result == {
        "texts": ["继续", "继续"],
        "turnIds": ["turn-1", "turn-2"],
        "attached": 2,
    }


def test_stream_ticket_failure_leaves_retryable_message_and_attachments(
        page: Page, backend_url, auth_token):
    """A pre-SSE failure must not strand a cleared attachment-only draft."""
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const sid = app.currentId;
          const st = app._ensureTabState(sid);
          st.streaming = false;
          st.es = null;
          const session = app.sessions.find(s => s.id === sid);
          if (session) {
            session.active = false;
            session.model = 'e2e-model';
          }
          app.model = 'e2e-model';
          app.availableModels = [{
            model: 'e2e-model', label: 'E2E', group: 'E2E',
            provider: '', supports_effort: false,
          }];
          st.messages.splice(0, st.messages.length);
          app.messages = st.messages;
          app.input = '';
          app.pendingImages = [{
            id: 'a'.repeat(32), mime: 'image/png',
            preview: 'data:image/png;base64,AA==', attach_ext: 'png',
            uploading: false, error: false,
          }];
          app.pendingDocs = [];

          const realFetch = window.fetch;
          let ticketCalls = 0;
          window.fetch = (url, init = {}) => {
            if (String(url) === '/api/chat/stream/start') {
              ticketCalls += 1;
              return Promise.resolve(new Response('bad request', { status: 400 }));
            }
            return realFetch(url, init);
          };
          await app.send({ sessionId: sid });
          const failed = st.messages.find(m => m.role === 'user');
          const afterFailure = {
            ticketCalls,
            failed: !!failed?._failed,
            retryable: failed?._error_retryable,
            imageId: failed?._retryPayload?.images?.[0]?.id || '',
            composerImages: app.pendingImages.length,
            streaming: st.streaming,
            timerCleared: st._streamTimer === null,
            elapsed: st.streamElapsed,
          };

          const realSend = app.send;
          let restored = null;
          app.send = () => {
            restored = {
              text: app.input,
              imageId: app.pendingImages[0]?.id || '',
            };
          };
          app.retryFailedMessage(failed);
          await new Promise(resolve => setTimeout(resolve, 20));
          app.send = realSend;
          window.fetch = realFetch;
          return { afterFailure, restored, rows: st.messages.length };
        }"""
    )
    assert result == {
        "afterFailure": {
            "ticketCalls": 1,
            "failed": True,
            "retryable": True,
            "imageId": "a" * 32,
            "composerImages": 0,
            "streaming": False,
            "timerCleared": True,
            "elapsed": 0,
        },
        "restored": {"text": "", "imageId": "a" * 32},
        "rows": 0,
    }


def test_inline_rename_via_dblclick(page: Page, backend_url, auth_token):
    """Double-click a tab title to swap in the rename input; Enter commits.
    Guards the x-if/blur race regression."""
    _login(page, backend_url, auth_token)
    active_name = page.locator(f"{SEL_TAB_ACTIVE} {SEL_TAB_NAME}")
    active_name.dblclick()

    inp = page.locator(f"{SEL_TAB_ACTIVE} {SEL_TAB_RENAME}")
    expect(inp).to_be_visible()
    inp.fill("e2e-renamed")
    inp.press("Enter")
    expect(active_name).to_contain_text("e2e-renamed")


def test_browser_title_reflects_session(page: Page, backend_url, auth_token):
    """document.title should include the active session's name after rename
    — exercises the x-effect on the root element."""
    _login(page, backend_url, auth_token)
    page.locator(f"{SEL_TAB_ACTIVE} {SEL_TAB_NAME}").dblclick()
    inp = page.locator(f"{SEL_TAB_ACTIVE} {SEL_TAB_RENAME}")
    inp.fill("title-probe")
    inp.press("Enter")
    page.wait_for_function("document.title.includes('title-probe')")
    assert "muselab" in page.title()


def test_keyboard_shortcut_ctrl_t_opens_tab(page: Page, backend_url, auth_token):
    """Ctrl+T opens a new tab and makes it active."""
    _login(page, backend_url, auth_token)
    start = page.locator(SEL_TAB).count()
    # Click into the tab strip first so focus is inside the app — global
    # keydown only fires when nothing else is consuming the event.
    page.locator(SEL_TABS).click()
    page.keyboard.press("Control+t")
    expect(page.locator(SEL_TAB)).to_have_count(start + 1)


def test_workspace_switches_files_previews_and_sessions_together(
        page: Page, backend_url, auth_token, tmp_path):
    """An app-level workspace owns all three surfaces and remembers each one."""
    _login(page, backend_url, auth_token)
    primary = page.evaluate(
        "() => document.querySelector('#app')._x_dataStack[0].currentWorkspacePath()")
    primary_id = page.evaluate(
        "() => document.querySelector('#app')._x_dataStack[0].currentId")
    other = Path(primary) / ("workspace-two-" + tmp_path.name)
    other.mkdir()
    (other / "WORKSPACE_ONLY.md").write_text(
        "# second workspace\n\nworkspace-isolated-preview\n", encoding="utf-8")

    page.locator('.filelist li[data-path="README.md"]').click()
    page.wait_for_function(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return app.selected === 'README.md' && app.rawText.includes('muselab e2e');
        }""")

    # Add through the actual server-side folder browser, not by typing an
    # absolute path or mutating Alpine state.
    page.locator(".workspace-picker-btn").click()
    expect(page.locator(".workspace-picker-pop")).to_be_visible()
    page.locator(".workspace-picker-add").click()
    expect(page.locator(".workspace-browser-modal")).to_be_visible()
    row = page.locator(
        f'.workspace-browser-row[data-workspace-path="{other}"]')
    expect(row).to_be_visible(timeout=5000)
    row.locator(".workspace-browser-entry").click()
    expect(page.locator(
        f'.workspace-browser-row.selected[data-workspace-path="{other}"]'
    )).to_be_visible()
    page.locator(".workspace-browser-confirm").click()
    page.wait_for_function(
        """([path]) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const session = app.sessions.find(s => s.id === app.currentId);
          return app.activeWorkspace === path && !app.workspaceSwitching
            && !app._creatingSession && session?.cwd === path;
        }""",
        arg=[str(other)],
        timeout=15000,
    )

    isolated = page.evaluate(
        """([path]) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const byId = new Map(app.sessions.map(s => [s.id, s]));
          return {
            visible: app.visible.map(n => n.path),
            selected: app.selected,
            currentId: app.currentId,
            tabCwds: app.workspaceOpenTabIds().map(id => byId.get(id)?.cwd),
            workspace: app.currentWorkspacePath(),
          };
        }""",
        arg=[str(other)],
    )
    assert "WORKSPACE_ONLY.md" in isolated["visible"]
    assert "README.md" not in isolated["visible"]
    assert isolated["selected"] == ""
    assert isolated["workspace"] == str(other)
    assert isolated["tabCwds"] and set(isolated["tabCwds"]) == {str(other)}
    secondary_id = isolated["currentId"]
    # A new workspace starts with an empty conversation. The onboarding view
    # owns the viewport until the first message, so resident transcript panes
    # are intentionally mounted but hidden from layout.
    expect(page.locator(".chat-empty:visible")).to_have_count(1)

    page.locator('.filelist li[data-path="WORKSPACE_ONLY.md"]').click()
    page.wait_for_function(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return app.selected === 'WORKSPACE_ONLY.md'
            && app.rawText.includes('workspace-isolated-preview');
        }""")

    # Hold an old-workspace directory response across the switch. A common
    # filename/path in both projects must never let that late payload populate
    # the newly-active tree cache.
    page.evaluate(
        """([primaryWorkspace]) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const realFetch = window.fetch;
          window.__workspaceRace = {
            ready: false, done: false, result: '',
            holdRoot: primaryWorkspace, rootReady: false,
          };
          window.fetch = (url, init = {}) => {
            if (String(url).includes('path=__workspace_race__')) {
              return new Promise(resolve => {
                window.__workspaceRace.ready = true;
                window.__workspaceRace.release = () => resolve(new Response(
                  JSON.stringify({entries: [{
                    name: 'README.md', path: '__workspace_race__/README.md',
                    is_dir: false, size: 1, mtime: 1,
                  }]}),
                  {status: 200, headers: {'Content-Type': 'application/json'}},
                ));
              });
            }
            const headers = init.headers || {};
            const encodedWorkspace = headers['X-Muselab-Workspace']
              || headers['x-muselab-workspace'] || '';
            let requestWorkspace = '';
            try { requestWorkspace = decodeURIComponent(encodedWorkspace); }
            catch { requestWorkspace = encodedWorkspace; }
            const requestUrl = new URL(String(url), location.origin);
            if (window.__workspaceRace.holdRoot
                && requestWorkspace === window.__workspaceRace.holdRoot
                && requestUrl.pathname === '/api/files/list'
                && requestUrl.searchParams.get('path') === '') {
              window.__workspaceRace.holdRoot = '';
              return new Promise(resolve => {
                window.__workspaceRace.rootReady = true;
                window.__workspaceRace.releaseRoot = () => {
                  realFetch(url, init).then(resolve);
                };
              });
            }
            return realFetch(url, init);
          };
          app.fetchChildren('__workspace_race__').then(
            () => { window.__workspaceRace.result = 'resolved'; },
            error => {
              window.__workspaceRace.result = error?.staleWorkspace
                ? 'stale' : String(error);
            },
          ).finally(() => {
            window.fetch = realFetch;
            window.__workspaceRace.done = true;
          });
        }""",
        arg=[primary],
    )
    page.wait_for_function("() => window.__workspaceRace?.ready === true")

    # Switching back restores the primary workspace's own preview and session.
    page.locator(".workspace-picker-btn").click()
    page.locator(
        f'.workspace-picker-row[title="{primary}"] .workspace-picker-select').click()
    page.wait_for_function(
        """([path]) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return app.workspaceSwitching && app.activeWorkspace === path
            && window.__workspaceRace?.rootReady === true;
        }""",
        arg=[primary],
    )
    locked = page.evaluate(
        """async ([oldSid]) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const before = app.tabState[oldSid].messages.length;
          const draft = app.input;
          app.input = 'must-not-send-during-workspace-switch';
          await app.send();
          const result = {
            textareaDisabled: document.querySelector('.chat-input-textarea').disabled,
            sendDisabled: document.querySelector('.chat-toolbar-send').disabled,
            visiblePanes: Array.from(document.querySelectorAll('.chat-session-pane'))
              .filter(el => getComputedStyle(el).display !== 'none').length,
            messagesUnchanged: app.tabState[oldSid].messages.length === before,
          };
          app.input = draft;
          return result;
        }""",
        arg=[secondary_id],
    )
    assert locked == {
        "textareaDisabled": True,
        "sendDisabled": True,
        "visiblePanes": 0,
        "messagesUnchanged": True,
    }
    page.evaluate("() => window.__workspaceRace.releaseRoot()")
    page.wait_for_function(
        """([path, sid]) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return app.currentWorkspacePath() === path && app.currentId === sid
            && app.selected === 'README.md' && app.rawText.includes('muselab e2e')
            && !app.workspaceSwitching;
        }""",
        arg=[primary, primary_id],
    )
    page.evaluate("() => window.__workspaceRace.release()")
    page.wait_for_function("() => window.__workspaceRace?.done === true")
    race = page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return {
            result: window.__workspaceRace.result,
            leaked: Object.keys(app.childCache)
              .some(key => key.startsWith('__workspace_race__:')),
          };
        }""")
    assert race == {"result": "stale", "leaked": False}

    # The secondary surface is independently remembered as well.
    page.locator(".workspace-picker-btn").click()
    page.locator(
        f'.workspace-picker-row[title="{other}"] .workspace-picker-select').click()
    page.wait_for_function(
        """([path, sid]) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return app.currentWorkspacePath() === path && app.currentId === sid
            && app.selected === 'WORKSPACE_ONLY.md'
            && app.rawText.includes('workspace-isolated-preview')
            && !app.workspaceSwitching;
        }""",
        arg=[str(other), secondary_id],
    )

    # Remove it through the UI. Files and native Codex threads remain on disk.
    page.locator(".workspace-picker-btn").click()
    page.locator(
        f'.workspace-picker-row[title="{primary}"] .workspace-picker-select').click()
    page.wait_for_function(
        "([path]) => document.querySelector('#app')._x_dataStack[0].currentWorkspacePath() === path",
        arg=[primary],
    )
    page.locator(".workspace-picker-btn").click()
    page.locator(
        f'.workspace-picker-row[title="{other}"] .workspace-picker-remove').click()
    expect(page.locator(".confirm-modal")).to_be_visible()
    page.locator(".confirm-modal .btn-danger").click()
    page.wait_for_function(
        """([path]) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return !app.workspaceSwitching
            && !app.sessionWorkspaces.some(w => w.path === path);
        }""",
        arg=[str(other)],
    )


def test_workspace_folder_browser_is_fullscreen_and_navigable_on_mobile(
        page: Page, backend_url, auth_token, tmp_path):
    page.set_viewport_size({"width": 390, "height": 844})
    _login(page, backend_url, auth_token)
    primary = page.evaluate(
        "() => document.querySelector('#app')._x_dataStack[0].currentWorkspacePath()")
    parent = Path(primary) / ("mobile-picker-" + tmp_path.name)
    child = parent / "nested-project"
    child.mkdir(parents=True)
    (child / "package.json").write_text('{"name":"nested"}\n', encoding="utf-8")

    page.locator(".workspace-picker-btn").click()
    page.locator(".workspace-picker-add").click()
    modal = page.locator(".workspace-browser-modal")
    expect(modal).to_be_visible()
    page.wait_for_timeout(250)  # let modal-in's scale transform settle
    box = modal.bounding_box()
    assert box is not None
    assert box["x"] == 0
    assert box["y"] == 0
    assert box["width"] >= 389
    assert box["height"] >= 843

    parent_row = page.locator(
        f'.workspace-browser-row[data-workspace-path="{parent}"]')
    expect(parent_row).to_be_visible(timeout=5000)
    parent_row.locator(".workspace-browser-open").click()
    page.wait_for_function(
        """([path]) => document.querySelector('#app')._x_dataStack[0]
          .workspaceBrowser.path === path""",
        arg=[str(parent)],
    )
    child_row = page.locator(
        f'.workspace-browser-row[data-workspace-path="{child}"]')
    expect(child_row).to_be_visible()
    expect(child_row).to_contain_text("Node.js")

    page.locator(".workspace-browser-up").click()
    page.wait_for_function(
        """([path]) => document.querySelector('#app')._x_dataStack[0]
          .workspaceBrowser.path === path""",
        arg=[primary],
    )
    page.locator(".workspace-browser-modal .modal-close").click()
    expect(modal).to_be_hidden()


def test_delayed_queue_acceptance_does_not_erase_a_new_draft(
        page: Page, backend_url, auth_token):
    """A queue POST can resolve after the user edits the shared composer.
    Only the submitted snapshot may be removed."""
    _login(page, backend_url, auth_token)
    page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const sid = app.currentId;
          // This test replaces the queue transport and never starts a model
          // turn. Do not let asynchronous provider discovery make it exercise
          // send()'s unrelated no-model gate instead of the draft race.
          if (!app.availableModels.length) {
            app.availableModels = [{ model: 'e2e-model', label: 'E2E model' }];
            app.model = 'e2e-model';
          }
          app.sessions.find(s => s.id === sid).active = true;
          app._ensureTabState(sid).streaming = false;
          app.input = 'queued draft';
          app.pendingDocs = [{ id: 'old-doc', name: 'old.txt' }];
          app._enqueueMessage = () => new Promise(resolve => {
            window.__acceptQueuedMessage = resolve;
          });
          window.__queueSendSid = sid;
          window.__queueSendDone = false;
          app.send().finally(() => { window.__queueSendDone = true; });
        }""")
    page.wait_for_function(
        "() => typeof window.__acceptQueuedMessage === 'function' "
        "|| window.__queueSendDone === true"
    )
    setup = page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const sid = window.__queueSendSid;
          return {
            accepted: typeof window.__acceptQueuedMessage === 'function',
            currentId: app.currentId,
            sendSid: sid,
            busy: app._isBusy(sid),
            active: !!app.sessions.find(s => s.id === sid)?.active,
            pendingCreate: !!app._sessionCreatePromises[sid],
            models: app.availableModels.length,
          };
        }"""
    )
    assert setup["accepted"], setup
    page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const target = 'draft-race-target';
          app.sessions.push({ id: target, name: 'Draft target', active: false,
            updated_at: Date.now() / 1000 });
          app._ensureTabState(target)._loaded = true;
          await app.openTab(target);
          app.input = 'new draft typed while waiting';
          app.pendingDocs.push({ id: 'new-doc', name: 'new.txt' });
          window.__acceptQueuedMessage(true);
        }""")
    page.wait_for_function("() => window.__queueSendDone === true")
    draft = page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return { input: app.input, docs: app.pendingDocs.map(d => d.id),
                   currentId: app.currentId };
        }""")
    assert draft == {"input": "new draft typed while waiting",
                     "docs": ["new-doc"], "currentId": "draft-race-target"}


def test_busy_send_waits_for_attachment_upload_before_enqueue(
        page: Page, backend_url, auth_token):
    """An in-flight upload must not become a queued text-only message."""
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const sid = app.currentId;
          const st = app._ensureTabState(sid);
          if (!app.availableModels.length) {
            app.availableModels = [{ model: 'e2e-model', label: 'E2E model' }];
            app.model = 'e2e-model';
          }
          app.sessions.find(s => s.id === sid).active = true;
          st.streaming = false;
          app.input = '';
          const image = {
            id: '', mime: 'image/png', preview: 'data:image/png;base64,AA==',
            attach_ext: 'png', uploading: true, error: false,
          };
          app.pendingImages = [image];
          app.pendingDocs = [];
          const realEnqueue = app._enqueueMessage;
          const enqueuedIds = [];
          app._enqueueMessage = async (_sid, item) => {
            enqueuedIds.push(item.pendingImages.map(entry => entry.id));
            return true;
          };

          const sending = app.send({ sessionId: sid });
          await new Promise(resolve => setTimeout(resolve, 120));
          const before = {
            calls: enqueuedIds.length,
            waiting: app._sendWaitingForUpload,
          };
          image.id = 'a'.repeat(32);
          image.uploading = false;
          await sending;
          const after = {
            calls: enqueuedIds.length,
            ids: enqueuedIds[0] || [],
            waiting: app._sendWaitingForUpload,
            composerImages: app.pendingImages.length,
          };
          app._enqueueMessage = realEnqueue;
          return { before, after };
        }"""
    )
    assert result == {
        "before": {"calls": 0, "waiting": True},
        "after": {
            "calls": 1,
            "ids": ["a" * 32],
            "waiting": False,
            "composerImages": 0,
        },
    }


def test_failed_attachment_blocks_send_and_preserves_composer(
        page: Page, backend_url, auth_token):
    """Upload failure requires an explicit remove/retry; text is not sent alone."""
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const sid = app.currentId;
          const st = app._ensureTabState(sid);
          if (!app.availableModels.length) {
            app.availableModels = [{ model: 'e2e-model', label: 'E2E model' }];
            app.model = 'e2e-model';
          }
          app.sessions.find(s => s.id === sid).active = false;
          st.streaming = false;
          st.es = null;
          st.messages.splice(0, st.messages.length);
          app.messages = st.messages;
          app.input = 'read the attached image';
          const image = {
            id: '', mime: 'image/png', preview: 'data:image/png;base64,AA==',
            attach_ext: 'png', uploading: true, error: false,
          };
          app.pendingImages = [image];
          app.pendingDocs = [];
          const realFetch = window.fetch;
          let ticketCalls = 0;
          window.fetch = (url, init = {}) => {
            if (String(url) === '/api/chat/stream/start') ticketCalls += 1;
            return realFetch(url, init);
          };

          const sending = app.send({ sessionId: sid });
          await new Promise(resolve => setTimeout(resolve, 20));
          image.error = 'upload failed';
          image.uploading = false;
          await sending;
          const answer = {
            ticketCalls,
            rows: st.messages.length,
            input: app.input,
            composerImages: app.pendingImages.length,
            imageError: !!app.pendingImages[0]?.error,
            waiting: app._sendWaitingForUpload,
          };
          window.fetch = realFetch;
          return answer;
        }"""
    )
    assert result == {
        "ticketCalls": 0,
        "rows": 0,
        "input": "read the attached image",
        "composerImages": 1,
        "imageError": True,
        "waiting": False,
    }


def test_enqueue_completion_race_attaches_started_turn_immediately(
        page: Page, backend_url, auth_token):
    """If the old turn ended during POST, the queued item must not go dormant."""
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const sid = app.currentId;
          const st = app._ensureTabState(sid);
          st.messages.splice(0, st.messages.length);
          st.streaming = false;
          st.pendingQueue = [];
          const session = app.sessions.find(s => s.id === sid);
          session.active = true;  // browser's stale pre-POST view
          session.model = 'e2e-model';
          app.model = 'e2e-model';
          app.availableModels = [{
            model: 'e2e-model', label: 'E2E', group: 'E2E', provider: '',
          }];
          app.input = 'land in the race';
          app.pendingImages = [];
          app.pendingDocs = [];

          const realFetch = window.fetch;
          const realSend = app.send;
          const reconnects = [];
          window.fetch = (url, init = {}) => {
            const value = String(url);
            if (value.endsWith(`/api/chat/sessions/${sid}/queue`)
                && init.method === 'POST') {
              const payload = JSON.parse(init.body);
              return Promise.resolve(new Response(JSON.stringify({
                ok: true, started: true, item: { id: 'race-item' },
                items: [], paused: false, echoed_drain: payload.drain_if_idle,
              }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
            }
            if (value.endsWith(`/api/chat/sessions/${sid}/queue`)
                && (!init.method || init.method === 'GET')) {
              return Promise.resolve(new Response(JSON.stringify({
                items: [], paused: false,
              }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
            }
            if (value.endsWith(`/api/chat/sessions/${sid}/active`)) {
              return Promise.resolve(new Response(JSON.stringify({
                active: true, turn_id: 'race-turn',
                user_text: 'land in the race', user_images: [], user_docs: [],
                elapsed_seconds: 0.2,
              }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
            }
            return realFetch(url, init);
          };
          const runOriginalSend = realSend.bind(app);
          app.send = opts => { reconnects.push(opts); };
          await runOriginalSend({ sessionId: sid });
          const answer = {
            input: app.input,
            users: st.messages.filter(m => m.role === 'user').map(m => ({
              text: m.text, turn: m._turn_id,
            })),
            reconnects: reconnects.map(x => ({
              reconnect: !!x.reconnect, sessionId: x.sessionId,
            })),
            queue: st.pendingQueue.length,
          };
          app.send = realSend;
          window.fetch = realFetch;
          return answer;
        }"""
    )
    assert result["input"] == ""
    assert result["users"] == [{"text": "land in the race", "turn": "race-turn"}]
    assert len(result["reconnects"]) == 1
    assert result["reconnects"][0]["reconnect"] is True
    assert result["reconnects"][0]["sessionId"]
    assert result["queue"] == 0


def test_current_session_queues_for_server_active_turn_without_duplicate_bubbles(
        page: Page, backend_url, auth_token):
    """A server-owned turn is busy even without a local EventSource; sending
    must enqueue once instead of starting a second turn or duplicating it."""
    _login(page, backend_url, auth_token)
    page.locator(SEL_TAB_NEW).click()
    page.wait_for_function(
        "() => !document.querySelector('#app')._x_dataStack[0]._creatingSession")
    sid = page.evaluate(
        "() => document.querySelector('#app')._x_dataStack[0].currentId")

    posts = []

    def queue_route(route, request):
        if request.method == "POST":
            posts.append(request.post_data_json)
            route.fulfill(status=200, json={"ok": True})
            return
        route.fulfill(status=200, json={
            "items": [{
                "id": "queued-probe",
                "text": "queued from current session",
                "image_ids": "",
                "enqueued_at": 1,
            }],
            "paused": False,
        })

    page.route(f"**/api/chat/sessions/{sid}/queue", queue_route)
    # The product intentionally removes the resident transcript stack from
    # layout while a brand-new session has no messages; otherwise its 100%
    # minimum height sits below the onboarding empty state and recreates the
    # large blank band this layout is meant to prevent. Seed one existing
    # bubble so this test exercises the real "busy transcript queues and
    # follows the new row" path instead of waiting for a deliberately hidden
    # empty pane.
    page.evaluate(
        """([id]) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const st = app._ensureTabState(id);
          st.messages.push({
            role: 'assistant',
            text: 'existing transcript',
            _k: 'queue-scroll-seed',
          });
          app._activateTabState(id);
        }""",
        arg=[sid],
    )
    scroller = page.locator(
        f"{SEL_SESSION_PANE}[data-pane-id='{sid}'] .msg-pane")
    scroller.evaluate(
        """el => {
          for (let i = 0; i < 80; i++) {
            const row = document.createElement('div');
            row.className = 'msg';
            row.style.minHeight = '36px';
            row.textContent = `queue scroll row ${i}`;
            el.appendChild(row);
          }
          el.scrollTop = 0;
        }""")
    page.wait_for_function(
        """([id]) => {
          const el = document.querySelector(
            `.chat-session-pane[data-pane-id='${id}'] .msg-pane`);
          return el && el.scrollHeight > el.clientHeight && el.scrollTop === 0;
        }""",
        arg=[sid],
    )
    page.evaluate(
        """async ([id]) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const session = app.sessions.find(s => s.id === id);
          session.active = true;
          const st = app._ensureTabState(id);
          st.streaming = false;
          app.input = 'queued from current session';
          await app.send();
        }""",
        arg=[sid],
    )

    assert len(posts) == 1
    assert posts[0]["text"] == "queued from current session"
    expect(page.locator(".msg.user.queued")).to_have_count(1)
    page.wait_for_function(
        """([id]) => {
          const el = document.querySelector(
            `.chat-session-pane[data-pane-id='${id}'] .msg-pane`);
          return el && (el.scrollHeight - el.scrollTop - el.clientHeight) < 2;
        }""",
        arg=[sid],
    )


def test_mobile_server_owned_turn_keeps_stop_visible_and_preserves_queue(
        page: Page, backend_url, auth_token):
    """The transcript and composer must agree on session-level running state.

    Reproduces the phone bug: the server says the current session is active,
    while root `streaming` is false after a tab switch/reconnect. Two queued
    messages must not hide or repurpose the stop button.
    """
    page.set_viewport_size({"width": 390, "height": 844})
    _login(page, backend_url, auth_token)
    page.locator(SEL_TAB_NEW).click()
    page.wait_for_function(
        "() => !document.querySelector('#app')._x_dataStack[0]._creatingSession")

    interrupts = []

    def interrupt_route(route, request):
        interrupts.append(request.url)
        route.fulfill(status=200, json={
            "ok": True,
            "interrupted": [request.url.split("session_id=", 1)[-1]],
            "queue_paused": True,
        })

    page.route("**/api/chat/interrupt?*", interrupt_route)
    sid = page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const sid = app.currentId;
          const session = app.sessions.find(s => s.id === sid);
          session.active = true;
          const st = app._ensureTabState(sid);
          st.streaming = false;
          st.messages.splice(0, st.messages.length,
            { role: 'user', text: 'long task' },
            { role: 'assistant', text: 'partial reply' });
          st.streamElapsed = 12.5;
          st._streamStartedAt = Date.now() - 12_500;
          st.pendingQueue = [
            { id: 'queued-1', text: 'first', images: [], docs: [],
              expiredCount: 0, enqueuedAt: Date.now() },
            { id: 'queued-2', text: 'second', images: [], docs: [],
              expiredCount: 0, enqueuedAt: Date.now() },
          ];
          st._queuePaused = false;
          app.streaming = false;
          app.mobileTab = 'chat';
          app._syncQueueFromServer = async () => {};
          app.refreshSessions = async () => true;
          return sid;
        }"""
    )

    stop = page.locator(".chat-toolbar-stop")
    expect(stop).to_be_visible()
    expect(stop).to_be_enabled()
    expect(page.locator(".chat-toolbar-queue-badge")).to_have_text("2")
    title = stop.get_attribute("title") or ""
    assert "停止" in title or "Stop" in title
    assert "撤回" not in title and "Pop" not in title

    stop.click()
    page.wait_for_function(
        """([id]) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const st = app.tabState[id];
          const session = app.sessions.find(s => s.id === id);
          return st && !st._stopping && !st.streaming
            && st.pendingQueue.length === 2 && st._queuePaused
            && session && !session.active;
        }""",
        arg=[sid],
    )

    assert len(interrupts) == 1
    assert f"session_id={sid}" in interrupts[0]
    footer = page.evaluate(
        """([id]) => {
          const st = document.querySelector('#app')._x_dataStack[0].tabState[id];
          const tail = st.messages[st.messages.length - 1];
          return { elapsed: tail.elapsed, hasTimestamp: Number.isFinite(tail.ts) };
        }""",
        arg=[sid],
    )
    assert 12 <= footer["elapsed"] < 15
    assert footer["hasTimestamp"] is True
    expect(stop).to_be_hidden()


def test_stop_cancels_pending_sse_reconnect(page: Page, backend_url, auth_token):
    """A backoff callback must not resurrect a turn after Stop succeeds."""
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const sid = app.currentId;
          const st = app._ensureTabState(sid);
          st.messages.splice(0, st.messages.length);
          st.streaming = false;
          st.es = null;
          const session = app.sessions.find(s => s.id === sid);
          session.active = false;
          session.model = 'e2e-model';
          app.model = 'e2e-model';
          app.availableModels = [{
            model: 'e2e-model', label: 'E2E', group: 'E2E', provider: '',
          }];
          app.input = 'stop during reconnect';
          app.pendingImages = [];
          app.pendingDocs = [];

          const RealEventSource = window.EventSource;
          const streams = [];
          class FakeEventSource extends EventTarget {
            constructor(url) {
              super(); this.url = url; this.readyState = 0; streams.push(this);
              setTimeout(() => {
                this.readyState = 1;
                if (this.onopen) this.onopen(new Event('open'));
              }, 0);
            }
            close() { this.readyState = 2; this.closed = true; }
          }
          window.EventSource = FakeEventSource;

          const realFetch = window.fetch;
          const realSync = app._syncQueueFromServer;
          const realRefresh = app.refreshSessions;
          let activeProbes = 0;
          window.fetch = (url, init = {}) => {
            const value = String(url);
            if (value === '/api/chat/stream/start') {
              return Promise.resolve(new Response(JSON.stringify({ ticket: 'e2e-ticket' }), {
                status: 200, headers: { 'Content-Type': 'application/json' },
              }));
            }
            if (value.startsWith('/api/chat/interrupt?')) {
              return Promise.resolve(new Response(JSON.stringify({
                ok: true, interrupted: [sid], queue_paused: false,
              }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
            }
            if (value.endsWith(`/api/chat/sessions/${sid}/active`)) {
              activeProbes += 1;
              return Promise.resolve(new Response(JSON.stringify({ active: true }), {
                status: 200, headers: { 'Content-Type': 'application/json' },
              }));
            }
            return realFetch(url, init);
          };
          app._syncQueueFromServer = async () => {};
          app.refreshSessions = async () => true;

          await app.send({ sessionId: sid });
          streams[0].dispatchEvent(new Event('error'));
          const timerWasArmed = !!st._reconnectTimer;
          await app.stop();
          await new Promise(resolve => setTimeout(resolve, 1050));
          const answer = {
            timerWasArmed,
            timerCleared: st._reconnectTimer === null,
            streaming: st.streaming,
            streams: streams.length,
            activeProbes,
          };
          window.EventSource = RealEventSource;
          window.fetch = realFetch;
          app._syncQueueFromServer = realSync;
          app.refreshSessions = realRefresh;
          return answer;
        }"""
    )
    assert result == {
        "timerWasArmed": True,
        "timerCleared": True,
        "streaming": False,
        "streams": 1,
        "activeProbes": 0,
    }


# Note: drag-and-drop tab reorder and right-click context menu are harder
# to drive reliably with Playwright's HTML5 drag emulation across browsers.
# Left as manual smoke for now.
