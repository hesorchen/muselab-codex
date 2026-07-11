"""Browser bridge for MCP form and URL elicitation requests."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .process import ServerRequest


ElicitationPublisher = Callable[[str, str, dict[str, Any]], Awaitable[None]]
_METHOD = "mcpServer/elicitation/request"


@dataclass(frozen=True)
class _PendingElicitation:
    future: asyncio.Future[dict[str, Any]]
    mode: str
    schema: dict[str, Any] | None = None


class CodexElicitationBroker:
    """Suspend an MCP elicitation until the browser accepts or declines it."""

    def __init__(self, *, timeout: float = 1800.0):
        self.timeout = timeout
        self.publisher: ElicitationPublisher | None = None
        self._pending: dict[tuple[str, str], _PendingElicitation] = {}

    async def handle(self, request: ServerRequest) -> dict[str, Any]:
        if request.method != _METHOD:
            raise ValueError("unsupported app-server elicitation request")
        thread_id = _required_string(request.params, "threadId")
        request_id = str(request.id)
        mode = _required_string(request.params, "mode")
        if mode not in {"form", "url"}:
            # The OpenAI extended form is deliberately not advertised by this
            # client; fail closed if a server sends it anyway.
            return {"action": "cancel"}
        key = (thread_id, request_id)
        if key in self._pending:
            raise ValueError("duplicate app-server elicitation request")

        schema = request.params.get("requestedSchema") if mode == "form" else None
        if mode == "form" and not isinstance(schema, dict):
            raise ValueError("MCP form elicitation is missing requestedSchema")
        event = _form_event(request.params, request_id, schema) \
            if mode == "form" else _url_event(request.params, request_id)
        future = asyncio.get_running_loop().create_future()
        self._pending[key] = _PendingElicitation(future, mode, schema)
        try:
            if self.publisher is None:
                return {"action": "cancel"}
            try:
                await self.publisher(thread_id, mode, event)
            except ValueError:
                return {"action": "cancel"}
            try:
                return await asyncio.wait_for(asyncio.shield(future), self.timeout)
            except TimeoutError:
                return {"action": "cancel"}
        finally:
            self._pending.pop(key, None)
            if not future.done():
                future.cancel()

    def submit_answers(
        self,
        thread_id: str,
        request_id: str,
        answers: Mapping[str, Any],
    ) -> bool:
        pending = self._pending.get((thread_id, request_id))
        if pending is None or pending.future.done() or pending.mode != "form":
            return False
        pending.future.set_result({
            "action": "accept",
            "content": _coerce_form_content(answers, pending.schema or {}),
        })
        return True

    def submit_decision(self, thread_id: str, request_id: str, decision: str) -> bool:
        pending = self._pending.get((thread_id, request_id))
        if pending is None or pending.future.done() or pending.mode != "url":
            return False
        actions = {
            "allow": "accept",
            "always": "accept",
            "deny": "decline",
            "cancel": "cancel",
        }
        action = actions.get(decision)
        if action is None:
            raise ValueError("invalid elicitation decision")
        pending.future.set_result({"action": action})
        return True

    async def close(self) -> None:
        pending = tuple(self._pending.values())
        self._pending.clear()
        for item in pending:
            if not item.future.done():
                item.future.set_result({"action": "cancel"})


def _form_event(
    params: dict[str, Any],
    request_id: str,
    schema: dict[str, Any],
) -> dict[str, Any]:
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        raise ValueError("MCP form elicitation has no fields")
    required = set(schema.get("required") or [])
    questions = []
    for field, raw in properties.items():
        if not isinstance(field, str) or not isinstance(raw, dict):
            continue
        options, multi_select = _schema_options(raw)
        title = raw.get("title") if isinstance(raw.get("title"), str) else field
        description = raw.get("description")
        question = description if isinstance(description, str) and description else title
        questions.append({
            "id": field,
            "header": title,
            "question": question,
            "options": options,
            "multiSelect": multi_select,
            "isOther": not options,
            "isSecret": False,
            "required": field in required,
        })
    if not questions:
        raise ValueError("MCP form elicitation has no supported fields")
    return {
        "id": request_id,
        "kind": "mcp_form",
        "server": str(params.get("serverName") or "MCP"),
        "message": str(params.get("message") or ""),
        "questions": questions,
    }


def _schema_options(raw: dict[str, Any]) -> tuple[list[dict[str, str]], bool]:
    if raw.get("type") == "boolean":
        return [
            {"label": "Yes", "description": "", "value": "true"},
            {"label": "No", "description": "", "value": "false"},
        ], False
    values = raw.get("enum")
    names = raw.get("enumNames")
    multi_select = raw.get("type") == "array"
    if multi_select:
        items = raw.get("items")
        if isinstance(items, dict):
            values = items.get("enum")
            titled = items.get("anyOf")
            if isinstance(titled, list):
                return [
                    {"label": str(item.get("title") or item.get("const")),
                     "description": "", "value": str(item.get("const") or "")}
                    for item in titled if isinstance(item, dict) and item.get("const") is not None
                ], True
    if isinstance(raw.get("oneOf"), list):
        return [
            {"label": str(item.get("title") or item.get("const")),
             "description": "", "value": str(item.get("const") or "")}
            for item in raw["oneOf"]
            if isinstance(item, dict) and item.get("const") is not None
        ], False
    if not isinstance(values, list):
        return [], multi_select
    return [
        {
            "label": str(names[index]) if isinstance(names, list) and index < len(names)
                     else str(value),
            "description": "",
            "value": str(value),
        }
        for index, value in enumerate(values)
    ], multi_select


def _url_event(params: dict[str, Any], request_id: str) -> dict[str, Any]:
    url = _required_string(params, "url")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("MCP elicitation URL must use http or https")
    return {
        "id": request_id,
        "kind": "mcp_url",
        "tool": str(params.get("serverName") or "MCP"),
        "summary": str(params.get("message") or "Open authorization URL"),
        "url": url,
    }


def _coerce_form_content(
    answers: Mapping[str, Any],
    schema: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(answers, Mapping):
        raise ValueError("answers must be an object")
    properties = schema.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    unknown = set(answers) - set(properties)
    required = set(schema.get("required") or [])
    missing = required - {
        field for field, value in answers.items()
        if value is not None and value != "" and value != []
    }
    if unknown or missing:
        raise ValueError("answers must match the elicitation fields")
    content = {}
    for field, spec in properties.items():
        if not isinstance(spec, dict):
            raise ValueError(f"invalid elicitation schema for {field}")
        raw = answers.get(field)
        if raw is None or raw == "" or raw == []:
            continue
        values = raw if isinstance(raw, list) else [raw]
        value = values if spec.get("type") == "array" else (values[0] if values else "")
        field_type = spec.get("type")
        if field_type == "boolean":
            if isinstance(value, bool):
                content[field] = value
            elif str(value).lower() in {"true", "false"}:
                content[field] = str(value).lower() == "true"
            else:
                raise ValueError(f"invalid boolean answer for {field}")
        elif field_type == "integer":
            content[field] = int(value)
        elif field_type == "number":
            content[field] = float(value)
        elif field_type == "array":
            content[field] = [str(item) for item in values]
        else:
            content[field] = str(value)
    return content


def _required_string(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"elicitation request is missing {key}")
    return value
