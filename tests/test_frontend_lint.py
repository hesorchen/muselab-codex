"""Frontend static lint — narrow but high-value checks for bug classes that
already shipped once. These read frontend/ as plain text; no JS runtime
needed.

Why this exists: JS object literals silently shadow earlier definitions when
the same key appears twice. We hit this in the multi-tab sprint
(2026-05-17) — a second `closeChatTab(...)` was added below the first one
and the upper definition was lost without any warning. The duplicate sat
undiscovered until a button stopped working. Pytest is the cheapest
guard."""
from __future__ import annotations
import re
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path


FRONTEND = Path(__file__).resolve().parents[1] / "frontend"


class _ComposerTemplateGuard(HTMLParser):
    """Track whether the persistent composer is accidentally inside template."""

    def __init__(self):
        super().__init__()
        self.template_depth = 0
        self.composer_depth: int | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "template":
            self.template_depth += 1
        classes = dict(attrs).get("class", "").split()
        if "chat-input" in classes:
            self.composer_depth = self.template_depth

    def handle_endtag(self, tag):
        if tag == "template":
            self.template_depth -= 1


def test_chat_composer_is_not_nested_in_template():
    """A swallowed template close makes the entire composer non-rendering."""
    parser = _ComposerTemplateGuard()
    parser.feed((FRONTEND / "index.html").read_text(encoding="utf-8"))

    assert parser.composer_depth is not None, "chat composer is missing"
    assert parser.composer_depth == 0, (
        "chat composer is nested in <template>; check for malformed comments "
        "or a missing </template> before .chat-input"
    )


# Match top-level method definitions inside the Alpine x-data object:
#     methodName(args) {
#     async methodName(args) {
#     *gen(args) {
# - Exactly 4 spaces of indent (the component's outer indent level).
# - Strips optional `async ` / `static ` / `*` prefix so it doesn't capture
#   the keyword as the name. Without this, `async closeChatTab` matched as
#   `async` and missed the real collision.
# - Excludes arrow assignments (`const foo = () =>`) and `function ` decls.
# `(?!\{)` negative lookahead excludes calls like `_report({ ... })` where
# the open paren is immediately followed by a `{` (object literal arg). A
# real method def starts with `name(arg…)` or `name()`, never `name({`.
_METHOD_DEF = re.compile(
    r"^    (?:async\s+|static\s+|\*\s*)?([a-zA-Z_][a-zA-Z0-9_]*)\s*\((?!\{)"
)


def test_app_js_has_no_duplicate_method_definitions():
    """Guard against silently shadowed methods in app.js.

    Real bug, 2026-05-17: two `closeChatTab(id)` definitions coexisted —
    JS kept only the second, so the toolbar's close button (wired to the
    first) silently broke. This test would have caught it instantly."""
    text = (FRONTEND / "app.js").read_text(encoding="utf-8")

    names = []
    for line in text.splitlines():
        m = _METHOD_DEF.match(line)
        if not m:
            continue
        name = m.group(1)
        # Skip JS keywords that legitimately appear in the same column shape
        # (if/for/while/switch/return/etc.) — not method defs.
        if name in {
            "if", "for", "while", "switch", "return", "throw", "catch",
            "do", "else", "function", "case",
        }:
            continue
        names.append(name)

    dupes = [n for n, c in Counter(names).items() if c > 1]
    assert not dupes, (
        f"Duplicate method definitions in app.js: {dupes}. "
        "JS keeps only the LAST one — the earlier definitions are dead "
        "code and any caller wired to them silently breaks. Rename or "
        "merge the duplicates."
    )


def test_i18n_zh_en_key_parity():
    """Both language sections in i18n/index.js must define the same set of
    keys. A missing translation causes `t('foo.bar')` to fall back to the
    key literal — exposed to users as 'foo.bar' on screen. We hit this
    historically when a quick zh-only addition landed without the en
    mirror; the English UI showed raw keys until a user reported it."""
    text = (FRONTEND / "i18n" / "index.js").read_text(encoding="utf-8")
    # The file has shape `window.MUSELAB_STRINGS = { zh: {...}, en: {...} };`
    # — split it at the top-level "zh:" / "en:" labels. The blocks are
    # several hundred lines but contain no nested object literals that look
    # like another language label, so a greedy "until next label" works.
    zh_match = re.search(r"\bzh:\s*\{(.*?)\n  \},\s*en:", text, re.S)
    en_match = re.search(r"\ben:\s*\{(.*?)\n  \},?\s*\};", text, re.S)
    assert zh_match, "couldn't find zh: { ... } block in i18n/index.js"
    assert en_match, "couldn't find en: { ... } block in i18n/index.js"
    zh_keys = set(re.findall(r'"([\w.]+)"\s*:', zh_match.group(1)))
    en_keys = set(re.findall(r'"([\w.]+)"\s*:', en_match.group(1)))
    only_zh = zh_keys - en_keys
    only_en = en_keys - zh_keys
    assert not only_zh and not only_en, (
        f"i18n key drift between zh and en. "
        f"only in zh: {sorted(only_zh)[:8]}; "
        f"only in en: {sorted(only_en)[:8]}. "
        f"Add the missing translations or `t()` will leak raw keys to "
        f"users on the side that's missing them."
    )


def test_image_generation_toolbar_uses_native_chat_instead_of_removed_api():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    index = (FRONTEND / "index.html").read_text(encoding="utf-8")

    assert "promptNativeImageGeneration()" in app
    assert '@click="promptNativeImageGeneration()"' in index
    assert "/api/chat/image-generate" not in app
    assert 'x-ref="imageGenPrompt"' not in index
    assert "current.startsWith(lead) ? current : lead + current" in app


def test_removed_background_task_compatibility_ui_cannot_call_dead_routes():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    index = (FRONTEND / "index.html").read_text(encoding="utf-8")

    for dead in (
        "/api/chat/task-output", "task_started", "task_progress",
        "task_notification", "openTaskOutput", "stopBackgroundTask",
        "_ensureBgContPoller", "task_status",
    ):
        assert dead not in app
        assert dead not in index


def test_native_tab_menu_does_not_offer_unsupported_system_prompt_editor():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    index = (FRONTEND / "index.html").read_text(encoding="utf-8")

    assert "menuEditPrompt" not in app
    assert "editSessionPrompt" not in app
    assert "编辑 system prompt" not in index
    assert "Edit system prompt" not in index


def test_session_export_uses_header_auth_blob_and_surfaces_failures():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("async menuExportMarkdown(id)")
    end = app.index("\n    async menuDelete", start)
    method = app[start:end]

    assert "?token=" not in method
    assert "{ headers: this.hdr() }" in method
    assert "await r.blob()" in method
    assert "URL.revokeObjectURL(href)" in method
    assert '"导出失败"' in method


def test_native_boot_does_not_poll_noop_interrupted_turn_sidecar():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")

    assert "/api/chat/interrupted-turns" not in app
    assert "_checkInterruptedTurns" not in app


def test_stream_transport_never_falls_back_to_prompt_or_token_query_params():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("// Ticket flow: POST the prompt")
    end = app.index("const es = new EventSource(url)", start)
    transport = app[start:end]

    assert 'fetch("/api/chat/stream/start"' in transport
    assert 'url = "/api/chat/stream?ticket="' in transport
    assert '"?prompt="' not in transport
    assert '"&token="' not in transport


def test_attachment_uploads_have_a_real_timeout_and_do_not_log_filenames():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")

    assert app.count("() => uploadController.abort(), 5 * 60 * 1000") == 2
    assert app.count("signal: uploadController.signal") == 2
    assert app.count("clearTimeout(uploadTimeout)") == 2
    assert "[muselab][upload]" not in app


def test_slash_compact_uses_native_codex_path():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")

    assert 'case "compact": {' in app
    assert "await this.runCompact(this.currentId);" in app
    assert "/sessions/${this.currentId}/compact" not in app


def test_skills_ui_uses_native_codex_endpoints():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    index = (FRONTEND / "index.html").read_text(encoding="utf-8")

    assert 'fetch("/api/chat/skills"' in app
    assert 'fetch("/api/chat/skills?force_reload=true"' in app
    assert "async toggleSkill(sk)" in app
    assert "/api/settings/skills" not in app
    assert '@click="toggleSkill(sk)"' in index
    assert ':disabled="!sk.enabled"' in index
    assert 'x-text="sk.display_name || sk.name"' in index


def test_mcp_ui_uses_native_codex_endpoints_and_inventory():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    index = (FRONTEND / "index.html").read_text(encoding="utf-8")

    assert 'fetch("/api/chat/mcp"' in app
    assert 'fetch("/api/chat/mcp?reload=true"' in app
    assert "async loginMcpOauth(s)" in app
    assert "/api/settings/mcp" not in app
    assert "mcpExamples" not in app
    assert 'x-text="s.tools.map(tool => tool.name).join(\' · \')"' in index
    assert ':disabled="s.disabled || !s.tool_count"' in index
    assert '@click="loginMcpOauth(s)"' in index


def test_shared_runtime_and_mcp_elicitation_are_wired():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    index = (FRONTEND / "index.html").read_text(encoding="utf-8")

    assert '@click="loadVersions()"' in index
    assert "settings.versions.cli_resume_command" in index
    assert "copyCliResumeCommand()" in app
    assert "askOptionValue(opt)" in index
    assert "m.kind === 'mcp_form'" in index
    assert "m.kind === 'mcp_url'" in index


def test_context_ring_never_invents_a_model_window():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("async _refreshCtxMeter(")
    end = app.index("\n    async showCtxBreakdown()", start)
    method = app[start:end]

    assert "200000" not in method
    assert "st.sessionUsage.context_limit" in method


def test_native_session_mounts_tab_before_activating_it():
    """Changing currentId before its keyed pane exists corrupts Alpine DOM state."""
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("async _newServerSession()")
    end = app.index("\n    newSession()", start)
    method = app[start:end]

    mount = method.index("this.openTabIds.push(draftId)")
    activate = method.index("this.currentId = draftId")
    request = method.index('fetch("/api/chat/sessions"')
    assert mount < activate < request
    assert "name: prefix + stamp" not in method
    assert 'method: "PATCH"' not in method
    assert "if (meta.auto_named)" in method
    assert "thread/name/set" in method
    assert "this.tabState[meta.id] = this.tabState[draftId]" in method
    assert "await this.$nextTick()" in method


def test_default_permission_is_saved_separately_from_current_session():
    """Settings defaults must not be re-seeded from the active old thread."""
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    open_start = app.index("openSettings()")
    open_end = app.index("\n    openSettingsPage", open_start)
    save_start = app.index("async saveSettings()")
    save_end = app.index("\n    async deleteSession", save_start)

    assert 'defaultPermission: "default"' in app
    assert "permission: this.defaultPermission || \"default\"" in app[open_start:open_end]
    assert "this.defaultPermission = defaults.permission" in app[save_start:save_end]
    assert "this.savePrefs()" in app[save_start:save_end]
    assert "defaultPermission: this.defaultPermission" in app

    create_start = app.index("async _newServerSession()")
    create_end = app.index("\n    newSession()", create_start)
    create = app[create_start:create_end]
    assert 'const seedPermission = this.defaultPermission || "default"' in create
    assert "permission: seedPermission" in create
    assert "meta.permission = meta.permission || seedPermission" in create

    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    assert "Codex 默认" not in html
    assert "Codex default" not in html
    assert html.count("permissionLabel('default')") == 3
    assert "'Ask as needed'" not in html


def test_session_permission_is_persisted_with_owner_and_stale_response_guards():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    start = app.index("async onPermissionChange()")
    end = app.index("\n    async onThinkingChange()", start)
    method = app[start:end]

    assert "const sid = this.currentId" in method
    assert "++st._permissionPatchSeq" in method
    assert "JSON.stringify({ permission: next })" in method
    assert "this.tabState[sid] !== st" in method
    assert "if (this.currentId === sid) this.permission" in method
    assert html.count('@change="onPermissionChange()"') == 2
    assert "_permissionPatchSeq: 0" in app
    assert "st._permissionPatchSeq =" in app


def test_session_setting_writes_are_serialized_before_server_persistence():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("async _serializeTabSettingPatch")
    end = app.index("\n    modelGroups()", start)
    settings = app[start:end]

    assert "const prior = st[tailKey] || Promise.resolve()" in settings
    assert "Promise.resolve(prior).catch(() => {}).then(work)" in settings
    assert '"_effortPatchTail"' in settings
    assert '"_serviceTierPatchTail"' in settings
    assert '"_permissionPatchTail"' in settings
    assert '"_thinkingPatchTail"' in settings


def test_native_optimistic_session_resets_composer_settings_and_locks_controls():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    start = app.index("async _newServerSession()")
    end = app.index("\n    newSession()", start)
    method = app[start:end]

    assert 'this.effort = ""' in method
    assert "this.fastModeEnabled = false" in method
    assert "this.thinkingEnabled = true" in method
    assert 'effort: ""' in method
    assert 'service_tier: ""' in method
    assert "thinking: true" in method
    assert "this.effort = meta.effort || \"\"" in method
    assert "this.fastModeEnabled = this._isFastServiceTier(" in method
    assert "this.thinkingEnabled = meta.thinking !== false" in method
    assert "_sessionCreatePromises && this._sessionCreatePromises[sid]" in app
    assert "st._modelChanging" in app
    assert "st._effortPatchTail" in app
    assert html.count(
        ':disabled="workspaceSwitching || _sessionSettingsBusy(currentId)"'
    ) >= 7


def test_mobile_session_settings_expose_all_controls_without_clipping():
    """The gear popover must expose per-session controls without crowding the row."""
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")

    start = html.index('<div class="chat-toolbar-more">')
    end = html.index("<!-- Native reasoning-summary", start)
    popover = html[start:end]

    assert "t('session.permission')" in popover
    assert 'x-model="permission"' in popover
    assert "t('session.intelligence')" in popover
    assert 'x-model="effort"' in popover
    assert "t('fast.label')" in popover
    assert 'x-model="fastModeEnabled"' in popover
    assert "t('thinking.label')" in popover
    assert 'x-model="thinkingEnabled"' in popover
    # Native iOS select sheets dispatch their confirmation click outside the
    # document. click.outside can hide the select before x-model receives its
    # change event; pointerdown.outside only sees genuine taps away.
    assert '@pointerdown.outside="composerSettingsOpen = false"' in popover
    assert '@click.outside="composerSettingsOpen = false"' not in popover
    assert "'settings-open': composerSettingsOpen" in html
    assert ".chat-input-wrap.settings-open { overflow: visible; }" in css


def test_turn_done_stamps_the_actual_tail_message_for_footer():
    """A trailing tool/task item must not leave completion footer empty."""
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("const _markDone = (cancelled = false, timing = {}) =>")
    end = app.index('es.addEventListener("done"', start)
    mark_done = app[start:end]

    assert "streamState.messages.splice(k, 1" in mark_done
    assert "ts: _completedMs" in mark_done
    assert "elapsed: _elapsed >= 1 ? _elapsed : 0" in mark_done
    assert "Number(timing.elapsedMs)" in mark_done
    assert "if (m.role !== \"assistant\") continue" not in mark_done


def test_reconnect_footer_timing_uses_relative_server_age():
    """Phone/server clock skew must not leak into the elapsed counter."""
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")

    assert "elapsedSeconds: d.elapsed_seconds" in app
    assert "Date.now() - (_relativeAge * 1000)" in app
    assert "elapsedMs: _doneElapsedMs" in app


def test_stale_permission_card_is_marked_expired_on_404():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("async decidePermission(msg, decision, sid = this.currentId)")
    end = app.index("\n    async togglePinSession", start)
    method = app[start:end]

    assert "if (r.status === 404)" in method
    assert 'msg.decision = "expired"' in method


def test_send_waits_for_native_id_when_started_from_an_optimistic_draft():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("async send(opts = {})")
    end = app.index("\n    retryFailedMessage", start)
    method = app[start:end]

    pending = method.index("const pendingCreate")
    snapshot = method.index("const sendSid = opts.sessionId || this.currentId")
    assert pending < snapshot
    assert "await pendingCreate" in method
    assert "return this.send(opts)" in method


def test_session_poll_is_single_flight_and_saved_tab_keys_are_sanitized():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")

    assert "if (this._sessionListPullPromise) return this._sessionListPullPromise" in app
    assert "async _pullSessionListOnce(" in app
    assert "signal: controller.signal" in app
    assert "this._reconcileOpenSession(this.sessions)" in app
    assert "[...new Set(p.openTabIds.filter(" in app


def test_open_session_revision_is_only_advanced_after_transcript_load():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("_reconcileOpenSession(next)")
    end = app.index("\n    _sessionsEqual", start)
    reconcile = app[start:end]

    assert reconcile.index("st._seenUpdated = newU") > reconcile.index(
        "if (!needsRefresh)"
    )
    assert "_reconcileTargetUpdated" in reconcile
    assert "const stillBehind" in reconcile
    assert "st._pendingExternalUpdate = true" in reconcile


def test_delayed_session_list_preserves_pending_setting_echoes():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("_retainExpectedSessionSettings(meta)")
    end = app.index("\n    // Shared session-list applier", start)
    helper = app[start:end]
    equal_start = app.index("_sessionsEqual(a, b)")
    equal_end = app.index("\n    // Generic select-rebind", equal_start)
    equality = app[equal_start:equal_end]

    assert '"_modelExpected"' in helper
    assert '"_effortExpected"' in helper
    assert '"_serviceTierExpected"' in helper
    assert '"_permissionExpected"' in helper
    assert '"_thinkingExpected"' in helper
    assert "expected.echoed = true" in helper
    assert "next.map(meta => this._retainExpectedSessionSettings(meta))" in app
    assert "const s = this._retainExpectedSessionSettings(await r.json())" in app
    assert '(x.permission || "default") !== (y.permission || "default")' in equality
    assert '(x.service_tier || "") !== (y.service_tier || "")' in equality


def test_fast_mode_is_catalog_gated_and_sent_independently_from_effort():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    start = app.index("async onFastModeChange()")
    end = app.index("\n    async onPermissionChange()", start)
    method = app[start:end]
    send_start = app.index("async send(opts = {})")
    send_end = app.index("\n    retryFailedMessage", send_start)
    send = app[send_start:send_end]

    assert 'typeof meta.fast_service_tier === "string"' in app
    assert 'toLowerCase() === "fast"' in app
    assert "this.fastModeEnabled ? this._fastServiceTier(this.model)" in method
    assert 'body: JSON.stringify({ service_tier: next })' in method
    assert "this._canonicalServiceTier(" in send
    assert "focused.service_tier, focused.model || this.model" in app
    assert 'service_tier: sendServiceTier' in send
    assert 'serviceTier: sendServiceTier' in send
    assert html.count('x-model="fastModeEnabled"') == 2
    assert html.count('x-show="_supportsFast(model)"') == 2
    assert html.count('x-model="thinkingEnabled"') == 2


def test_large_markdown_preview_skips_rich_dom_postprocessing():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("_renderPreviewMd(text)")
    end = app.index("\n    // opts.streaming", start)
    method = app[start:end]

    assert "this.LARGE_MD_DEFER_CHARS" in method
    assert "this._mdRenderUncached(body, { streaming: true })" in method


def test_open_file_updates_shared_preview_tabs_immutably():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("async openFile(n, opts = {})")
    end = app.index("\n    async openByPath(path)", start)
    method = app[start:end]

    assert "this.tabs.splice(pi, 1" not in method
    assert "this.tabs.push({ path: n.path" not in method
    assert "this.tabs = this.tabs.map((t, i)" in method
    assert "this.tabs = [...this.tabs, { path: n.path" in method


def test_preview_loads_are_abortable_and_latest_request_owned():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("async openFile(n, opts = {})")
    end = app.index("\n    async csvLoadPage", start)
    method = app[start:end]

    assert "this._previewAbort.abort()" in method
    assert "const loadSeq = ++this._previewLoadSeq" in method
    assert "signal: controller.signal" in method
    assert "loadSeq !== this._previewLoadSeq" in method
    assert "(opts.forceReload || needsDiskReload)" in method
    assert '"svg"' in method and 'this.previewMode = "img"' in method


def test_reopening_edited_file_keeps_the_current_buffer():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("async openFile(n, opts = {})")
    end = app.index("\n    async csvLoadPage", start)
    method = app[start:end]

    same_path_guard = "this.editing && n.path === this.selected && !opts.forceReload"
    assert same_path_guard in method
    assert method.index(same_path_guard) < method.index("this.editing = false")
    assert "!opts.editsConfirmed && !this._confirmLoseEdits()" in method


def test_upload_overwrite_uses_operation_local_editor_snapshot():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    prep_start = app.index("_prepareUploadOverwrite(dirPath, files)")
    sync_start = app.index("async _syncUploadedFiles", prep_start)
    sync_end = app.index("\n    onPreviewTabDragStart", sync_start)
    upload = app[prep_start:sync_end]

    assert "editorText = this._cm ? this._cm.getValue() : this.editText" in upload
    assert "uploadContext.editorRef === this._cm" in upload
    assert "liveText !== uploadContext.editorText" in upload
    assert "this._previewNeedsReload = path" in upload
    assert "_uploadDiscardApproved" not in app


def test_csv_page_commits_offset_only_after_success():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("async csvLoadPage(")
    end = app.index("\n    csvWindowEnd", start)
    csv = app[start:end]

    assert "if (this._csvAbort) this._csvAbort.abort()" in csv
    assert "const loadSeq = ++this._csvLoadSeq" in csv
    assert csv.index("this.csvOffset = reqOffset") > csv.index("const data = await r.json()")
    assert "this.csvOffset = next" not in csv
    assert "this.csvData = null" not in csv


def test_preview_cache_and_find_are_memory_bounded():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")

    assert "PREVIEW_CACHE_MAX_BYTES" in app
    assert "_previewCacheEntryBytes(entry)" in app
    assert "this._previewCacheBytes > this.PREVIEW_CACHE_MAX_BYTES" in app
    assert "PREVIEW_FIND_MAX_MATCHES: 500" in app
    assert "this.previewFind.truncated = true" in app


def test_preview_tabs_own_and_persist_their_reading_positions():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    open_start = app.index("async openFile(n, opts = {})")
    open_end = app.index("\n    async csvLoadPage", open_start)
    open_file = app[open_start:open_end]

    assert "this._capturePreviewViewState(this.selected)" in open_file
    assert "this._schedulePreviewViewRestore(cachedPath, loadSeq)" in open_file
    assert "this.csvLoadPage(targetView.csvOffset)" in open_file
    assert "view: this._sanitizePreviewViewState(tab.view)" in app
    assert "this._capturePreviewViewState(this.selected);" in app[
        app.index("_captureWorkspaceSurface(path = \"\")"):
        app.index("async _changeWorkspaceSurface", app.index("_captureWorkspaceSurface(path = \"\")"))
    ]
    assert 'x-ref="previewBody"' in html
    assert '@scroll.passive="onPreviewViewportScroll()"' in html
    assert 'd.__muselab === "preview-scroll"' in app
    assert '__muselab: "preview-scroll-restore"' in app
    assert "this._htmlPreviewFramePosition" in app


def test_html_preview_keeps_four_live_iframes_with_source_ownership():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    open_start = app.index("async openFile(n, opts = {})")
    open_end = app.index("\n    async csvLoadPage", open_start)
    open_file = app[open_start:open_end]
    clear_start = app.index("    _clearPreviewState() {")
    clear_end = app.index("\n    onPreviewImageError()", clear_start)
    clear = app[clear_start:clear_end]

    assert "HTML_PREVIEW_CACHE_MAX: 4" in app
    assert "_touchHtmlPreviewFrame(path)" in app
    assert "next.length >= this.HTML_PREVIEW_CACHE_MAX" in app
    assert "reusedHtmlFrame = this._touchHtmlPreviewFrame(n.path)" in open_file
    assert "this.previewMode === \"html\" && reusedHtmlFrame" in open_file
    assert 'x-for="entry in htmlPreviewFrames" :key="entry.path"' in html
    assert ':src="rawUrl(entry.path, {preview:true})"' in html
    assert "selected===entry.path" in html
    assert "this._htmlPreviewMessageOwner(e.source)" in app
    assert "ownerPath === this.selected" in app
    assert "this.htmlPreviewFrames = []" in clear


def test_mobile_preview_captures_before_hiding_and_pins_tree_taps():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    tab_start = app.index("setMobileTab(next)")
    tab_end = app.index("\n    // The queue is authoritative", tab_start)
    mobile_tab = app[tab_start:tab_end]
    click_start = app.index("async onNodeClick(ev, n)")
    click_end = app.index("\n    // ===== multi-select helpers", click_start)
    node_click = app[click_start:click_end]

    assert mobile_tab.index("this._capturePreviewViewState(ownerPath)") < mobile_tab.index(
        "this.mobileTab = next")
    assert mobile_tab.index("this._restorePreviewViewState(ownerPath, ownerLoadSeq)") < (
        mobile_tab.index("this.mobileTab = next"))
    assert "this._schedulePreviewViewRestore(ownerPath, ownerLoadSeq)" in mobile_tab
    assert 'this.mobileTab !== "preview"' in app
    assert "preview: !this._isMobileLayout()" in node_click
    assert html.count("@click=\"setMobileTab('") == 3


def test_file_tree_refresh_preserves_last_good_content_on_failure():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("async loadRoot()")
    end = app.index("\n    // In-place removal", start)
    load = app[start:end]

    assert "const treeSeq = ++this._treeLoadSeq" in load
    assert "children = await this.fetchChildren" in load
    assert load.index("this.visible =") > load.index("children = await this.fetchChildren")
    assert "return this.loadRoot()" in load
    assert "this.childCache = {};" not in load
    assert "this.treeError" in load
    assert "const wantedExpanded = new Set(pendingExpanded)" in load
    assert "const nextVisible = await buildRows(children, 0)" in load
    assert "this.expanded = nextExpanded" in load
    assert ".slice(0, 8)" not in load


def test_directory_path_changes_and_removals_cover_descendant_tabs():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    remap_start = app.index("_remapPreviewPaths(src, dst)")
    remap_end = app.index("\n    _canRemovePreviewPaths", remap_start)
    drop_start = app.index("async _dropPreviewPathsUnder(roots)")
    drop_end = app.index("\n    // Double-click", drop_start)
    remap = app[remap_start:remap_end]
    drop = app[drop_start:drop_end]

    assert "this._pathAtOrBelow(t.path, src)" in remap
    assert "this.selectedPaths = new Set" in remap
    assert "this.fileClipboard" in remap
    assert "oldTabs.filter(t => !removed(t.path))" in drop
    assert "await this.openFile" in drop


def test_preview_bulk_close_uses_owner_safe_tab_switch_and_shared_clear():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("async previewTabMenuAction(action)")
    end = app.index("\n    async onPreviewDrop", start)
    menu = app[start:end]

    assert menu.index("await this.switchTab(path)") < menu.index(
        "this.tabs = this.tabs.filter(t => t.path === path)")
    assert "this.closeAllTabs()" in menu
    assert 'this.tabs = []; this.selected = ""' not in menu

    clear_start = app.index("    _clearPreviewState() {")
    clear_end = app.index("\n    onPreviewImageError()", clear_start)
    clear = app[clear_start:clear_end]
    assert "this.closePreviewFind()" in clear
    assert clear.index("this.editing = false") < clear.index('this.selected = ""')


def test_file_tree_keyed_list_never_uses_in_place_splice():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("// ===== file tree =====")
    end = app.index("\n    // ===== context menu =====", start)
    tree = app[start:end]

    assert "this.visible.splice(" not in tree
    assert "_uniqueFileNodes(nodes)" in tree


def test_boot_paints_shell_before_deferred_tree_and_stats_work():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("async _bootApp()")
    end = app.index("\n    // Start the always-on background connections", start)
    boot = app[start:end]

    assert "requestAnimationFrame(() => this._markReady())" in boot
    assert "this._fetchModels({ retries: 2 })" in boot
    assert "const workspaceReady = this.fetchSessionWorkspaces()" in boot
    assert "workspaceReady.then(() => this.initSessions())" in boot
    assert "workspaceReady.then(() => this.loadRoot()).catch(() => {})" in boot
    assert "setTimeout(() => { this.fetchStats(); }, 300)" in boot
    assert "await this.fetchContextInfo()" not in boot


def test_presence_restart_replaces_global_handlers_instead_of_stacking_them():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("_startPresence()")
    end = app.index("\n    async _pingHealth()", start)
    presence = app[start:end]

    assert 'document.removeEventListener(\n          "visibilitychange"' in presence
    assert 'document.addEventListener(\n        "visibilitychange"' in presence
    assert 'window.removeEventListener("pagehide"' in presence
    assert 'window.addEventListener("pagehide"' in presence
    assert presence.index("removeEventListener") < presence.index("addEventListener")


def test_model_discovery_precedes_optional_rate_limit_and_retries():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("async fetchStats()")
    end = app.index("\n    async fetchCodexRateLimit", start)
    stats = app[start:end]
    model_start = app.index("async _fetchModels(opts = {})")
    model_end = app.index("\n    // Ensure `this.model`", model_start)
    models = app[model_start:model_end]

    assert stats.index("await this._fetchModels") < stats.index("this.fetchCodexRateLimit()")
    assert "if (this._modelsFetchPromise) return this._modelsFetchPromise" in models
    assert "attempt <= retries" in models


def test_codex_quota_labels_distinguish_equal_duration_limits():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    start = app.index("codexLimitWindowLabel(window)")
    end = app.index("\n    // \"resets in 3h 12m\"", start)
    labeler = app[start:end]

    assert "window.limit_name" in labeler
    assert 'limitId === "codex" ? "Codex" : limitId' in labeler
    assert "`${name} · ${period}`" in labeler
    assert 'x-text="codexLimitWindowLabel(w) || w.key"' in html


def test_chat_transcript_uses_stable_session_and_message_keys():
    index = (FRONTEND / "index.html").read_text(encoding="utf-8")

    assert 'x-for="tid in mountedChatPaneIds()" :key="tid"' in index
    assert 'x-show="tid === currentId && sessionInCurrentWorkspace(tid)"' in index
    assert 'x-for="(m, i) in paneMessages(tid)"' in index
    assert ":key=\"m._k || m.uuid || ('m-' + i)\"" in index
    assert "chat-grid" not in index


def test_chat_messages_do_not_scan_long_text_for_unused_intrinsic_height():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    index = (FRONTEND / "index.html").read_text(encoding="utf-8")

    assert "estIntrinsicH" not in app
    assert "contain-intrinsic-size" not in index


def test_chat_tab_loads_are_single_flight_and_resident_dom_is_bounded():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("async _ensureSessionLoaded(sid)")
    end = app.index("\n    async loadSession", start)
    loader = app[start:end]
    resident_start = app.index("mountedChatPaneIds()")
    resident_end = app.index("\n    // [resident-panes] LRU bookkeeping", resident_start)
    resident = app[resident_start:resident_end]

    assert "this._sessionLoadPromises[sid]" in loader
    assert "if (ok) st._loaded = true" in loader
    assert "this.currentId" in resident
    assert "...this.residentPaneIds()" in resident
    assert "new Set(this.openTabIds || [])" in resident
    assert "return [...new Set(ids)]" in resident


def test_transcript_render_helpers_are_session_scoped():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")

    assert "this.paneMessages(sid)" in app
    assert "isLatestEditTool(i, m, tid)" in html
    assert "isMsgExpanded(i, m, false, tid)" in html
    assert "toolResultSummary(m, i, tid)" in html
    assert "findToolUseFor(m, i, tid)" in html
    assert "taskLogLine(m, tid)" in html
    assert "splitPaneIds" not in app
    assert "chatGrid" not in app


def test_new_sessions_are_bound_to_the_active_workspace_and_roll_back_locally():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("async _newServerSession()")
    end = app.index("\n    newSession()", start)
    native_create = app[start:end]

    assert "const seedCwd = options.cwd || this.currentWorkspacePath()" in native_create
    assert "cwd: seedCwd" in native_create
    assert "[seedCwd]: draftId" in native_create
    assert "this.workspaceOpenTabIds(seedCwd)" in native_create
    assert "chatGrid" not in native_create


def test_visible_session_reconciles_stream_and_scroll_by_owner():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    reconcile_start = app.index("_reconcileOpenSession(next)")
    reconcile_end = app.index("\n    _sessionsEqual", reconcile_start)
    reconcile = app[reconcile_start:reconcile_end]
    send_start = app.index("async send(opts = {})")
    send_end = app.index("\n    retryFailedMessage", send_start)
    send = app[send_start:send_end]

    assert "for (const sid of (this.currentId ? [this.currentId] : []))" in reconcile
    assert "sessionId: sid" in app[app.index("async _checkActiveTurn"):send_start]
    assert "streamSid === this.currentId" in send
    assert "streamState.atBottom !== false" in send


def test_silent_mobile_stream_recovers_from_server_replay_without_reload():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("async _recoverStalledStream(sid = this.currentId)")
    end = app.index("\n    _retireStaleSessionStream", start)
    recovery = app[start:end]

    assert "Date.now() - observedActivity < 18_000" in recovery
    assert "d.events_so_far" in recovery
    assert "st._serverActiveObserved = true" in recovery
    assert "await this.send({" in recovery
    assert "reconnect: true" in recovery
    assert "this._retireStaleSessionStream(sid, st)" in recovery
    assert "await this.loadSession(sid, { quiet: true })" in recovery
    assert "this._recoverStalledStream(streamSid)" in app


def test_single_transcript_stack_has_one_visible_session():
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")

    assert ".chat-session-stack" in css
    assert ".chat-session-pane" in css
    assert ".chat-body.msgs-hidden .chat-session-pane.active .msg" in css
    assert ".chat-grid" not in css


def test_async_queue_clear_only_removes_submitted_composer_snapshot():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("async send(opts = {})")
    end = app.index("\n    retryFailedMessage", start)
    send = app[start:end]

    assert "const draftInputSnapshot = this.input" in send
    assert "if (this.input === draftInputSnapshot) this.input = \"\"" in send
    assert "this.pendingImages.filter(im => !sentImages.has(im))" in send
    assert "this.pendingDocs.filter(d => !sentDocs.has(d))" in send


def test_stream_placeholder_tolerates_uninitialized_tab_state():
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")

    assert "modelLabel((tabState[tid] && tabState[tid].streamingModel) || '')" in html
    assert "tabState[tid] && tabState[tid].streamElapsed > 1" in html
    assert "fmtStreamElapsed((tabState[tid] && tabState[tid].streamElapsed) || 0)" in html
    assert 'class="msg assistant msg-streaming first-token-pending"' in html
    assert ":not(.msg-streaming) .msg-avatar" in css


def test_message_actions_use_their_session_stream_state():
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    pane_start = html.index('<template x-for="tid in mountedChatPaneIds()"')
    pane_end = html.index('<div class="chat-input">', pane_start)
    panes = html[pane_start:pane_end]

    assert 'x-show="!isTabStreaming(tid) && !m._editing"' in panes
    assert 'x-show="!streaming && !m._editing"' not in panes


def test_stop_control_follows_session_state_and_never_removes_queue_items():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    start = app.index("async stop() {")
    end = app.index("\n    // ====== ask_user_question", start)
    stop = app[start:end]

    assert 'x-show="isTabStreaming(currentId)"' in html
    assert "chat-toolbar-stop" in html
    assert 'x-show="streaming" @click="stop()"' not in html
    assert "撤回队尾" not in html
    assert "removePendingQueueItem" not in stop
    assert "await fetch(" in stop
    assert "if (this.isTabStreaming(this.currentId)) { this.stop();" in app
    assert 'if (this.isTabStreaming(this.currentId)) await this.stop();' in app


def test_outline_fetch_throttles_failed_optimistic_draft_requests():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("async refreshOutlineFromBackend(sid)")
    end = app.index("\n    // Pull the next older window", start)
    method = app[start:end]

    assert method.index("st._outlineFetchedAt = now") < method.index("await fetch(")


def test_multi_workspace_ui_is_app_level_and_new_sessions_inherit_it():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")

    assert "window.prompt" not in app
    assert "cwd: seedCwd" in app
    assert 'headers["X-Muselab-Workspace"] = encodeURIComponent(this.activeWorkspace)' in app
    assert '"&workspace=" + encodeURIComponent(workspace)' in app
    assert "workspace-picker" in html
    assert ':disabled="workspaceSwitching || _creatingSession"' in html
    assert "workspaceOpenTabIds()" in html
    assert "chat-toolbar-workspace" not in html
    assert "chat-grid" not in html
    assert ':title="tabTooltip(tid)"' in html
    assert ':disabled="workspaceSwitching || !availableModels.length"' in html
    assert "if (this.workspaceSwitching && !opts.reconnect && !opts.resumedItem) return" in app
    assert "_workspacePreviewTabs(surface = {})" in app
    assert "if (!path || seen.has(path)) return false" in app
    assert "async _refreshSessionsAfterWorkspaceRegistryChange()" in app
    assert app.count("await this._refreshSessionsAfterWorkspaceRegistryChange()") == 2


def test_session_history_and_workspace_use_distinct_semantic_icons():
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    history_start = html.index('class="chat-tab-history-btn"')
    history_end = html.index("</button>", history_start)
    workspace_start = html.index('class="workspace-picker-btn"')
    workspace_end = html.index("</button>", workspace_start)

    assert '#i-history' in html[history_start:history_end]
    assert '#i-folder' not in html[history_start:history_end]
    assert '#i-folder' in html[workspace_start:workspace_end]


def test_add_workspace_uses_a_server_folder_browser_on_desktop_and_mobile():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")
    backend = (FRONTEND.parent / "backend" / "codex" / "api.py").read_text(
        encoding="utf-8")
    start = app.index("async addWorkspace()")
    end = app.index("\n    async removeWorkspace", start)
    add = app[start:end]

    assert "this.prompt" not in add
    assert "browser.show = true" in add
    assert "await this.browseWorkspaceDirectory(this.currentWorkspacePath())" in add
    assert 'fetch(\n          "/api/chat/workspaces/browse" + query' in app
    assert "workspaceBrowserTarget()" in app
    assert 'class="modal workspace-browser-modal"' in html
    assert ':data-workspace-path="directory.path"' in html
    assert 'class="btn-primary workspace-browser-confirm"' in html
    assert '@click="addWorkspacePathManually()"' in html
    assert "async addWorkspacePathManually()" in app
    assert '@router.get("/workspaces/browse"' in backend
    assert ".workspace-browser-modal" in css
    assert "height: 100dvh" in css[css.index("@media (max-width: 720px)", css.index(
        ".workspace-browser-modal")):]


def test_workspace_async_file_surfaces_reject_late_previous_owner_results():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    trash_start = app.index("async loadTrash()")
    trash_end = app.index("\n    openTrashModal()", trash_start)
    trash = app[trash_start:trash_end]
    meta_start = app.index("async loadSelectedMeta(path)")
    meta_end = app.index("\n    // Format a unix-seconds", meta_start)
    meta = app[meta_start:meta_end]
    children_start = app.index("async fetchChildren(path, opts = {})")
    children_end = app.index("\n    _uniqueFileNodes", children_start)
    children = app[children_start:children_end]
    upload_start = app.index("async _syncUploadedFiles(")
    upload_end = app.index("\n    onPreviewTabDragStart", upload_start)
    upload = app[upload_start:upload_end]
    save_start = app.index("async saveEdit()")
    save_end = app.index("\n    // ===== @ mention", save_start)
    save = app[save_start:save_end]
    palette_start = app.index("async _fetchPaletteFiles()")
    palette_end = app.index("\n    // Build the item list", palette_start)
    palette = app[palette_start:palette_end]

    assert "const loadSeq = ++this._trashLoadSeq" in trash
    assert "ownerWorkspace === this.currentWorkspacePath()" in trash
    assert trash.count("if (!isOwner()) return") >= 2
    assert "const loadSeq = ++this._selectedMetaSeq" in meta
    assert "ownerWorkspace === this.currentWorkspacePath()" in meta
    assert "this.selected === path" in meta
    assert "opts.ownerWorkspace || this.currentWorkspacePath()" in children
    assert "this._workspaceIsCurrent(ownerWorkspace)" in children
    assert "stale.staleWorkspace = true" in children
    assert "ownerWorkspace = this.currentWorkspacePath()" in upload
    assert "if (!this._workspaceIsCurrent(ownerWorkspace)) return" in upload
    assert save.index("if (!sameOwner) return") < save.index(
        "this._previewCacheDel(savePath)")
    assert "const requestSeq = ++this._paletteFileSeq" in palette
    assert "requestSeq === this._paletteFileSeq" in palette


def test_context_upload_keeps_the_workspace_that_opened_the_file_picker():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    handler_start = app.index("async ctxUploadHandler(ev)")
    handler_end = app.index("\n    async doRename", handler_start)
    handler = app[handler_start:handler_end]

    assert "this._ctxUploadWorkspace = this.currentWorkspacePath()" in app
    assert "const ownerWorkspace = this._ctxUploadWorkspace" in handler
    assert "!this._workspaceIsCurrent(ownerWorkspace)" in handler


def test_mobile_preview_header_prioritizes_title_and_controls():
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")

    assert ".pane.preview .pane-fileinfo { display: none; }" in css
    assert ".pane.preview .preview-title-stack {" in css
    assert ".pane.preview .preview-mobile-path," in css
    assert ".pane.preview .preview-mobile-mtime {\n    display: block;" in css
    assert ".pane.preview .pane-head { height: 58px; }" in css
    assert ".pane.preview .preview-keep-mobile { flex-shrink: 0; }" in css
    assert "selected.slice(0, selected.lastIndexOf('/') + 1)" in html


def test_history_jump_and_paging_keep_their_session_owner():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    jump_start = app.index("_scrollToUserMsg(m, ownerSid = this.currentId)")
    jump_end = app.index("\n    // Short preview text", jump_start)
    jump = app[jump_start:jump_end]
    paging_start = app.index("async loadEarlierMessages(sid)")
    paging_end = app.index("\n    // Evict the oldest rendered", paging_start)
    paging = app[paging_start:paging_end]

    assert "const sid = ownerSid" in jump
    assert "const body = this._chatScrollEl(sid)" in jump
    assert "body && body.querySelector" in jump
    assert "document.querySelector(" not in jump
    assert "this._scrollToUserMsg(m, sid)" in jump
    assert "if (sid === this.currentId) this.messages = st.messages" in jump
    assert "const isVisible = sid === this.currentId" in paging
    assert "const scrollEl = isVisible ? this._chatScrollEl(sid) : null" in paging


def test_tab_disposal_invalidates_late_loads_and_bounds_hover_cache():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    dispose_start = app.index("_disposeTabRuntime(sid)")
    dispose_end = app.index("\n    async deleteSessionById", dispose_start)
    dispose = app[dispose_start:dispose_end]
    load_start = app.index("async loadSession(sid, opts = {})")
    load_end = app.index("\n    // Warm OPEN-but-inactive tabs", load_start)
    load = app[load_start:load_end]
    prefetch_start = app.index("prefetchSession(sid)")
    prefetch_end = app.index("\n    // One authoritative cold-load", prefetch_start)
    prefetch = app[prefetch_start:prefetch_end]

    assert "clearTimeout(st._reconcileRetryTimer)" in dispose
    assert "delete this._sessionLoadPromises[sid]" in dispose
    assert "st._queueSyncSeq =" in dispose
    assert "delete this._cachedLatestEditIdxByTid[sid]" in dispose
    assert "delete this._cachedTaskSubjectMaps[sid]" in dispose
    assert "!key.startsWith(prefix)" in dispose
    assert "delete this.tabState[sid]" in dispose
    assert load.count("if (this.tabState[sid] !== st) return false") >= 3
    assert "sid === this.currentId && this.tabState[sid] === st" in load
    assert "const previous = this._hoverPrefetchedSid" in prefetch
    assert "this._disposeTabRuntime(previous)" in prefetch
    assert "this._prefetchTargetSid !== sid" in prefetch


def test_failed_transcript_refresh_preserves_the_last_good_messages():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("async loadSession(sid, opts = {})")
    end = app.index("\n    // Warm OPEN-but-inactive tabs", start)
    load = app[start:end]
    failed = load[
        load.index("if (!r.ok) {"):
        load.index("const s = this._retainExpectedSessionSettings(await r.json())")
    ]

    assert "return false" in failed
    assert "st.messages.length = 0" not in failed
    assert "this.messages = st.messages" not in failed


def test_idle_preload_stays_in_workspace_and_yields_to_visible_stream():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("_idlePreloadStep()")
    end = app.index("\n    // ===== Lazy-loaded history controls", start)
    method = app[start:end]

    assert "const ids = this.workspaceOpenTabIds()" in method
    assert "currentState && currentState.streaming" in method
    assert "_idlePreloadRetryTimer" in method
    assert "}, 1000)" in method


def test_visible_session_management_calls_have_native_backend_routes():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    backend = (FRONTEND.parent / "backend" / "codex" / "api.py").read_text(
        encoding="utf-8")

    assert '@router.post("/sessions/organize"' in backend
    assert '@router.post("/sessions/purge-old"' in backend
    assert '"/sessions/{thread_id}/export"' in backend
    assert '@router.get("/search"' in backend
    assert "/api/chat/reset?" not in app
