"""Offline tests for the workspace-scoped Codex thread service."""

import sys
from pathlib import Path

import pytest

from backend.codex import (
    AppServerProtocolError,
    AppServerResponseError,
    CodexAppServer,
    CodexRuntime,
    CodexThreadService,
)


FAKE_SERVER = Path(__file__).parent / "fixtures" / "fake_codex_app_server.py"


def runtime_with_fake_server() -> CodexRuntime:
    return CodexRuntime(lambda: CodexAppServer(
        command=(sys.executable, str(FAKE_SERVER), "normal"),
    ))


@pytest.mark.asyncio
async def test_thread_lifecycle_uses_codex_as_source_of_truth(tmp_path):
    runtime = runtime_with_fake_server()
    service = CodexThreadService(runtime, tmp_path)
    try:
        thread = await service.start(name="First thread")
        assert thread["id"] == "thread-1"
        assert thread["name"] == "First thread"
        assert thread["cwd"] == str(tmp_path.resolve())

        page = await service.list()
        assert [item["id"] for item in page.data] == ["thread-1"]
        assert page.next_cursor is None

        read = await service.read("thread-1")
        assert read["name"] == "First thread"
        resumed = await service.resume("thread-1")
        assert resumed["id"] == "thread-1"

        await service.rename("thread-1", "Renamed")
        assert (await service.read("thread-1"))["name"] == "Renamed"

        service.set_pinned("thread-1", True)
        assert (await service.read("thread-1"))["pinned"] is True
        assert (await service.list()).data[0]["pinned"] is True

        await service.delete("thread-1")
        assert (await service.list()).data == []
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_thread_pin_metadata_survives_service_restart(tmp_path):
    requester = StubRequester({"thread": {"id": "thread-1"}})
    service = CodexThreadService(requester, tmp_path)
    service.set_pinned("thread-1", True)

    restarted = CodexThreadService(requester, tmp_path)
    assert (await restarted.read("thread-1"))["pinned"] is True

    restarted.set_pinned("thread-1", False)
    reloaded = CodexThreadService(requester, tmp_path)
    assert (await reloaded.read("thread-1"))["pinned"] is False


class StubRequester:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def request(self, method, params=None, *, timeout=None):
        self.calls.append((method, params, timeout))
        return self.result


@pytest.mark.asyncio
async def test_list_is_scoped_to_exact_workspace_and_stable_sort(tmp_path):
    requester = StubRequester({"data": [], "nextCursor": "next"})
    service = CodexThreadService(requester, tmp_path)

    page = await service.list(cursor="cursor", limit=20, search_term="note")

    assert page.next_cursor == "next"
    assert requester.calls == [("thread/list", {
        "cwd": str(tmp_path.resolve()),
        "limit": 20,
        "archived": False,
        "sortKey": "updated_at",
        "sortDirection": "desc",
        "cursor": "cursor",
        "searchTerm": "note",
    }, 4.0)]


@pytest.mark.asyncio
async def test_list_cache_reuses_page_and_explicit_invalidation_refreshes(tmp_path):
    requester = StubRequester({
        "data": [{"id": "thread-1", "updatedAt": 1}],
        "nextCursor": None,
    })
    service = CodexThreadService(requester, tmp_path)

    assert (await service.list()).data[0]["id"] == "thread-1"
    assert (await service.list()).data[0]["id"] == "thread-1"
    assert len(requester.calls) == 1

    service.invalidate_list_cache()
    await service.list()
    assert len(requester.calls) == 2


@pytest.mark.asyncio
async def test_resume_preserves_persisted_approval_and_sandbox(tmp_path):
    requester = StubRequester({"thread": {"id": "thread-1"}})
    service = CodexThreadService(
        requester,
        tmp_path,
        approval_policy="never",
        sandbox="danger-full-access",
    )

    await service.resume(
        "thread-1",
        model="gpt-test",
        model_provider="openai",
        config={"model_reasoning_effort": "high"},
    )

    assert requester.calls == [("thread/resume", {
        "threadId": "thread-1",
        "cwd": str(tmp_path.resolve()),
        "model": "gpt-test",
        "modelProvider": "openai",
        "config": {"model_reasoning_effort": "high"},
    }, None)]


@pytest.mark.asyncio
async def test_start_inherits_native_codex_permissions_by_default(tmp_path):
    requester = StubRequester({"thread": {"id": "thread-1"}})
    service = CodexThreadService(requester, tmp_path)

    await service.start()

    method, params, timeout = requester.calls[0]
    assert method == "thread/start"
    assert timeout is None
    assert params == {
        "cwd": str(tmp_path.resolve()),
        "ephemeral": False,
    }


@pytest.mark.asyncio
async def test_start_can_explicitly_override_native_permissions(tmp_path):
    requester = StubRequester({"thread": {"id": "thread-1"}})
    service = CodexThreadService(
        requester,
        tmp_path,
        approval_policy="never",
        sandbox="danger-full-access",
    )

    await service.start()

    assert requester.calls[0][1]["approvalPolicy"] == "never"
    assert requester.calls[0][1]["sandbox"] == "danger-full-access"


@pytest.mark.asyncio
async def test_invalid_thread_results_fail_as_protocol_errors(tmp_path):
    service = CodexThreadService(StubRequester({"thread": {}}), tmp_path)
    with pytest.raises(AppServerProtocolError, match="without an id"):
        await service.read("thread-1")

    service = CodexThreadService(StubRequester({"data": "not-a-list"}), tmp_path)
    with pytest.raises(AppServerProtocolError, match="invalid result"):
        await service.list()


@pytest.mark.asyncio
async def test_empty_names_and_ids_are_rejected_before_protocol_call(tmp_path):
    requester = StubRequester({})
    service = CodexThreadService(requester, tmp_path)

    with pytest.raises(ValueError, match="name cannot be empty"):
        await service.rename("thread-1", "  ")
    with pytest.raises(ValueError, match="id cannot be empty"):
        await service.delete("  ")
    assert requester.calls == []


class PendingRequester:
    def __init__(self, workspace):
        self.thread = {
            "id": "pending-1",
            "name": None,
            "cwd": str(workspace),
            "createdAt": 1,
            "updatedAt": 1,
            "turns": [],
        }

    async def request(self, method, params=None, *, timeout=None):
        if method == "thread/start":
            return {"thread": dict(self.thread)}
        if method == "thread/name/set":
            self.thread["name"] = params["name"]
            return {}
        if method == "thread/list":
            return {"data": [], "nextCursor": None}
        if method in {"thread/read", "thread/resume"}:
            raise AppServerResponseError(method, -32600)
        if method == "thread/delete":
            return {}
        raise AssertionError(method)


@pytest.mark.asyncio
async def test_empty_pre_turn_thread_is_merged_from_pending_sidecar(tmp_path):
    requester = PendingRequester(tmp_path.resolve())
    service = CodexThreadService(requester, tmp_path)

    thread = await service.start(name="Pending")
    assert thread["name"] == "Pending"
    assert [item["id"] for item in (await service.list()).data] == ["pending-1"]
    assert (await service.list(cursor="next-page")).data == []
    assert (await service.read("pending-1"))["id"] == "pending-1"
    assert (await service.resume("pending-1"))["id"] == "pending-1"

    await service.delete("pending-1")
    assert (await service.list()).data == []


@pytest.mark.asyncio
async def test_listed_empty_thread_remains_pending_until_resume_succeeds(tmp_path):
    class ListedPendingRequester(PendingRequester):
        async def request(self, method, params=None, *, timeout=None):
            if method == "thread/list":
                return {"data": [dict(self.thread)], "nextCursor": None}
            return await super().request(method, params, timeout=timeout)

    requester = ListedPendingRequester(tmp_path.resolve())
    service = CodexThreadService(requester, tmp_path)

    thread = await service.start()
    service.set_pinned(thread["id"], True)
    assert [item["id"] for item in (await service.list()).data] == [thread["id"]]

    # Codex 0.144.1 may list a thread before its first turn creates a rollout.
    # The expected resume error must still be absorbed after a list refresh.
    resumed = await service.resume(thread["id"])
    assert resumed["id"] == thread["id"]
    assert resumed["pinned"] is True


@pytest.mark.asyncio
async def test_successful_empty_resume_does_not_end_pending_protection(tmp_path):
    class FlakyEmptyResumeRequester(PendingRequester):
        def __init__(self, workspace):
            super().__init__(workspace)
            self.resume_count = 0

        async def request(self, method, params=None, *, timeout=None):
            if method == "thread/resume":
                self.resume_count += 1
                if self.resume_count == 1:
                    return {"thread": dict(self.thread)}
            return await super().request(method, params, timeout=timeout)

    requester = FlakyEmptyResumeRequester(tmp_path.resolve())
    service = CodexThreadService(requester, tmp_path)
    thread = await service.start()

    assert (await service.resume(thread["id"]))["id"] == thread["id"]
    assert (await service.resume(thread["id"]))["id"] == thread["id"]

    service.mark_materialized(thread["id"])
    with pytest.raises(AppServerResponseError):
        await service.resume(thread["id"])


class LazyStartingPendingRequester(PendingRequester):
    """First protocol call starts a new app-server runtime generation."""

    class _Health:
        def __init__(self, restart_count):
            self.restart_count = restart_count

    def __init__(self, workspace):
        super().__init__(workspace)
        self.restart_count = 0

    def health(self):
        return self._Health(self.restart_count)

    async def request(self, method, params=None, *, timeout=None):
        result = await super().request(method, params, timeout=timeout)
        if method == "thread/start":
            self.restart_count = 1
        return result


@pytest.mark.asyncio
async def test_lazy_runtime_start_keeps_new_empty_thread_pending(tmp_path):
    requester = LazyStartingPendingRequester(tmp_path.resolve())
    service = CodexThreadService(requester, tmp_path)

    thread = await service.start()

    # resume is expected to receive -32600 for an empty thread, but the
    # sidecar must absorb it even when thread/start advanced the generation.
    assert (await service.resume(thread["id"]))["id"] == thread["id"]


@pytest.mark.asyncio
async def test_children_filters_parent_threads_across_pages(tmp_path):
    class ChildrenRequester:
        def __init__(self):
            self.calls = []

        async def request(self, method, params=None, *, timeout=None):
            self.calls.append((method, params, timeout))
            if params.get("cursor") is None:
                return {
                    "data": [
                        {"id": "child-1", "parentThreadId": "parent-1", "updatedAt": 2},
                        {"id": "other", "parentThreadId": "parent-2", "updatedAt": 3},
                    ],
                    "nextCursor": "page-2",
                }
            return {
                "data": [{"id": "child-2", "parentThreadId": "parent-1", "updatedAt": 1}],
                "nextCursor": None,
            }

    requester = ChildrenRequester()
    service = CodexThreadService(requester, tmp_path)
    children = await service.children("parent-1")

    assert [thread["id"] for thread in children] == ["child-1", "child-2"]
    assert [call[1].get("cursor") for call in requester.calls] == [None, "page-2"]
