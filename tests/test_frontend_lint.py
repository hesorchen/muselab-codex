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


def test_image_generation_history_prompt_actions_are_wired():
    """History prompt actions need both Alpine handlers and template wiring."""
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    index = (FRONTEND / "index.html").read_text(encoding="utf-8")

    assert "copyImageGenPrompt(job)" in app
    assert "reuseImageGenPrompt(job)" in app
    assert '@click="copyImageGenPrompt(job)"' in index
    assert '@click="reuseImageGenPrompt(job)"' in index
    assert 'x-ref="imageGenPrompt"' in index


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
    start = app.index("async _refreshCtxMeter()")
    end = app.index("\n    async showCtxBreakdown()", start)
    method = app[start:end]

    assert "200000" not in method
    assert "this.sessionUsage.context_limit" in method


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


def test_mobile_session_settings_expose_permission_and_effort_without_clipping():
    """The gear popover must show both per-session controls above composer."""
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")

    start = html.index('<div class="chat-toolbar-more">')
    end = html.index("<!-- Extended-thinking", start)
    popover = html[start:end]

    assert "t('session.permission')" in popover
    assert 'x-model="permission"' in popover
    assert "t('session.intelligence')" in popover
    assert 'x-model="effort"' in popover
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
    start = app.index("const _markDone = (cancelled = false) =>")
    end = app.index('es.addEventListener("done"', start)
    mark_done = app[start:end]

    assert "streamState.messages.splice(k, 1" in mark_done
    assert "ts: m.ts || _now" in mark_done
    assert "if (m.role !== \"assistant\") continue" not in mark_done


def test_stale_permission_card_is_marked_expired_on_404():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("async decidePermission(msg, decision)")
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
    snapshot = method.index("const sendSid = this.currentId")
    assert pending < snapshot
    assert "await pendingCreate" in method
    assert "return this.send(opts)" in method


def test_session_poll_is_single_flight_and_saved_tab_keys_are_sanitized():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")

    assert "if (this._sessionListPullPromise) return this._sessionListPullPromise" in app
    assert "async _pullSessionListOnce(" in app
    assert "[...new Set(p.openTabIds.filter(" in app


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
    assert "setTimeout(() => { this.loadRoot().catch(() => {}); }, 150)" in boot
    assert "setTimeout(() => { this.fetchStats(); }, 300)" in boot
    assert "await this.fetchContextInfo()" not in boot


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


def test_chat_messages_use_one_flat_keyed_loop():
    index = (FRONTEND / "index.html").read_text(encoding="utf-8")

    assert 'x-for="(m, i) in messages"' in index
    assert 'x-for="tid in residentPaneIds()"' not in index
    assert "paneMessages(tid)" not in index
