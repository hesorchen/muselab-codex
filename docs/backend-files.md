# Files API

> [简体中文](backend-files_zh.md)

The Files API (`/api/files/*`) is the sole filesystem surface muselab exposes to
the browser. Every read, write, search, and deletion goes through this router —
the archive root (`MUSELAB_ROOT`) is the only directory it can touch, and
whole-file operations (not streaming byte-range) are the unit of exchange. No
direct filesystem path is ever handed to the browser; every path string is
validated and resolved through [`safe_resolve`](#safe_resolve-in-depth) before
any OS call happens.

Authentication details — token format, timing-safe comparison, token scrubbing
from access logs — are covered in [backend-security.md](backend-security.md).

---

## Endpoint reference

All 18 endpoints share the prefix `/api/files` ([`backend/files.py:L16`](../backend/files.py#L16)).
Auth variants: **header** = `X-Auth-Token` header only; **query** = `?token=`
query param only. The query-param variant exists for browser contexts where
custom headers cannot be sent (`<img src>`, `<iframe src>`).

### Read / Inspect

| Method | Path | Purpose | Auth |
|--------|------|---------|------|
| `GET` | `/api/files/list` | List directory entries. Params: `path`, `show_hidden`. Returns `{root, path, entries[], truncated}`. Capped at 500 entries; trash dir always excluded even when `show_hidden=true`. ([`L360–L393`](../backend/files.py#L360)) | header |
| `GET` | `/api/files/read` | Read a text file as `text/plain`. Rejects known-binary extensions and files whose first 4 KB contains >5% non-UTF-8 bytes. Size cap: 2 MB. ([`L583–L618`](../backend/files.py#L583)) | header |
| `GET` | `/api/files/stat` | Lightweight metadata for a single path: `{path, name, is_dir, size, mtime}`. ([`L621–L644`](../backend/files.py#L621)) | header |
| `GET` | `/api/files/raw` | Stream a raw file for in-browser rendering. Images/PDF/media served inline; HTML/SVG served inline with a strict CSP; everything else forced to `application/octet-stream` attachment. `Cache-Control: no-cache` to force conditional GETs. ([`L656–L709`](../backend/files.py#L656)) | query |
| `GET` | `/api/files/download` | Force-download any file as `application/octet-stream` with `Content-Disposition: attachment`. ([`L712–L721`](../backend/files.py#L712)) | query |
| `GET` | `/api/files/xlsx` | Read-only XLSX preview as structured JSON. Sheets ≤ 20, rows ≤ 500, cols ≤ 50; cells clipped to 500 chars. Uses `openpyxl` loaded on demand. ([`L406–L471`](../backend/files.py#L406)) | header |
| `GET` | `/api/files/csv` | Paginated CSV/TSV preview. Params: `offset`, `limit` (default 200, max 1000). Returns sniffed delimiter, header row, and `total_rows`. ([`L485–L572`](../backend/files.py#L485)) | header |
| `GET` | `/api/files/grep` | Full-text search (pure Python, no shell dependency). Min query: 2 chars; time budget: 8 s; per-file cap: 1 MB. Returns `{hits[], truncated}` with `{path, name, line, snippet}` per hit. ([`L1193–L1265`](../backend/files.py#L1193)) | header |
| `GET` | `/api/files/search` | Filename/dirname substring search (no file content read). Returns `{entries[], truncated}`. ([`L1268–L1300`](../backend/files.py#L1268)) | header |

### Write / Mutate

| Method | Path | Purpose | Auth |
|--------|------|---------|------|
| `PUT` | `/api/files/write` | Overwrite or create a file. Body: `{path, content}`. Atomic via tmpfile + rename. Write cap: 10 MB. Refuses to target the trash directory. ([`L736–L762`](../backend/files.py#L736)) | header |
| `POST` | `/api/files/upload` | Multipart upload. Form fields: `path` (target dir), `file`. Strips to basename, blocks dangerous suffixes (`.exe`, `.dll`, `.so`, `.dylib`, `.scr`, `.ps1`, etc.), blocks sensitive filenames, cap: 100 MB (override via `MUSELAB_MAX_UPLOAD_MB`). Same-name existing file is soft-deleted to trash before rename. ([`L776–L849`](../backend/files.py#L776)) | header |
| `POST` | `/api/files/mkdir` | Create a directory (parents=True). ([`L983–L988`](../backend/files.py#L983)) | header |
| `POST` | `/api/files/rename` | Move/rename a file or directory. Body: `{src, dst}`. 404 if source missing; 409 if destination exists. ([`L996–L1008`](../backend/files.py#L996)) | header |
| `POST` | `/api/files/copy-bak` | Duplicate a file as `<name>.bak` (or `.bak.2`, `.bak.3` … up to 999). Files only; server derives the destination name. ([`L1047–L1086`](../backend/files.py#L1047)) | header |
| `DELETE` | `/api/files/delete` | Soft-delete by default (moves to `.muselab-dustbin/`). `?permanent=true` for hard delete. Refuses to target the trash dir itself. ([`L856–L889`](../backend/files.py#L856)) | header |

### Trash management

| Method | Path | Purpose | Auth |
|--------|------|---------|------|
| `GET` | `/api/files/trash/list` | List all trash items newest-first. Returns `{items[], total_size, ttl_days}`. ([`L895–L909`](../backend/files.py#L895)) | header |
| `POST` | `/api/files/trash/restore` | Restore one item to its original path. 409 if destination occupied. Body: `{trash_id}`. ([`L916–L950`](../backend/files.py#L916)) | header |
| `DELETE` | `/api/files/trash/purge` | Permanently delete one trash item (manifest + payload). Body: `{trash_id}`. ([`L953–L962`](../backend/files.py#L953)) | header |
| `DELETE` | `/api/files/trash/empty` | Permanently delete all trash items. ([`L965–L976`](../backend/files.py#L965)) | header |

---

## `safe_resolve` in depth

**Signature:** `safe_resolve(rel: str, allow_sensitive: bool = False) -> Path`
([`L316–L346`](../backend/files.py#L316))

Every endpoint calls `safe_resolve` before any filesystem operation. It provides
three independent layers of defense:

### 1 — `..` traversal blocking

`(ROOT / rel).resolve()` canonicalizes the path, following symlinks and
collapsing `..` components. The result is then checked to be a descendant of
`ROOT.resolve()` ([`L339–L341`](../backend/files.py#L339)). A request for
`../../etc/passwd` resolves to `/etc/passwd`, which is not under ROOT, and is
rejected with HTTP 400 `"path escapes root"`.

### 2 — Symlink escape blocking

Because `.resolve()` follows symlinks, a symlink inside ROOT pointing to
`/etc/shadow` has its real target path checked against ROOT — the link name
alone is not trusted ([`L333–L341`](../backend/files.py#L333)). The same guard
runs independently in the grep endpoint, where each candidate file is resolved
before being opened and both the link name and the resolved target are checked
([`L1229–L1237`](../backend/files.py#L1229)).

### 3 — Sensitive-filename blocklist

`_is_sensitive()` ([`L305–L313`](../backend/files.py#L305)) is checked against
two sets and one prefix rule ([`L286–L313`](../backend/files.py#L286)):

**`SENSITIVE_NAMES`** (exact basename, case-insensitive):
`.env`, `.env.local`, `.env.production`, `.env.development`, `.netrc`,
`.pgpass`, `.npmrc`, `.pypirc`, `.dockercfg`, `.htpasswd`, `.htaccess`,
`credentials`, `credentials.json`, `service-account.json`, `id_rsa`, `id_dsa`,
`id_ecdsa`, `id_ed25519`, `authorized_keys`, `known_hosts`, `.bash_history`,
`.zsh_history`, `.python_history`, `.node_repl_history`, `.sqlite_history`,
`.lesshst`, `.viminfo`, `.wget-hsts`, `.npm-debug.log`, `.yarn-error.log`

**`SENSITIVE_SUFFIX`** (extension-based):
`.pem`, `.key`, `.p12`, `.pfx`, `.keystore`, `.jks`, `.env`

**Prefix rule:** any filename starting with `.env.` is blocked regardless of the
rest of the name ([`L309`](../backend/files.py#L309)).

Blocked paths return HTTP 403. The check applies whether or not the file
actually exists on disk, so the API can neither read nor create credential-shaped
paths.

**`allow_sensitive=True`** is used only in two places: `trash_restore`
([`L936`](../backend/files.py#L936)) so a previously-deleted `.env` can be moved
back out of trash, and in `copy_bak` for the final destination path
([`L1070`](../backend/files.py#L1070)).

**NUL byte injection** is rejected before any path operations ([`L331–L332`](../backend/files.py#L331)):
a NUL in the path string raises HTTP 400 early.

---

## Trash semantics

Deleting a file moves it to `.muselab-dustbin/` under `MUSELAB_ROOT` rather than
unlinking it. The trash directory is invisible to `/list`, `/search`, and `/grep`
at all times — it surfaces only through the dedicated `/trash/*` endpoints.

**Trash item layout** ([`L18–L31`](../backend/files.py#L18)):

```
<ROOT>/.muselab-dustbin/<trash_id>.json   ← manifest
<ROOT>/.muselab-dustbin/<trash_id>        ← payload (file or dir)
```

`trash_id` format: `<unix_ts>_<8-hex>` — sortable, collision-resistant.

**`_guard_not_trash()`** ([`L39–L53`](../backend/files.py#L39)) prevents all
write, upload, rename, and copy-bak operations from targeting the dustbin
directly. Only the `/trash/*` endpoints can modify its contents.

**`trash_id` format validation** ([`L62–L80`](../backend/files.py#L62)) uses
regex `^\d{1,20}_[0-9a-f]{8}$` on every restore/purge call to block path
traversal payloads (e.g. `../../etc/passwd` as a `trash_id`).

**Auto-expiry:** configurable via `MUSELAB_TRASH_TTL_DAYS` (default 30 days;
`0` = disabled). Runs as a background task at startup. See
[configuration.md](configuration.md) for the env var reference.

**Restore conflict:** if the original path is occupied when restoring, the
endpoint returns HTTP 409. The user must rename or clear the occupying item
first.

---

## Special renderers: xlsx and csv

The preview pane needs structured data, not raw bytes, to render spreadsheets
and CSV files as interactive tables. Two dedicated endpoints serve this need:

**`/api/files/xlsx`** ([`L406–L471`](../backend/files.py#L406)) parses XLSX
files with `openpyxl` (`read_only=True, data_only=True`) and returns JSON with
per-sheet row arrays. Formulas are not re-evaluated — only the cached value from
the last save is returned. Limits exist to keep the response size bounded: up to
20 sheets, 500 rows, 50 columns, and 500 characters per cell.

**`/api/files/csv`** ([`L485–L572`](../backend/files.py#L485)) sniffs the
delimiter, detects the header row, and returns a paginated window (`offset` +
`limit`). The pagination avoids loading large CSV files into memory: the
endpoint streams through the file to the requested offset on each call.

Both endpoints are consumed exclusively by the preview pane in the frontend.
See [frontend.md](frontend.md) for how the pane selects the renderer based on
file extension.

---

*See also:* [architecture.md](architecture.md) · [backend-security.md](backend-security.md) · [configuration.md](configuration.md)
