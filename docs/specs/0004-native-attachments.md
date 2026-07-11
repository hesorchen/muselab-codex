# Codex-native attachments

- **Status:** Implemented
- **Scope:** Phase 2, first slice
- **Protocol baseline:** `codex-cli 0.144.1` stable v2 schema

## Goal

Keep the browser upload experience while removing its dependency on the former
chat module and process-global base64 store. Uploaded files must be
available to `codex app-server` as stable workspace-local paths and remain
visible after a browser refresh.

## Protocol mapping

The version-matched app-server schema exposes two stable user-input shapes used
by this slice:

| Browser attachment | `turn/start.input` |
|---|---|
| PNG, JPEG, GIF, or WebP | `{ "type": "localImage", "path": "..." }` |
| PDF, UTF-8 text/code, or XLSX | `{ "type": "mention", "name": "...", "path": "..." }` |

Text remains a separate `{ "type": "text", "text": "..." }` input. An
attachment-only turn is valid; an empty turn with neither text nor attachments
is rejected.

## Storage and lifecycle

Uploads are written atomically below:

```text
$MUSELAB_ROOT/.muselab-codex/attachments/
├── staged/                 # uploaded, not yet sent
└── threads/<thread-id>/    # claimed by one Codex thread
```

The one-time SSE ticket carries only opaque attachment ids. Redeeming the
ticket moves each staged file into its thread directory before `turn/start`.
Retries on the same thread reuse the claimed file; another thread cannot claim
it. Deleting the Codex thread removes its attachment directory.

Codex remains the transcript source of truth. On `thread/read`, persisted
`localImage` inputs are mapped back to the inherited `images` UI field. Real
`codex-cli 0.144.1` validation showed that app-server accepts document
`mention` inputs but omits them from the materialized user-message content. The
turn therefore also sets `clientUserMessageId`; muselab stores a small sidecar
under the same thread attachment directory that maps this stable client id to
document display metadata. It contains names and kinds, never document bytes or
prompt text. Only references that resolve inside the matching thread's
attachment directory are exposed.

## Security limits

- upload and download endpoints require `MUSELAB_TOKEN`;
- each upload is capped at 10 MiB and each plain-text file at 200 KiB;
- a turn accepts at most eight unique attachment ids;
- image types are detected from file signatures, not trusted MIME headers;
- text files must be valid UTF-8;
- user filenames are reduced to a basename and stripped of control characters;
- the attachment root, thread directories, and served paths are checked against
  the resolved workspace boundary, including symlink escape attempts;
- protocol inputs contain local paths, never base64 payloads or data URLs.

## Deferred work

Server-backed message queues are still a legacy-only feature, so queued native
turns with attachments are outside this slice. Staged-upload expiry and a UI for
storage usage can be added when the native queue or storage-settings slice is
migrated.
