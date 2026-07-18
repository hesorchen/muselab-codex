"""Codex-native attachment persistence and path-boundary tests."""

from io import BytesIO
import json

import pytest
from fastapi import HTTPException, UploadFile
from starlette.datastructures import Headers

from backend.codex.attachments import CodexAttachmentService


def _upload(name: str, body: bytes, mime: str) -> UploadFile:
    return UploadFile(
        file=BytesIO(body),
        filename=name,
        headers=Headers({"content-type": mime}),
    )


@pytest.mark.asyncio
async def test_upload_prepare_and_restore_history(tmp_path):
    service = CodexAttachmentService(tmp_path)
    uploaded = await service.upload(_upload(
        "../screen.png", b"\x89PNG\r\n\x1a\nimage", "image/png"))

    prepared = service.prepare("thread-1", uploaded["id"])

    assert uploaded["name"] == "screen.png"
    assert prepared.inputs[0]["type"] == "localImage"
    image_path = prepared.inputs[0]["path"]
    assert str(tmp_path.resolve()) in image_path
    images, docs = service.history_items("thread-1", prepared.inputs)
    assert images == prepared.images
    assert docs == []
    resolved, mime = service.resolve("thread-1", image_path.rsplit("/", 1)[-1])
    assert resolved.read_bytes().endswith(b"image")
    assert mime == "image/png"

    with pytest.raises(ValueError, match="missing or expired"):
        service.prepare("thread-2", uploaded["id"])


@pytest.mark.asyncio
async def test_staged_attachment_can_be_described_and_previewed_before_queue_drain(
    tmp_path,
):
    service = CodexAttachmentService(tmp_path)
    uploaded = await service.upload(_upload(
        "queue-shot.png", b"\x89PNG\r\n\x1a\nqueued", "image/png"))

    assert service.describe_staged(uploaded["id"]) == [{
        "id": uploaded["id"],
        "kind": "image",
        "name": "queue-shot.png",
        "mime": "image/png",
        "available": True,
    }]
    path, mime = service.resolve_staged_image(uploaded["id"])
    assert path.read_bytes().endswith(b"queued")
    assert mime == "image/png"

    service.prepare("thread-1", uploaded["id"])
    assert service.describe_staged(uploaded["id"])[0]["available"] is False
    with pytest.raises(HTTPException) as missing:
        service.resolve_staged_image(uploaded["id"])
    assert missing.value.status_code == 404


@pytest.mark.asyncio
async def test_document_sidecar_survives_app_server_dropping_mention(tmp_path):
    service = CodexAttachmentService(tmp_path)
    uploaded = await service.upload(_upload(
        "notes.md", b"# notes", "text/markdown"))
    prepared = service.prepare("thread-1", uploaded["id"])

    images, docs = service.history_items(
        "thread-1",
        [{"type": "text", "text": "read this"}],
        prepared.client_user_message_id,
    )

    assert prepared.client_user_message_id is not None
    assert images == []
    assert docs == [{"name": "notes.md", "kind": "text"}]


@pytest.mark.asyncio
async def test_upload_rejects_spoofed_and_non_utf8_files(tmp_path):
    service = CodexAttachmentService(tmp_path)

    with pytest.raises(HTTPException) as spoofed:
        await service.upload(_upload("fake.png", b"not an image", "image/png"))
    assert spoofed.value.status_code == 400

    with pytest.raises(HTTPException) as non_utf8:
        await service.upload(_upload("notes.txt", b"\xff\xfe", "text/plain"))
    assert non_utf8.value.status_code == 400


def test_attachment_root_symlink_cannot_escape_workspace(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    hidden = tmp_path / "workspace" / ".muselab-codex"
    hidden.mkdir(parents=True)
    (hidden / "attachments").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="inside the workspace"):
        CodexAttachmentService(tmp_path / "workspace")


@pytest.mark.asyncio
async def test_queued_attachment_has_one_owner_and_remove_reclaims_bytes(tmp_path):
    service = CodexAttachmentService(tmp_path)
    uploaded = await service.upload(_upload(
        "queued.png", b"\x89PNG\r\n\x1a\nowned", "image/png"))
    service.reserve_for_queue("thread-1", "queue-1", uploaded["id"])

    with pytest.raises(ValueError, match="another queued message"):
        service.prepare(
            "thread-2", uploaded["id"], queue_item_id="queue-2",
            client_user_message_id="a" * 32)
    service.prepare(
        "thread-1", uploaded["id"], queue_item_id="queue-1",
        client_user_message_id="b" * 32)
    assert service.describe_staged(uploaded["id"])[0]["available"] is False
    assert service.release_queue(
        "thread-1", "queue-1", uploaded["id"], "b" * 32) == 1
    assert service.describe_staged(uploaded["id"])[0]["available"] is False


@pytest.mark.asyncio
async def test_attachment_reconcile_adopts_v1_reclaims_orphans_and_quarantines(
    tmp_path,
):
    service = CodexAttachmentService(tmp_path)
    adopted = await service.upload(_upload(
        "adopt.png", b"\x89PNG\r\n\x1a\nadopt", "image/png"))
    orphan = await service.upload(_upload(
        "orphan.png", b"\x89PNG\r\n\x1a\norphan", "image/png"))
    expired = await service.upload(_upload(
        "expired.png", b"\x89PNG\r\n\x1a\nexpired", "image/png"))
    claimed = await service.upload(_upload(
        "claimed.png", b"\x89PNG\r\n\x1a\nclaimed", "image/png"))
    service.reserve_for_queue("thread-2", "queue-2", orphan["id"])
    service.reserve_for_queue("thread-3", "queue-3", claimed["id"])
    service.prepare(
        "thread-3", claimed["id"], queue_item_id="queue-3",
        client_user_message_id="c" * 32)
    expired_meta = service.staged / f"{expired['id']}.json"
    payload = json.loads(expired_meta.read_text())
    payload["created_at"] = 1
    expired_meta.write_text(json.dumps(payload))
    (service.staged / "broken.json").write_text("{broken")

    result = service.reconcile(
        {adopted["id"]: ("thread-1", "queue-1")},
        ttl_seconds=10,
        now=100,
    )

    adopted_meta = json.loads(
        (service.staged / f"{adopted['id']}.json").read_text())
    assert (adopted_meta["queue_owner_thread"],
            adopted_meta["queue_owner_item"]) == ("thread-1", "queue-1")
    assert result["adopted"] == 1
    assert result["reclaimed"] == 2
    assert result["expired"] == 1
    assert result["quarantined"] >= 1
    assert any(service.quarantine.iterdir())


@pytest.mark.asyncio
async def test_thread_delete_reclaims_staged_queue_ownership(tmp_path):
    service = CodexAttachmentService(tmp_path)
    uploaded = await service.upload(_upload(
        "delete.png", b"\x89PNG\r\n\x1a\ndelete", "image/png"))
    service.reserve_for_queue("thread-1", "queue-1", uploaded["id"])
    service.delete_thread("thread-1")
    assert service.describe_staged(uploaded["id"])[0]["available"] is False
