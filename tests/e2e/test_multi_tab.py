"""Browser-level smoke tests for the multi-tab chat UI.

These cover the regression classes that bit us during the 2026-05-17
multi-tab sprint and can ONLY be caught in a real browser:
- DOM event wiring (click, drag, contextmenu)
- Alpine x-effect / x-show / x-if reactivity races
- localStorage round-tripping (preview path, open tabs)
- document.title responding to streaming + session changes

Skipped by default. Enable with `RUN_E2E=1`. See tests/e2e/README.md."""
from __future__ import annotations
import pytest

pytest.importorskip("playwright.sync_api",
                    reason="install with: uv add --group dev pytest-playwright")
from playwright.sync_api import Page, expect  # noqa: E402


# Selectors mirror frontend/index.html. Centralised so a UI rename only
# breaks one place.
SEL_LOGIN = ".login"
SEL_LOGIN_INPUT = '.login input[type="password"]'
SEL_TABS = ".chat-tabs-list"
SEL_TAB = ".chat-tab:not(.chat-workspace-tab)"
SEL_TAB_ACTIVE = ".chat-tab:not(.chat-workspace-tab).active"
SEL_TAB_NAME = ".chat-tab-name"
SEL_TAB_RENAME = ".chat-tab-rename-input"
SEL_TAB_CLOSE = ".chat-tab-close"
SEL_TAB_NEW = ".chat-tab-new"
SEL_GRID_PANE = ".chat-grid-pane:visible"
SEL_GRID_PANE_ACTIVE = ".chat-grid-pane.active:visible"
SEL_GRID_MASK = ".chat-grid-mask"
SEL_GRID_TOGGLE = ".chat-grid-toggle"
SEL_GRID_MENU = ".chat-grid-menu"


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
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
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
          const realVisible = app.visibleChatPaneIds;
          const attached = [];
          app._syncQueueFromServer = async () => {};
          app.visibleChatPaneIds = () => [sid];
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
          app.visibleChatPaneIds = realVisible;
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


def test_chat_grid_shows_multiple_sessions_with_one_active_pane(
        page: Page, backend_url, auth_token):
    """Multiple conversations remain visible while currentId is the sole
    interactive/composer target; clicking a mask activates that pane."""
    _login(page, backend_url, auth_token)
    page.locator(SEL_TAB_NEW).click()
    page.locator(SEL_TAB_NEW).click()
    page.wait_for_function(
        """() => document.querySelector('#app')._x_dataStack[0].openTabIds.length >= 3""")
    page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const ids = app.openTabIds.slice(-3);
          for (const id of ids) await app.addSessionToGrid(id);
        }""")
    expect(page.locator(SEL_GRID_PANE)).to_have_count(3)
    expect(page.locator(SEL_GRID_PANE_ACTIVE)).to_have_count(1)

    inactive = page.locator(f"{SEL_GRID_PANE}:not(.active)").first
    target = inactive.get_attribute("data-pane-id")
    page.evaluate(
        """([id]) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const st = app._ensureTabState(id);
          st.messages.splice(0, st.messages.length, ...Array.from({length: 80}, (_, i) => ({
            _k: `grid-scroll-${i}`,
            role: 'assistant',
            text: `scroll row ${i}`,
            html: `<p>scroll row ${i}</p>`,
          })));
        }""",
        arg=[target],
    )
    scroller = inactive.locator(".msg-pane")
    page.wait_for_function(
        "([id]) => { const el = document.querySelector(`.chat-grid-pane[data-pane-id='${id}'] .msg-pane`); return el && el.scrollHeight > el.clientHeight; }",
        arg=[target],
    )
    mask = inactive.locator(SEL_GRID_MASK)
    expected_label = page.evaluate(
        "() => document.querySelector('#app')._x_dataStack[0].lang === 'zh'"
    )
    expect(mask).to_have_text(
        "点击选中会话" if expected_label else "Click to select session")
    mask_style = mask.evaluate(
        """el => {
          const s = getComputedStyle(el);
          return { background: s.backgroundColor, color: s.color,
                   fontSize: getComputedStyle(el.firstElementChild).fontSize };
        }""")
    assert mask_style["background"] not in {"transparent", "rgba(0, 0, 0, 0)"}
    assert mask_style["color"] not in {"transparent", "rgba(0, 0, 0, 0)"}
    assert float(mask_style["fontSize"].removesuffix("px")) >= 18

    mask.dispatch_event("wheel", {"deltaY": 600})
    page.wait_for_function(
        "([id]) => document.querySelector(`.chat-grid-pane[data-pane-id='${id}'] .msg-pane`).scrollTop > 0",
        arg=[target],
    )
    assert scroller.evaluate("el => el.scrollTop") > 0

    mask.click()
    page.wait_for_function(
        "([id]) => document.querySelector('#app')._x_dataStack[0].currentId === id",
        arg=[target],
    )
    expect(page.locator(SEL_GRID_PANE_ACTIVE)).to_have_attribute("data-pane-id", target)


def test_mobile_hides_multi_view_and_renders_only_current_session(
        page: Page, backend_url, auth_token):
    """Desktop split preferences may remain stored, but phones expose neither
    the multi-view control nor background panes."""
    page.set_viewport_size({"width": 390, "height": 844})
    _login(page, backend_url, auth_token)
    page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          app.splitPaneIds = [app.currentId, 'stored-desktop-pane'];
        }""")

    expect(page.locator(".chat-grid-menu-wrap")).to_be_hidden()
    expect(page.locator(SEL_GRID_PANE)).to_have_count(1)
    visible = page.evaluate(
        """() => { const app = document.querySelector('#app')._x_dataStack[0];
        return { visible: app.visibleChatPaneIds(), stored: app.splitPaneIds }; }""")
    assert visible["visible"] == [page.evaluate(
        "() => document.querySelector('#app')._x_dataStack[0].currentId")]
    assert len(visible["stored"]) == 2


def test_mobile_single_session_grid_has_no_separator_fill(
        page: Page, backend_url, auth_token):
    """The grid's desktop separator color must not paint a tall grey block
    below a short single-session transcript on phones."""
    page.set_viewport_size({"width": 390, "height": 844})
    _login(page, backend_url, auth_token)

    colors = page.evaluate(
        """() => {
          const grid = document.querySelector('.chat-grid');
          const pane = document.querySelector('.chat-grid-pane');
          return {
            grid: getComputedStyle(grid).backgroundColor,
            pane: getComputedStyle(pane).backgroundColor,
          };
        }""")
    assert colors["grid"] in {"transparent", "rgba(0, 0, 0, 0)"}
    assert colors["pane"] in {"transparent", "rgba(0, 0, 0, 0)"}


def test_new_chat_replaces_selected_grid_pane_without_leaving_multi_view(
        page: Page, backend_url, auth_token):
    """The + button follows ordinary grid navigation: the optimistic draft
    and its adopted native id replace the selected pane in place."""
    _login(page, backend_url, auth_token)
    while page.evaluate(
            "() => document.querySelector('#app')._x_dataStack[0].openTabIds.length") < 3:
        page.locator(SEL_TAB_NEW).click()
        page.wait_for_function(
            "() => !document.querySelector('#app')._x_dataStack[0]._creatingSession")

    before = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          await app.setChatGridView(3);
          return { currentId: app.currentId,
                   splitPaneIds: [...app.splitPaneIds],
                   visibleIds: [...app.visibleChatPaneIds()],
                   layout: app.chatGridLayout,
                   openTabIds: [...app.openTabIds],
                   tabCount: app.openTabIds.length };
        }""")
    expect(page.locator(SEL_GRID_PANE)).to_have_count(3)
    expect(page.locator(".chat-workspace-tab.active")).to_have_count(1)
    page.locator(SEL_TAB_NEW).click()
    expect(page.locator(SEL_TAB)).to_have_count(before["tabCount"] + 1)
    expect(page.locator(SEL_GRID_PANE)).to_have_count(3)
    optimistic = page.evaluate(
        """() => { const app = document.querySelector('#app')._x_dataStack[0];
        return { mode: app.chatViewMode, currentId: app.currentId,
                 splitPaneIds: [...app.splitPaneIds],
                 visibleIds: [...app.visibleChatPaneIds()] }; }""")
    selected = before["splitPaneIds"].index(before["currentId"])
    expected_optimistic = list(before["splitPaneIds"])
    expected_optimistic[selected] = optimistic["currentId"]
    assert optimistic["mode"] == "grid"
    assert optimistic["currentId"] not in before["visibleIds"]
    assert optimistic["splitPaneIds"] == expected_optimistic
    assert optimistic["visibleIds"] == expected_optimistic
    expect(page.locator(".chat-workspace-tab.active")).to_have_count(1)
    expect(page.locator(SEL_TAB_ACTIVE)).to_have_count(0)

    page.wait_for_function(
        "() => document.querySelector('#app')?._x_dataStack?.[0]?._creatingSession === false")
    expect(page.locator(SEL_GRID_PANE)).to_have_count(3)
    adopted = page.evaluate(
        """() => { const app = document.querySelector('#app')._x_dataStack[0];
        return { mode: app.chatViewMode, currentId: app.currentId,
                 splitPaneIds: [...app.splitPaneIds],
                 visibleIds: [...app.visibleChatPaneIds()],
                 focusId: app.chatGridFocusId,
                 layout: app.chatGridLayout }; }""")
    expected_adopted = list(before["splitPaneIds"])
    expected_adopted[selected] = adopted["currentId"]
    assert adopted["mode"] == "grid"
    assert not adopted["currentId"].startswith("draft-")
    assert adopted["splitPaneIds"] == expected_adopted
    assert adopted["visibleIds"] == expected_adopted
    assert adopted["focusId"] == adopted["currentId"]
    assert adopted["layout"] == before["layout"]


def test_failed_new_chat_restores_the_original_grid_selection(
        page: Page, backend_url, auth_token):
    """A failed native thread/start must not leave the selected grid cell
    missing after its optimistic draft is removed."""
    _login(page, backend_url, auth_token)
    before = page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const origin = app.currentId;
          const peer = 'create-failure-peer';
          app.sessions.push({ id: peer, name: peer, active: false,
            updated_at: Date.now() / 1000 });
          app.openTabIds.push(peer);
          app._ensureTabState(peer)._loaded = true;
          app.splitPaneIds = [origin, peer];
          app.chatViewMode = 'grid';
          app.chatGridFocusId = origin;
          return { currentId: origin, split: [...app.splitPaneIds] };
        }"""
    )

    def fail_create(route, request):
        if request.method == "POST":
            route.fulfill(status=503, body="unavailable")
        else:
            route.continue_()

    page.route("**/api/chat/sessions", fail_create)
    page.locator(SEL_TAB_NEW).click()
    page.wait_for_function(
        "() => document.querySelector('#app')?._x_dataStack?.[0]?._creatingSession === false"
    )
    restored = page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return { mode: app.chatViewMode, currentId: app.currentId,
                   split: [...app.splitPaneIds],
                   visible: [...app.visibleChatPaneIds()] };
        }"""
    )
    assert restored == {
        "mode": "grid",
        "currentId": before["currentId"],
        "split": before["split"],
        "visible": before["split"],
    }


def test_failed_new_chat_preserves_newer_grid_focus(
        page: Page, backend_url, auth_token):
    """Creation rollback repairs the draft cell without stealing focus."""
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const origin = app.currentId;
          const peer = 'create-failure-focus-peer';
          app.sessions.push({ id: peer, name: peer, active: false,
            updated_at: Date.now() / 1000 });
          app.openTabIds.push(peer);
          app._ensureTabState(peer)._loaded = true;
          app.splitPaneIds = [origin, peer];
          app.chatViewMode = 'grid';
          app.chatGridFocusId = origin;

          const realFetch = window.fetch;
          let resolveCreate;
          window.fetch = (url, init = {}) => {
            if (String(url).endsWith('/api/chat/sessions')
                && init.method === 'POST') {
              return new Promise(resolve => { resolveCreate = resolve; });
            }
            return realFetch(url, init);
          };
          const creating = app.newSession();
          while (!resolveCreate) await new Promise(r => setTimeout(r, 0));
          const draft = app.currentId;
          app.currentId = peer;
          app.chatGridFocusId = peer;
          app._activateTabState(peer);
          resolveCreate(new Response('unavailable', { status: 503 }));
          await creating;
          window.fetch = realFetch;
          return {
            origin, peer, draft,
            mode: app.chatViewMode,
            currentId: app.currentId,
            focusId: app.chatGridFocusId,
            split: [...app.splitPaneIds],
          };
        }"""
    )
    assert result["draft"].startswith("draft-")
    assert result["mode"] == "grid"
    assert result["currentId"] == result["peer"]
    assert result["focusId"] == result["peer"]
    assert result["split"] == [result["origin"], result["peer"]]


def test_session_init_eagerly_restores_all_visible_grid_panes(
        page: Page, backend_url, auth_token):
    """A restored grid must hydrate every visible conversation instead of
    painting an empty dark pane beneath the inactive-session veil."""
    _login(page, backend_url, auth_token)
    while page.evaluate(
            "() => document.querySelector('#app')._x_dataStack[0].openTabIds.length") < 3:
        page.locator(SEL_TAB_NEW).click()
        page.wait_for_function(
            "() => document.querySelector('#app')?._x_dataStack?.[0]?._creatingSession === false")

    pane_ids = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          await app.setChatGridView(3);
          for (const id of app.splitPaneIds) {
            const st = app._ensureTabState(id);
            st._loaded = false;
            st.messages.length = 0;
          }
          return [...app.splitPaneIds];
        }""")
    assert len(pane_ids) == 3
    expect(page.locator(".chat-grid-pane-loading:visible")).to_have_count(3)
    expect(page.locator(f"{SEL_GRID_MASK}:visible")).to_have_count(0)

    restored = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          await app._initSessionsOnce({ skipRefresh: true });
          return { paneIds: [...app.splitPaneIds],
                   visibleIds: [...app.visibleChatPaneIds()],
                   loadedIds: app.visibleChatPaneIds().filter(
                     id => app.tabState[id] && app.tabState[id]._loaded) };
        }""")
    assert restored["paneIds"] == pane_ids
    assert restored["loadedIds"] == restored["visibleIds"]
    expect(page.locator(SEL_GRID_PANE)).to_have_count(3)
    expect(page.locator(".chat-grid-pane-loading:visible")).to_have_count(0)
    expect(page.locator(f"{SEL_GRID_MASK}:visible")).to_have_count(2)


def test_two_pane_grid_scrolls_inside_each_pane(page: Page, backend_url, auth_token):
    """The two-column implicit grid row must be height-constrained; otherwise
    message content stretches the row below the viewport and gets clipped."""
    _login(page, backend_url, auth_token)
    page.locator(SEL_TAB_NEW).click()
    page.wait_for_function(
        "() => document.querySelector('#app')._x_dataStack[0].openTabIds.length >= 2")
    page.wait_for_function(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return app.openTabIds.slice(-2).every(id => app.tabState[id] && app.tabState[id]._loaded);
        }""")
    ids = page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const ids = app.openTabIds.slice(-2);
          app.splitPaneIds = ids;
          app.chatViewMode = 'grid';
          return ids;
        }""")
    expect(page.locator(SEL_GRID_PANE)).to_have_count(2)
    for sid in ids:
        pane = page.locator(f"{SEL_GRID_PANE}[data-pane-id='{sid}']")
        scroller = pane.locator(".msg-pane")
        scroller.evaluate(
            """el => { for (let i = 0; i < 100; i++) {
              const row = document.createElement('div');
              row.className = 'msg'; row.style.minHeight = '36px';
              row.textContent = `scroll row ${i}`; el.appendChild(row);
            }}""")
        page.wait_for_function(
            "([id]) => { const el = document.querySelector(`.chat-grid-pane[data-pane-id='${id}'] .msg-pane`); return el && el.scrollHeight > el.clientHeight; }",
            arg=[sid],
        )
        if pane.evaluate("el => el.classList.contains('active')"):
            scroller.hover()
            page.mouse.wheel(0, 700)
        else:
            pane.locator(SEL_GRID_MASK).dispatch_event("wheel", {"deltaY": 700})
        page.wait_for_function(
            "([id]) => document.querySelector(`.chat-grid-pane[data-pane-id='${id}'] .msg-pane`).scrollTop > 0",
            arg=[sid],
        )


def test_background_grid_history_prepend_preserves_viewport(
        page: Page, backend_url, auth_token):
    """Loading older rows in a visible non-focused pane must anchor its scroll."""
    _login(page, backend_url, auth_token)
    page.locator(SEL_TAB_NEW).click()
    page.wait_for_function(
        "() => document.querySelector('#app')._x_dataStack[0].openTabIds.length >= 2")
    background_id = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          await app.setChatGridView(2);
          const sid = app.splitPaneIds.find(id => id !== app.currentId);
          const st = app._ensureTabState(sid);
          const mk = (prefix, i) => ({
            role: i % 2 ? 'assistant' : 'user',
            text: `${prefix} ${i} ${'viewport anchor '.repeat(45)}`,
            html: i % 2 ? `<p>${prefix} ${i} ${'viewport anchor '.repeat(45)}</p>` : '',
            uuid: `${prefix}-${i}`, _k: `${sid}-${prefix}-${i}`, _noAnim: true,
          });
          st.messages.splice(0, st.messages.length,
            ...Array.from({ length: 24 }, (_, i) => mk('visible', i)));
          st._earlierMessages = Array.from({ length: 6 }, (_, i) => mk('older', i));
          st._hasMoreHistory = true;
          st._loaded = true;
          return sid;
        }""")
    pane = page.locator(
        f".chat-grid-pane[data-pane-id='{background_id}'] .msg-pane")
    expect(pane.locator(".msg[data-uuid^='visible-']")).to_have_count(24)

    result = page.evaluate(
        """async sid => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const el = document.querySelector(
            `.chat-grid-pane[data-pane-id="${sid}"] .msg-pane`);
          el.scrollTop = Math.max(100, Math.floor(
            (el.scrollHeight - el.clientHeight) * 0.45));
          app._userScrollIntent(sid);
          app.onChatScroll(sid, { currentTarget: el });
          const before = { top: el.scrollTop, height: el.scrollHeight };
          await app.loadEarlierMessages(sid);
          await new Promise(resolve => requestAnimationFrame(
            () => requestAnimationFrame(resolve)));
          return {
            before,
            after: { top: el.scrollTop, height: el.scrollHeight },
            count: el.querySelectorAll(
              '.msg[data-uuid^="visible-"], .msg[data-uuid^="older-"]').length,
          };
        }""",
        background_id,
    )
    assert result["count"] == 30
    height_delta = result["after"]["height"] - result["before"]["height"]
    top_delta = result["after"]["top"] - result["before"]["top"]
    assert height_delta > 0
    assert abs(top_delta - height_delta) <= 6


def test_selecting_completed_grid_pane_clears_status_dot(
        page: Page, backend_url, auth_token):
    """An idle pane has no status dot; a completed background pane keeps one
    only until the user selects it, then clears the underlying unread flag."""
    _login(page, backend_url, auth_token)
    page.locator(SEL_TAB_NEW).click()
    page.wait_for_function(
        "() => document.querySelector('#app')._x_dataStack[0].openTabIds.length >= 2")
    background_id = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          await app.setChatGridView(2);
          const id = app.splitPaneIds.find(x => x !== app.currentId);
          app._ensureTabState(id).unread = true;
          return id;
        }""")
    pane = page.locator(f"{SEL_GRID_PANE}[data-pane-id='{background_id}']")
    expect(pane.locator(".chat-grid-pane-status:visible")).to_have_count(1)

    pane.locator(SEL_GRID_MASK).click()

    expect(page.locator(".chat-grid-pane-status:visible")).to_have_count(0)
    assert page.evaluate(
        "([id]) => document.querySelector('#app')._x_dataStack[0].tabState[id].unread",
        [background_id],
    ) is False


def test_single_pane_can_reach_latest_message(page: Page, backend_url, auth_token):
    """Single-pane mode scrolls on .chat-body; its only grid row must remain
    content-sized instead of clipping messages to one viewport."""
    _login(page, backend_url, auth_token)
    page.wait_for_function(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return app.tabState[app.currentId] && app.tabState[app.currentId]._loaded;
        }""")
    page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          app.splitPaneIds = [];
          app.chatViewMode = 'single';
        }""")
    page.locator(".chat-grid-pane .msg-pane").evaluate(
        """el => { for (let i = 0; i < 100; i++) {
          const row = document.createElement('div');
          row.className = 'msg'; row.style.minHeight = '36px';
          row.textContent = `single row ${i}`; el.appendChild(row);
        }}""")
    body = page.locator(".chat-body")
    page.wait_for_function(
        "() => { const el = document.querySelector('.chat-body'); return el && el.scrollHeight > el.clientHeight; }")
    body.evaluate("el => { el.scrollTop = el.scrollHeight; }")
    page.wait_for_function(
        """() => {
          const body = document.querySelector('.chat-body');
          const last = document.querySelector('.chat-grid-pane .msg:last-child');
          if (!body || !last) return false;
          return last.getBoundingClientRect().bottom <= body.getBoundingClientRect().bottom + 1;
        }""")


def test_grid_view_menu_and_toolbar_alignment(page: Page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          while (app.openTabIds.length < 4) {
            const id = `layout-test-${app.openTabIds.length}`;
            app.sessions.push({ id, name: id, active: false,
              created_at: Date.now() / 1000, updated_at: Date.now() / 1000 });
            app.openTabIds.push(id);
            const st = app._ensureTabState(id);
            st.messages.length = 0;
            st._loaded = true;
          }
        }""")
    expect(page.locator(SEL_TAB)).to_have_count(4)

    grid_box = page.locator(SEL_GRID_TOGGLE).bounding_box()
    plus_box = page.locator(SEL_TAB_NEW).bounding_box()
    assert grid_box and plus_box
    assert abs(grid_box["y"] - plus_box["y"]) <= 1
    assert abs(grid_box["height"] - plus_box["height"]) <= 1

    page.locator(SEL_GRID_TOGGLE).click()
    expect(page.locator(SEL_GRID_MENU)).to_be_visible()
    choices = page.locator(f"{SEL_GRID_MENU} button")
    expect(choices).to_have_count(6)
    choices.nth(5).click()
    expect(page.locator(SEL_GRID_PANE)).to_have_count(4)
    expect(page.locator(".chat-grid.panes-4")).to_have_count(1)

    # Shrinking 4 → 3 must remove panes-4. A stale class leaves the fourth
    # grid cell visibly empty even though only three pane nodes remain.
    page.locator(SEL_GRID_TOGGLE).click()
    choices.nth(3).click()
    expect(page.locator(SEL_GRID_PANE)).to_have_count(3)
    expect(page.locator(".chat-grid.panes-3")).to_have_count(1)
    expect(page.locator(".chat-grid.panes-4")).to_have_count(0)
    pane_boxes = [page.locator(SEL_GRID_PANE).nth(i).bounding_box()
                  for i in range(3)]
    assert all(pane_boxes)
    primary, upper, lower = pane_boxes
    # 主 pane 跨两行；右侧两块上下堆叠。Geometry catches the real blank-cell
    # regression that a class-only assertion misses when :first-child fails.
    assert primary["height"] > upper["height"] * 1.8
    assert abs(upper["height"] - lower["height"]) <= 2
    assert abs(upper["x"] - lower["x"]) <= 1
    assert lower["y"] > upper["y"]

    # 次主三窗：前两个会话在左侧上下排列，最后一个占满右侧。
    page.locator(SEL_GRID_TOGGLE).click()
    choices.nth(4).click()
    secondary_boxes = [page.locator(SEL_GRID_PANE).nth(i).bounding_box()
                       for i in range(3)]
    assert all(secondary_boxes)
    upper, lower, primary = secondary_boxes
    assert primary["height"] > upper["height"] * 1.8
    assert abs(upper["x"] - lower["x"]) <= 1
    assert primary["x"] > upper["x"]

    # 上下双栏：两块等宽、上下堆叠。
    page.locator(SEL_GRID_TOGGLE).click()
    choices.nth(2).click()
    row_boxes = [page.locator(SEL_GRID_PANE).nth(i).bounding_box()
                 for i in range(2)]
    assert all(row_boxes)
    upper, lower = row_boxes
    assert abs(upper["width"] - lower["width"]) <= 2
    assert abs(upper["x"] - lower["x"]) <= 1
    assert lower["y"] > upper["y"]

    page.locator(SEL_GRID_TOGGLE).click()
    choices.nth(0).click()
    expect(page.locator(SEL_GRID_PANE)).to_have_count(1)


def test_grid_layout_change_preserves_curated_membership_and_close_invariant(
        page: Page, backend_url, auth_token):
    """Changing only the geometry must keep the curated ids; closing the
    selected pane must immediately select another pane that is still visible."""
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const origin = app.currentId;
          const extras = ['curated-a', 'curated-b', 'not-curated'];
          for (const id of extras) {
            if (!app.sessions.some(s => s.id === id)) {
              app.sessions.push({ id, name: id, active: false,
                updated_at: Date.now() / 1000 });
            }
            if (!app.openTabIds.includes(id)) app.openTabIds.push(id);
            app._ensureTabState(id)._loaded = true;
          }
          app.splitPaneIds = [origin, 'curated-b', 'curated-a'];
          app.chatViewMode = 'grid';
          app.chatGridFocusId = origin;
          await app.setChatGridView(3, 'secondary');
          const afterLayout = [...app.splitPaneIds];
          await app.closeChatTab(app.currentId);
          return {
            origin,
            afterLayout,
            currentId: app.currentId,
            visible: [...app.visibleChatPaneIds()],
            mode: app.chatViewMode,
          };
        }""")
    assert result["afterLayout"] == [result["origin"], "curated-b", "curated-a"]
    assert "not-curated" not in result["afterLayout"]
    assert result["currentId"] in result["visible"]
    assert len(result["visible"]) == 2
    assert result["mode"] == "grid"


def test_clicking_a_tab_outside_the_grid_replaces_the_selected_pane(
        page: Page, backend_url, auth_token):
    """Ordinary tab navigation must stay inside an active multi-view
    workspace; an outside conversation replaces the selected pane."""
    _login(page, backend_url, auth_token)
    state = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const origin = app.currentId;
          for (const id of ['grid-peer', 'outside-grid']) {
            app.sessions.push({ id, name: id, active: false,
              updated_at: Date.now() / 1000 });
            app.openTabIds.push(id);
            app._ensureTabState(id)._loaded = true;
          }
          app.splitPaneIds = [origin, 'grid-peer'];
          app.chatViewMode = 'grid';
          app.chatGridFocusId = origin;
          await app.activateTab('outside-grid');
          return {
            origin,
            currentId: app.currentId,
            split: [...app.splitPaneIds],
            visible: [...app.visibleChatPaneIds()],
            mode: app.chatViewMode,
          };
        }"""
    )
    assert state["mode"] == "grid"
    assert state["currentId"] == "outside-grid"
    assert state["split"] == ["outside-grid", "grid-peer"]
    assert state["visible"] == state["split"]
    assert state["origin"] not in state["split"]


def test_adding_unopened_session_to_grid_keeps_the_origin_pane(
        page: Page, backend_url, auth_token):
    """openTab(id) must not steal currentId before addSessionToGrid seeds the
    two-pane workspace."""
    _login(page, backend_url, auth_token)
    state = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const origin = app.currentId;
          const target = 'history-not-open';
          app.sessions.push({ id: target, name: 'History', active: false,
            updated_at: Date.now() / 1000 });
          app._ensureTabState(target)._loaded = true;
          await app.addSessionToGrid(target);
          return { origin, target, currentId: app.currentId,
                   split: [...app.splitPaneIds] };
        }""")
    assert state["split"] == [state["origin"], state["target"]]
    assert state["currentId"] == state["target"]


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


def test_grid_queues_for_server_active_session_without_duplicate_bubbles(
        page: Page, backend_url, auth_token):
    """A visible background/server-owned turn is busy even without a local
    EventSource; sending must enqueue once in that pane instead of starting a
    second turn or duplicating the queue bubble across every grid pane."""
    _login(page, backend_url, auth_token)
    page.locator(SEL_TAB_NEW).click()
    page.wait_for_function(
        "() => !document.querySelector('#app')._x_dataStack[0]._creatingSession")
    sid = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          await app.setChatGridView(2);
          return app.currentId;
        }""")

    posts = []

    def queue_route(route, request):
        if request.method == "POST":
            posts.append(request.post_data_json)
            route.fulfill(status=200, json={"ok": True})
            return
        route.fulfill(status=200, json={
            "items": [{
                "id": "queued-probe",
                "text": "queued from grid",
                "image_ids": "",
                "enqueued_at": 1,
            }],
            "paused": False,
        })

    page.route(f"**/api/chat/sessions/{sid}/queue", queue_route)
    scroller = page.locator(
        f"{SEL_GRID_PANE}[data-pane-id='{sid}'] .msg-pane")
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
            `.chat-grid-pane[data-pane-id='${id}'] .msg-pane`);
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
          app.input = 'queued from grid';
          await app.send();
        }""",
        arg=[sid],
    )

    assert len(posts) == 1
    assert posts[0]["text"] == "queued from grid"
    expect(page.locator(".msg.user.queued")).to_have_count(1)
    page.wait_for_function(
        """([id]) => {
          const el = document.querySelector(
            `.chat-grid-pane[data-pane-id='${id}'] .msg-pane`);
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


def test_dragging_chat_tab_into_content_creates_split(page: Page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    page.locator(SEL_TAB_NEW).click()
    expect(page.locator(SEL_TAB)).to_have_count(2)
    source = page.locator(SEL_TAB).first
    target = page.locator(".chat-scroll-wrap")
    src_box = source.bounding_box()
    dst_box = target.bounding_box()
    assert src_box and dst_box
    page.mouse.move(src_box["x"] + src_box["width"] / 2,
                    src_box["y"] + src_box["height"] / 2)
    page.mouse.down()
    page.mouse.move(dst_box["x"] + dst_box["width"] / 2,
                    dst_box["y"] + dst_box["height"] / 2,
                    steps=12)
    expect(page.locator(".chat-grid-drop-overlay")).to_be_visible()
    page.mouse.up()
    expect(page.locator(SEL_GRID_PANE)).to_have_count(2)


# Note: drag-and-drop tab reorder and right-click context menu are harder
# to drive reliably with Playwright's HTML5 drag emulation across browsers.
# Left as manual smoke for now.
