"""Browser regressions for file-tree and preview async ownership.

These cases need Alpine + a real DOM: static source assertions cannot prove
that aborts, reactive tab state, and editor buffers settle on the right owner.
"""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api",
                    reason="install with: uv add --group dev pytest-playwright")
from playwright.sync_api import Page, expect  # noqa: E402


def _login(page: Page, base: str, token: str) -> None:
    page.goto(base, wait_until="domcontentloaded")
    page.wait_for_selector(".login, .chat-tabs-list", state="visible", timeout=5000)
    if page.locator(".login").is_visible():
        page.fill('.login input[type="password"]', token)
        page.keyboard.press("Enter")
    expect(page.locator(".chat-tabs-list")).to_be_visible(timeout=5000)
    page.wait_for_function(
        """() => {
          const app = document.querySelector('#app')?._x_dataStack?.[0];
          return app && app.authed && app.appReady && app._sessionsInitialized;
        }"""
    )


def test_latest_file_open_owns_preview_and_network_failure_exits_loading(
        page: Page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const realFetch = window.fetch;
          const delayed = (body, delay, signal) => new Promise((resolve, reject) => {
            const timer = setTimeout(() => resolve(new Response(body, {status: 200})), delay);
            signal?.addEventListener('abort', () => {
              clearTimeout(timer);
              reject(new DOMException('Aborted', 'AbortError'));
            }, {once: true});
          });
          window.fetch = (url, init = {}) => {
            const s = String(url);
            if (s.includes('/api/files/read?path=race-a.txt')) {
              return delayed('OLD_A', 120, init.signal);
            }
            if (s.includes('/api/files/read?path=race-b.txt')) {
              return delayed('LATEST_B', 10, init.signal);
            }
            if (s.includes('/api/files/read?path=offline.txt')) {
              return Promise.reject(new TypeError('offline'));
            }
            return realFetch(url, init);
          };
          try {
            const first = app.openFile({path: 'race-a.txt', name: 'race-a.txt'});
            await new Promise(r => setTimeout(r, 5));
            const second = app.openFile({path: 'race-b.txt', name: 'race-b.txt'});
            await Promise.all([first, second]);
            const latest = {
              selected: app.selected, rawText: app.rawText,
              mode: app.previewMode, loading: app.previewMode === 'loading',
            };
            const ok = await app.openFile({path: 'offline.txt', name: 'offline.txt'});
            return {
              latest, ok, offlineMode: app.previewMode,
              offlineTitle: app.previewError?.title || '',
            };
          } finally {
            window.fetch = realFetch;
          }
        }"""
    )
    assert result["latest"] == {
        "selected": "race-b.txt",
        "rawText": "LATEST_B",
        "mode": "text",
        "loading": False,
    }
    assert result["ok"] is False
    assert result["offlineMode"] == "unsupported"
    assert result["offlineTitle"]


def test_rapid_csv_switch_aborts_old_page_and_commits_latest(page: Page,
                                                              backend_url,
                                                              auth_token):
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const realFetch = window.fetch;
          const reply = (path, label, delay, signal) => new Promise((resolve, reject) => {
            const body = JSON.stringify({
              path, header: ['name'], rows: [[label]], offset: 0, limit: 200,
              total_rows: 1, has_header: true, delimiter: ',', cols_truncated: false,
            });
            const timer = setTimeout(() => resolve(new Response(body, {
              status: 200, headers: {'Content-Type': 'application/json'},
            })), delay);
            signal?.addEventListener('abort', () => {
              clearTimeout(timer);
              reject(new DOMException('Aborted', 'AbortError'));
            }, {once: true});
          });
          window.fetch = (url, init = {}) => {
            const s = String(url);
            if (s.includes('/api/files/csv?path=slow-a.csv')) {
              return reply('slow-a.csv', 'OLD', 120, init.signal);
            }
            if (s.includes('/api/files/csv?path=fast-b.csv')) {
              return reply('fast-b.csv', 'LATEST', 10, init.signal);
            }
            return realFetch(url, init);
          };
          try {
            const first = app.openFile({path: 'slow-a.csv', name: 'slow-a.csv'});
            await new Promise(r => setTimeout(r, 5));
            const second = app.openFile({path: 'fast-b.csv', name: 'fast-b.csv'});
            await Promise.all([first, second]);
            return {
              selected: app.selected, csvPath: app.csvPath,
              cell: app.csvData?.rows?.[0]?.[0], mode: app.previewMode,
              loading: app.csvLoading, offset: app.csvOffset,
            };
          } finally {
            window.fetch = realFetch;
          }
        }"""
    )
    assert result == {
        "selected": "fast-b.csv",
        "csvPath": "fast-b.csv",
        "cell": "LATEST",
        "mode": "csv",
        "loading": False,
        "offset": 0,
    }


def test_tree_refresh_failure_keeps_rows_and_search_ignores_stale_results(
        page: Page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const realFetch = window.fetch;
          const delayedJson = (body, delay, signal) => new Promise((resolve, reject) => {
            const timer = setTimeout(() => resolve(new Response(JSON.stringify(body), {
              status: 200, headers: {'Content-Type': 'application/json'},
            })), delay);
            signal?.addEventListener('abort', () => {
              clearTimeout(timer);
              reject(new DOMException('Aborted', 'AbortError'));
            }, {once: true});
          });
          window.fetch = (url, init = {}) => {
            const s = String(url);
            if (s.includes('/api/files/search?q=alpha')) {
              return delayedJson({entries: [{path: 'old.txt', name: 'old.txt'}]}, 100, init.signal);
            }
            if (s.includes('/api/files/grep?q=alpha')) {
              return delayedJson({hits: []}, 100, init.signal);
            }
            if (s.includes('/api/files/search?q=beta')) {
              return delayedJson({entries: [{path: 'new.txt', name: 'new.txt'}]}, 5, init.signal);
            }
            if (s.includes('/api/files/grep?q=beta')) {
              return delayedJson({hits: [{path: 'new.txt', name: 'new.txt', line: 1}]}, 5, init.signal);
            }
            if (s.includes('/api/files/list?path=')) {
              return Promise.reject(new TypeError('tree offline'));
            }
            return realFetch(url, init);
          };
          try {
            app.searchQ = 'alpha';
            const first = app.doSearch();
            await new Promise(r => setTimeout(r, 5));
            app.searchQ = 'beta';
            const second = app.doSearch();
            await Promise.all([first, second]);
            const search = {
              name: app.searchHits[0]?.name,
              grep: app.grepHits[0]?.name,
              searching: app.searching,
            };
            app.visible = [{path: 'keep.txt', name: 'keep.txt', is_dir: false, depth: 0}];
            const ok = await app.reloadTree();
            return {
              search, ok, paths: app.visible.map(n => n.path),
              treeLoading: app.treeLoading,
            };
          } finally {
            window.fetch = realFetch;
          }
        }"""
    )
    assert result["search"] == {
        "name": "new.txt", "grep": "new.txt", "searching": False,
    }
    assert result["ok"] is False
    assert result["paths"] == ["keep.txt"]
    assert result["treeLoading"] is False


def test_tree_refresh_restores_all_expanded_branches(page: Page,
                                                      backend_url,
                                                      auth_token):
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const realFetch = window.fetch;
          const entry = (path, isDir) => ({
            path, name: path.split('/').pop(), is_dir: isDir,
            size: 0, mtime: 1,
          });
          const listings = {
            '': [entry('alpha', true), entry('beta', true)],
            'alpha': [entry('alpha/deep', true), entry('alpha/a.txt', false)],
            'alpha/deep': [entry('alpha/deep/x.txt', false)],
            'beta': [entry('beta/b.txt', false)],
          };
          const calls = [];
          window.fetch = (url, init = {}) => {
            const parsed = new URL(String(url), location.origin);
            if (parsed.pathname === '/api/files/list') {
              const path = parsed.searchParams.get('path') || '';
              calls.push(path);
              return Promise.resolve(new Response(JSON.stringify({
                entries: listings[path] || [], truncated: false,
              }), {status: 200, headers: {'Content-Type': 'application/json'}}));
            }
            return realFetch(url, init);
          };
          app.visible = [
            {...entry('alpha', true), depth: 0},
            {...entry('alpha/deep', true), depth: 1},
            {...entry('alpha/deep/x.txt', false), depth: 2},
            {...entry('alpha/a.txt', false), depth: 1},
            {...entry('beta', true), depth: 0},
            {...entry('beta/b.txt', false), depth: 1},
          ];
          app.expanded = new Set(['alpha', 'alpha/deep', 'beta']);
          try {
            const ok = await app.reloadTree();
            return {
              ok,
              expanded: Array.from(app.expanded).sort(),
              paths: app.visible.map(n => n.path),
              calls: calls.sort(),
            };
          } finally {
            window.fetch = realFetch;
          }
        }"""
    )
    assert result == {
        "ok": True,
        "expanded": ["alpha", "alpha/deep", "beta"],
        "paths": [
            "alpha", "alpha/deep", "alpha/deep/x.txt", "alpha/a.txt",
            "beta", "beta/b.txt",
        ],
        "calls": ["", "alpha", "alpha/deep", "beta"],
    }


def test_directory_remap_and_delete_update_descendant_preview_state(
        page: Page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          app.editing = false;
          app.tabs = [
            {path: 'dir/a.txt', name: 'a.txt', preview: false},
            {path: 'dir/sub/b.txt', name: 'b.txt', preview: false},
            {path: 'other.txt', name: 'other.txt', preview: false},
          ];
          app.selected = 'dir/sub/b.txt';
          app.treeFocusPath = 'dir/a.txt';
          app.selectedPaths = new Set(['dir/a.txt', 'dir/sub/b.txt']);
          app.fileClipboard = {path: 'dir/a.txt', name: 'a.txt'};
          app._previewNeedsReload = 'dir/sub/b.txt';
          const moved = app._remapPreviewPaths('dir', 'renamed');
          const remapped = {
            moved,
            tabs: app.tabs.map(t => t.path),
            selected: app.selected,
            focus: app.treeFocusPath,
            picked: Array.from(app.selectedPaths).sort(),
            clipboard: app.fileClipboard.path,
            needsReload: app._previewNeedsReload,
          };
          const realOpen = app.openFile;
          app.openFile = async (node) => {
            app.selected = node.path;
            app.previewMode = 'text';
            return true;
          };
          try {
            await app._dropPreviewPathsUnder(['renamed']);
          } finally {
            app.openFile = realOpen;
          }
          return {
            remapped,
            remaining: app.tabs.map(t => t.path),
            selectedAfterDelete: app.selected,
            pickedAfterDelete: Array.from(app.selectedPaths),
            needsReloadAfterDelete: app._previewNeedsReload,
          };
        }"""
    )
    assert result["remapped"] == {
        "moved": True,
        "tabs": ["renamed/a.txt", "renamed/sub/b.txt", "other.txt"],
        "selected": "renamed/sub/b.txt",
        "focus": "renamed/a.txt",
        "picked": ["renamed/a.txt", "renamed/sub/b.txt"],
        "clipboard": "renamed/a.txt",
        "needsReload": "renamed/sub/b.txt",
    }
    assert result["remaining"] == ["other.txt"]
    assert result["selectedAfterDelete"] == "other.txt"
    assert result["pickedAfterDelete"] == []
    assert result["needsReloadAfterDelete"] == ""


def test_save_keeps_edits_typed_while_write_is_in_flight(page: Page,
                                                          backend_url,
                                                          auth_token):
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const realFetch = window.fetch;
          const realMount = app.mountCM;
          app.mountCM = () => {};
          app.tabs = [{path: 'typing.txt', name: 'typing.txt', preview: false}];
          app.selected = 'typing.txt';
          app.previewMode = 'text';
          app.previewLang = 'plaintext';
          app.rawText = 'disk-before';
          app.editText = 'sent-version';
          app._cm = null;
          app.editing = true;
          let finish;
          let sentBody = null;
          window.fetch = (url, init = {}) => {
            if (String(url).includes('/api/files/write')) {
              sentBody = JSON.parse(init.body);
              return new Promise(resolve => { finish = () => resolve(new Response('{}', {status: 200})); });
            }
            return realFetch(url, init);
          };
          try {
            const saving = app.saveEdit();
            while (!finish) await new Promise(r => setTimeout(r, 0));
            app.editText = 'typed-after-click';
            finish();
            await saving;
            return {
              sentBody, editing: app.editing, rawText: app.rawText,
              editText: app.editText, dirty: app.cmStatus.dirty,
            };
          } finally {
            window.fetch = realFetch;
            app.mountCM = realMount;
            app.editing = false;
          }
        }"""
    )
    assert result["sentBody"] == {"path": "typing.txt", "content": "sent-version"}
    assert result["editing"] is True
    assert result["rawText"] == "sent-version"
    assert result["editText"] == "typed-after-click"
    assert result["dirty"] is True


def test_reopening_current_file_does_not_discard_editor_buffer(page: Page,
                                                               backend_url,
                                                               auth_token):
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          app.tabs = [{path: 'draft.txt', name: 'draft.txt', preview: true}];
          app.selected = 'draft.txt';
          app.treeFocusPath = 'draft.txt';
          app.previewMode = 'text';
          app.rawText = 'saved';
          app.editText = 'unsaved draft';
          app.editing = true;
          app._cm = null;
          app.cmStatus = {...app.cmStatus, dirty: true};
          let confirms = 0;
          const realConfirm = window.confirm;
          window.confirm = () => { confirms += 1; return true; };
          try {
            const ok = await app.openFile(
              {path: 'draft.txt', name: 'draft.txt'}, {preview: false});
            return {
              ok, confirms, editing: app.editing, editText: app.editText,
              dirty: app.cmStatus.dirty,
              pinned: app.tabs.find(t => t.path === 'draft.txt')?.preview === false,
            };
          } finally {
            window.confirm = realConfirm;
            app.editing = false;
          }
        }"""
    )
    assert result == {
        "ok": True,
        "confirms": 0,
        "editing": True,
        "editText": "unsaved draft",
        "dirty": True,
        "pinned": True,
    }


def test_upload_completion_keeps_text_typed_after_overwrite_confirmation(
        page: Page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          app.tabs = [{path: 'upload.txt', name: 'upload.txt', preview: false}];
          app.selected = 'upload.txt';
          app.previewMode = 'text';
          app.rawText = 'disk-before';
          app.editText = 'confirmed-version';
          app.editing = true;
          app._cm = null;
          app.cmStatus = {...app.cmStatus, dirty: true};
          const realConfirm = window.confirm;
          const realOpen = app.openFile;
          window.confirm = () => true;
          try {
            const context = app._prepareUploadOverwrite('', [
              {name: 'upload.txt'},
            ]);
            app.editText = 'typed-during-upload';
            await app._syncUploadedFiles([{
              status: 'fulfilled',
              value: {path: 'upload.txt', replaced_trash_id: null},
            }], context);
            const preserved = {
              editing: app.editing, editText: app.editText,
              dirty: app.cmStatus.dirty,
              needsReload: app._previewNeedsReload,
            };
            let reload = null;
            app.openFile = async (node, opts) => {
              reload = {path: node.path, forceReload: !!opts.forceReload};
              return true;
            };
            await app.toggleEdit();
            return {
              preserved, editingAfterExit: app.editing, reload,
            };
          } finally {
            window.confirm = realConfirm;
            app.openFile = realOpen;
            app.editing = false;
          }
        }"""
    )
    assert result == {
        "preserved": {
            "editing": True,
            "editText": "typed-during-upload",
            "dirty": True,
            "needsReload": "upload.txt",
        },
        "editingAfterExit": False,
        "reload": {"path": "upload.txt", "forceReload": True},
    }
