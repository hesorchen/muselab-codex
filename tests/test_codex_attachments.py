"""Codex-native attachment persistence and path-boundary tests."""

from io import BytesIO

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
