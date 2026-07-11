"""Browser bridge for Codex ``item/tool/requestUserInput`` requests."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .process import ServerRequest


UserInputPublisher = Callable[[str, dict[str, Any]], Awaitable[None]]
_METHOD = "item/tool/requestUserInput"
_MAX_QUESTIONS = 16
_MAX_OPTIONS = 64
_MAX_ANSWERS_PER_QUESTION = 64
_MAX_ANSWER_LENGTH = 16_384


@dataclass(frozen=True)
class _PendingInput:
    future: asyncio.Future[dict[str, dict[str, list[str]]]]
    question_ids: tuple[str, ...]


class CodexUserInputBroker:
    """Suspend a native tool call until the browser supplies structured input."""

    def __init__(self, *, timeout: float = 1800.0):
        if timeout <= 0:
            raise ValueError("user input timeout must be positive")
        self.timeout = timeout
        self.publisher: UserInputPublisher | None = None
        self._pending: dict[tuple[str, str], _PendingInput] = {}

    async def handle(self, request: ServerRequest) -> dict[str, Any]:
        if request.method != _METHOD:
            raise ValueError("unsupported app-server client request")
        thread_id = _required_string(request.params, "threadId")
        request_id = str(request.id)
        questions = _normalize_questions(request.params.get("questions"))
        key = (thread_id, request_id)
        if key in self._pending:
            raise ValueError("duplicate app-server user input request")

        future = asyncio.get_running_loop().create_future()
        pending = _PendingInput(
            future=future,
            question_ids=tuple(question["id"] for question in questions),
        )
        self._pending[key] = pending
        try:
            if self.publisher is None:
                return {"answers": {}}
            try:
                await self.publisher(thread_id, {
                    "id": request_id,
                    "questions": questions,
                })
            except ValueError:
                return {"answers": {}}
            wait_timeout = _wait_timeout(request.params, self.timeout)
            try:
                answers = await asyncio.wait_for(
                    asyncio.shield(future), wait_timeout)
            except TimeoutError:
                answers = {}
            return {"answers": answers}
        finally:
            self._pending.pop(key, None)
            if not future.done():
                future.cancel()

    def submit(
        self,
        thread_id: str,
        request_id: str,
        answers: Mapping[str, Any],
    ) -> bool:
        pending = self._pending.get((thread_id, request_id))
        if pending is None or pending.future.done():
            return False
        pending.future.set_result(
            _normalize_answers(answers, pending.question_ids))
        return True

    async def close(self) -> None:
        pending = tuple(self._pending.values())
        self._pending.clear()
        for item in pending:
            if not item.future.done():
                item.future.set_result({})


class CodexClientRequestRouter:
    """Route app-server initiated requests to their native browser brokers."""

    def __init__(self, approvals, user_input: CodexUserInputBroker, elicitation):
        self.approvals = approvals
        self.user_input = user_input
        self.elicitation = elicitation

    async def handle(self, request: ServerRequest) -> dict[str, Any]:
        if request.method == _METHOD:
            return await self.user_input.handle(request)
        if request.method == "mcpServer/elicitation/request":
            return await self.elicitation.handle(request)
        return await self.approvals.handle(request)


def _required_string(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"user input request is missing {key}")
    return value


def _normalize_questions(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ValueError("user input request has no questions")
    if len(value) > _MAX_QUESTIONS:
        raise ValueError("user input request has too many questions")

    normalized = []
    seen_ids: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("user input question must be an object")
        question_id = _required_string(raw, "id")
        if question_id in seen_ids:
            raise ValueError("user input question ids must be unique")
        seen_ids.add(question_id)
        question = _required_string(raw, "question")
        header = raw.get("header")
        if not isinstance(header, str):
            raise ValueError("user input question is missing header")
        options = _normalize_options(raw.get("options"))
        normalized.append({
            "id": question_id,
            "header": header,
            "question": question,
            "options": options,
            "multiSelect": False,
            "isOther": raw.get("isOther") is True,
            "isSecret": raw.get("isSecret") is True,
        })
    return normalized


def _normalize_options(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > _MAX_OPTIONS:
        raise ValueError("user input question has invalid options")
    options = []
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("user input option must be an object")
        label = raw.get("label")
        description = raw.get("description")
        if not isinstance(label, str) or not label:
            raise ValueError("user input option is missing label")
        if not isinstance(description, str):
            raise ValueError("user input option is missing description")
        options.append({"label": label, "description": description})
    return options


def _normalize_answers(
    answers: Mapping[str, Any],
    question_ids: tuple[str, ...],
) -> dict[str, dict[str, list[str]]]:
    if not isinstance(answers, Mapping):
        raise ValueError("answers must be an object")
    expected = set(question_ids)
    if set(answers) != expected:
        raise ValueError("answers must match the pending question ids")

    normalized = {}
    for question_id in question_ids:
        value = answers[question_id]
        values = value if isinstance(value, list) else [value]
        if not values or len(values) > _MAX_ANSWERS_PER_QUESTION:
            raise ValueError("each question requires at least one answer")
        if any(not isinstance(item, str) or not item for item in values):
            raise ValueError("answer values must be non-empty strings")
        if any(len(item) > _MAX_ANSWER_LENGTH for item in values):
            raise ValueError("answer value is too long")
        normalized[question_id] = {"answers": list(values)}
    return normalized


def _wait_timeout(params: dict[str, Any], fallback: float) -> float:
    auto_resolution_ms = params.get("autoResolutionMs")
    if (isinstance(auto_resolution_ms, int)
            and not isinstance(auto_resolution_ms, bool)
            and auto_resolution_ms >= 0):
        return min(fallback, max(auto_resolution_ms / 1000, 0.001))
    return fallback
