# Session internals

> [简体中文](backend-sessions_zh.md)

This page describes how muselab stores and manages chat sessions: the two-layer
store shared with the Claude CLI, the session index and sidecar files, the
server-side message queue, attachment handling, session forking, and restart
recovery.

Related reading: [Architecture](architecture.md) · [Data & backup](data-and-backup.md) · [Scheduler](scheduler.md)

---

## 1. Two-layer store & ownership

Every session is jointly owned by **the Claude CLI** and **muselab**. The two
layers never overlap — each owns a distinct set of facts.

```
~/.claude/projects/<cwd-key>/          CLI owns this tree
└── <sid>.jsonl                        ← transcript (messages, tool calls, compact boundaries)

muselab/sessions/                      muselab owns this tree
├── index.json                         ← session list + display metadata
├── <sid>.sidecar.json                 ← per-message annotations + context meter
├── <sid>.queue.json                   ← server-side message queue (only when non-empty)
└── active_turns/<sid>.json            ← in-flight turn sentinel (deleted on clean finish)

$MUSELAB_ROOT/.muselab-attach/<sid>/   muselab owns this tree (in the archive)
└── <original-filename>                ← persisted attachment originals
```

`<cwd-key>` is derived via the SDK's `project_key_for_directory(ROOT)` — for
example `/home/alice/archive` → `-home-alice-archive`.
([`backend/chat.py:L99-L113`](../backend/chat.py#L99-L113))

For third-party vendor models (DeepSeek, GLM, MiniMax, Kimi, Qwen, MiMo) the
CLI uses an isolated config dir under `/tmp/muselab-vendor-cli-config-<uid>/`
instead of `~/.claude/`. ([`backend/chat.py:L69-L97`](../backend/chat.py#L69-L97))

**What lives where:**

| Fact | Owner | Location |
|------|-------|----------|
| Conversation transcript (messages, tool calls, compact boundaries) | CLI | `~/.claude/projects/<cwd-key>/<sid>.jsonl` |
| `custom_title` / `aiTitle` (Haiku-generated after each turn) | CLI | same JSONL |
| `last_modified`, `created_at`, `first_prompt`, `tag` | CLI | same JSONL |
| Session display name, `model`, `system_prompt`, `auto_named`, `pinned`, `effort`, `thinking` | muselab | `sessions/index.json` |
| Sessions created in the UI but not yet sent | muselab | `sessions/index.json` only (no JSONL exists yet) |
| Per-message cost, model badge, timestamps, attachment refs | muselab | `sessions/<sid>.sidecar.json` |
| Server-side pending messages | muselab | `sessions/<sid>.queue.json` |
| Attachment originals (full-res) | muselab | `$MUSELAB_ROOT/.muselab-attach/<sid>/` |

The **same UUID** (`sid`) is the key in every layer — there is no translation
table. ([`backend/sessions.py:L80-L81`](../backend/sessions.py#L80-L81),
[`backend/chat.py:L116-L127`](../backend/chat.py#L116-L127))

---

## 2. The session index

`sessions/index.json` is a JSON array. Each entry represents one session and
carries the facts the CLI does not track.
([`backend/sessions.py:L75-L77`](../backend/sessions.py#L75-L77))

| Field | Type | Notes |
|-------|------|-------|
| `id` | string (UUID v4) | Primary key; matches the CLI JSONL filename |
| `name` | string | Display name; may be a first-line snippet or user-set title |
| `model` | string | Empty string means "use the configured default" |
| `system_prompt` | string | Per-session override; empty = muselab default |
| `created_at` | float (Unix seconds) | SDK's `created_at` is in ms and divided by 1000 during merge |
| `updated_at` | float (Unix seconds) | Derived from `last_modified` (ms→s); bumped by every `bump_session()` call |
| `message_count` | int | All SDK message frames; updated after each turn |
| `turn_count` | int | User-typed prompts only (excludes tool-result sidechain frames) |
| `auto_named` | bool | `true` until the user renames or a substantive first message becomes the title |
| `pinned` | bool | Pinned sessions sort to top; not stored in the CLI JSONL |
| `effort` | string | `""` / `"low"` / `"medium"` / `"high"` / `"xhigh"` / `"max"`; empty = SDK adaptive |
| `thinking` | bool | Extended thinking on/off; defaults to `true` |

([`backend/sessions.py:L194-L238`](../backend/sessions.py#L194-L238),
[`backend/sessions.py:L342-L374`](../backend/sessions.py#L342-L374))

`tag` and `first_prompt` are **not** stored in `index.json`; they are merged
from `SDKSessionInfo` at read time.
([`backend/sessions.py:L228-L229`](../backend/sessions.py#L228-L229))

`list_sessions()` merges the SDK's JSONL scan with `index.json`. Sessions that
exist only in `index.json` (no JSONL yet) are appended at the end so they
appear in the picker immediately after creation.
([`backend/sessions.py:L318-L334`](../backend/sessions.py#L318-L334))

The list is cached for 30 seconds (`_LIST_CACHE_TTL_S = 30.0`). Any
muselab-internal mutation (create, rename, delete, pin, bump) calls
`invalidate_sessions_cache()` immediately via `_save_index`, so UI-driven
changes are visible at once; only external `claude --resume` writes wait for
the TTL. ([`backend/sessions.py:L129-L163`](../backend/sessions.py#L129-L163))

---

## 3. Sidecar files

Each session has up to three files in `sessions/`:

| Filename | Purpose |
|----------|---------|
| `<sid>.sidecar.json` | Per-message annotations: cost, model badge, timestamps, attachment refs, context meter |
| `<sid>.queue.json` | Server-side message queue (only present when non-empty or paused) |
| `<sid>.json` | **Legacy** — pre-2026-05-17 full transcript store; no longer written |

([`backend/sessions.py:L80-L81`](../backend/sessions.py#L80-L81),
[`backend/sessions.py:L761-L762`](../backend/sessions.py#L761-L762),
[`backend/sessions.py:L26`](../backend/sessions.py#L26))

### Sidecar top-level schema

| Key | Type | Description |
|-----|------|-------------|
| `messages` | object (UUID → annotation) | Per-message annotations keyed by message UUID matching the CLI JSONL |
| `context_max_tokens` | int or null | SDK-measured `maxTokens` for the context meter denominator; persisted so it survives restarts without a live turn |
| `pending_attachments` | array | Transient upload queue before message UUID is known (see §5) |

([`backend/sessions.py:L174-L183`](../backend/sessions.py#L174-L183),
[`backend/sessions.py:L576-L607`](../backend/sessions.py#L576-L607))

### Per-message annotation fields

Each entry in `messages` is keyed by the **message UUID** from the CLI JSONL:

| Field | Attached to | Description |
|-------|-------------|-------------|
| `cost` | assistant turn | Per-turn USD cost computed from `ResultMessage` token counts |
| `model` | assistant turn | Model ID that produced this reply (shown as a badge in the UI) |
| `ts` | assistant turn | Unix seconds when the annotation was written |
| `elapsed_s` | assistant turn | Wall-clock seconds the turn took to stream |
| `images` | user message | Uploaded images consumed by this message (thumbnail URL; base64 not stored here) |
| `docs` | user message | Uploaded documents consumed by this message |

([`backend/sessions.py:L556-L573`](../backend/sessions.py#L556-L573))

The sidecar is written **only** for annotations — the transcript itself is the
CLI JSONL's job. After every turn, `chat.py` calls `bump_session()` (index
update) and `set_message_annotation()` (sidecar update) independently.
([`backend/sessions.py:L19-L23`](../backend/sessions.py#L19-L23))

All sidecar reads and writes are serialized by `_SIDECAR_LOCK` to prevent
lost updates when two operations interleave in FastAPI's thread pool.
All index reads and writes are serialized by `_INDEX_LOCK` for the same reason.
All writes use `atomic_write_text()` (temp-file + `os.replace()`) so no file
is ever torn by a crash mid-write.
([`backend/sessions.py:L101-L123`](../backend/sessions.py#L101-L123))

---

## 4. The message queue

muselab maintains a **server-side FIFO message queue** per session. When the
user submits a prompt while a turn is already in progress, the new message is
enqueued rather than dropped. The drain loop pops the head and starts the next
turn automatically when the current one finishes — even if no browser is
attached.

([`backend/sessions.py:L738-L757`](../backend/sessions.py#L738-L757),
[`backend/chat.py:L6633`](../backend/chat.py#L6633),
[`backend/chat.py:L6739-L6770`](../backend/chat.py#L6739-L6770))

### `<sid>.queue.json` shape

```json
{
  "items": [
    {
      "id": "q-<8-hex>",
      "text": "<user message text>",
      "image_ids": "<comma-separated upload ids>",
      "enqueued_at": 1718000000000
    }
  ],
  "paused": false
}
```

([`backend/sessions.py:L747-L756`](../backend/sessions.py#L747-L756),
[`backend/sessions.py:L800-L816`](../backend/sessions.py#L800-L816))

### Queue mechanics

- **Max depth:** 10 items (`_QUEUE_MAX = 10`). An 11th enqueue returns
  `{"ok": false, "error": "queue_full"}`.
  ([`backend/sessions.py:L758`](../backend/sessions.py#L758),
  [`backend/sessions.py:L806-L807`](../backend/sessions.py#L806-L807))
- **FIFO drain:** `dequeue_message()` pops the head; `reorder_queue()` allows
  reordering before drain.
  ([`backend/sessions.py:L819-L880`](../backend/sessions.py#L819-L880))
- **Auto-pause on error:** The `paused` flag is set to `true` when a queued
  turn errors, times out, or is cancelled. Auto-drain stops until the user
  resumes. ([`backend/sessions.py:L858-L865`](../backend/sessions.py#L858-L865))
- **File lifecycle:** The queue file is deleted when `items` is empty and
  `paused` is `false`, to avoid accumulating empty files.
  ([`backend/sessions.py:L783-L791`](../backend/sessions.py#L783-L791))
- **Re-queue on race loss:** If the drain trigger loses a concurrency race or
  fails to start a turn, the item is re-inserted at the head via `requeue_head`
  so nothing is silently dropped.
  ([`backend/sessions.py:L831-L840`](../backend/sessions.py#L831-L840))
- **Attachment TTL caveat:** `image_ids` in queue items reference the
  in-memory `_image_store` (10-minute TTL). If a queued turn is delayed past
  that TTL, its attachments are sent as text-only.
  ([`backend/sessions.py:L754-L757`](../backend/sessions.py#L754-L757))

---

## 5. Attachments

### In-memory staging

Uploads land in an in-memory store before they are bound to a message:

1. `POST /api/chat/upload-image` receives a multipart file.
2. The file is classified as `image` (png/jpg/gif/webp), `pdf`, `text`, or
   `xlsx`.
3. Stored in `_image_store[aid]` with a randomly generated `aid`.
4. TTL: **10 minutes** (`_IMAGE_TTL_S = 600`).
   ([`backend/chat.py:L4647-L4648`](../backend/chat.py#L4647-L4648))
5. Budget caps: max 48 entries or 256 MB total; oldest evicted first.
   Per-file max: 10 MB raw; text files max 200 KB.
   ([`backend/chat.py:L4649-L4680`](../backend/chat.py#L4649-L4680))

**All staged uploads are lost on backend restart** — the in-memory store is
not persisted. Re-attaching is the expected recovery path.

### Persistence to the archive

When an upload is consumed by a turn, the original file is saved to disk:

```
$MUSELAB_ROOT/.muselab-attach/<sid>/<original-filename>
```

This survives restarts and is served via
`GET /api/chat/sessions/{sid}/attachment/{filename}?token=...` for lightbox
display. ([`backend/chat.py:L1479-L1493`](../backend/chat.py#L1479-L1493))

### Pending attachment queue (pre-UUID binding)

The SDK writes the user-message JSONL record asynchronously, so the message
UUID is not known at upload time. muselab uses a `pending_attachments` list in
the sidecar as a staging area:

1. At upload time, `append_pending_attachments(sid, images, docs)` appends a
   `{"ts", "images", "docs"}` bundle. Max 50 entries; entries older than 24
   hours are pruned on each call.
   ([`backend/sessions.py:L622-L661`](../backend/sessions.py#L622-L661))
2. When `GET /sessions/{sid}` encounters a user message with inline image refs
   but no annotation, `consume_one_pending_attachments(sid, msg_uuid)` pops the
   oldest bundle and promotes it to a permanent `messages[msg_uuid]` annotation.
   ([`backend/sessions.py:L664-L686`](../backend/sessions.py#L664-L686))

After binding, the user-message annotation holds:

```json
{
  "images": [{"thumb": "<160px base64>", "url": "/api/chat/sessions/<sid>/attachment/<filename>?token=..."}],
  "docs":   [{"name": "<filename>", "text": "<content>"}]
}
```

---

## 6. Fork & edit-a-message

`POST /api/chat/sessions/{sid}/fork` creates a branch from any point in the
conversation. ([`backend/chat.py:L3898-L3933`](../backend/chat.py#L3898-L3933))

1. The SDK's `fork_session()` copies the CLI JSONL transcript up to the
   specified `up_to_message_id` into a new JSONL with a fresh `new_sid` and
   new message UUIDs.
2. muselab calls `register_session(new_sid, ...)` to add the fork to
   `index.json`. The fork inherits `model` and `system_prompt` from the source.
   ([`backend/sessions.py:L342-L374`](../backend/sessions.py#L342-L374))
3. The fork is immediately visible in the session picker.

The primary use-case is **message editing**: when the user edits a previous
message, the UI forks at the preceding assistant message and then re-sends the
revised text into the new branch.

---

## 7. Restart recovery

### Active-turn sidecars

At turn-start, a small sentinel file is written to
`sessions/active_turns/<sid>.json`:

```json
{
  "sid": "<session-id>",
  "user_text": "<full user prompt>",
  "user_text_preview": "<first line, max 200 chars>",
  "model": "<model id>",
  "started_at": 1718000000.0
}
```

The file is deleted on clean turn completion (success, error, or timeout). If
muselab is killed mid-turn, the file persists.

([`backend/chat.py:L513-L606`](../backend/chat.py#L513-L606))

### Startup scan

At process startup, `_scan_interrupted_turns_at_startup()` reads any leftover
`active_turns/*.json` files and stores them in `_interrupted_at_startup`. On
the next browser connection, a **toast** surfaces for each unfinished turn,
showing the prompt preview and model. The user decides whether to re-send.

**muselab deliberately does not auto-resume.** Auto-resuming would spend tokens
on prompts the user may have already abandoned or decided to rephrase.
([`backend/chat.py:L582-L606`](../backend/chat.py#L582-L606))

### Other restart behavior

| State | Behavior |
|-------|----------|
| Session list | Rebuilt from `index.json` + SDK JSONL scan; no warm-up needed |
| Context meter denominator | Persisted in `context_max_tokens` inside each sidecar; correct immediately without a live turn ([`backend/sessions.py:L576-L607`](../backend/sessions.py#L576-L607)) |
| Staged uploads (`_image_store`) | **Lost** — in-memory only; user re-attaches |
| Pending queue items | Persisted in `<sid>.queue.json`; drain resumes once a turn is sent |
| List cache | Cold on restart; first request pays the full JSONL scan (~400 ms on large archives); subsequent calls hit the 30 s TTL cache |

---

## File layout summary

```
muselab/sessions/
├── index.json                   session list (names, model, pinned, …)
├── <sid>.sidecar.json           per-message cost / model / attachments + context meter
├── <sid>.queue.json             server-side queue (absent when empty & not paused)
└── active_turns/
    └── <sid>.json               in-flight turn sentinel (deleted on clean finish)

~/.claude/projects/<cwd-key>/
└── <sid>.jsonl                  transcript — CLI's sole property

$MUSELAB_ROOT/.muselab-attach/
└── <sid>/
    └── <filename>               persisted attachment originals
```

See [Data & backup](data-and-backup.md) for which of these paths to include
in a backup and how to restore them on a new machine.
