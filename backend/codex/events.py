"""Small, UI-agnostic accumulators for Codex app-server notifications."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TurnEventAccumulator:
    """Collect the stable event fields needed by the Phase 0 spike.

    ``apply`` returns ``True`` only when a notification belongs to this
    accumulator's thread. Unknown event methods are deliberately ignored so a
    newer app-server can add notifications without breaking the client.
    """

    thread_id: str
    turn_id: str | None = None
    text_parts: list[str] = field(default_factory=list)
    status: str | None = None
    token_usage: dict[str, Any] | None = None
    completed: bool = False

    @property
    def text(self) -> str:
        return "".join(self.text_parts)

    def apply(self, notification: dict[str, Any]) -> bool:
        method = notification.get("method")
        params = notification.get("params")
        if not isinstance(method, str) or not isinstance(params, dict):
            return False
        if params.get("threadId") != self.thread_id:
            return False

        event_turn_id = params.get("turnId")
        turn = params.get("turn")
        if isinstance(turn, dict):
            event_turn_id = event_turn_id or turn.get("id")
        if self.turn_id is None and isinstance(event_turn_id, str):
            self.turn_id = event_turn_id
        if (self.turn_id is not None and isinstance(event_turn_id, str)
                and event_turn_id != self.turn_id):
            return False

        if method == "item/agentMessage/delta":
            delta = params.get("delta")
            if isinstance(delta, str):
                self.text_parts.append(delta)
        elif method == "thread/tokenUsage/updated":
            usage = params.get("tokenUsage")
            if isinstance(usage, dict):
                self.token_usage = dict(usage)
        elif method == "turn/completed":
            if isinstance(turn, dict) and isinstance(turn.get("status"), str):
                self.status = turn["status"]
            self.completed = True
        return True
