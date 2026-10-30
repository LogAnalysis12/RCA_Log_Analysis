"""Persistent, bounded decision ledger for RCA investigations.

The ledger stores decision metadata and concise tool-result summaries.
It never stores source code or full LLM conversations.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _clean(value: Any, limit: int = 500) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())[:limit]


class InvestigationLedger:
    def __init__(
        self,
        path: str | Path,
        *,
        run_id: str | None = None,
        max_summary_chars: int = 4000,
        recent_events: int = 12,
    ):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id or str(uuid.uuid4())
        self.max_summary_chars = max(500, int(max_summary_chars))
        self.recent_events = max(1, int(recent_events))
        self._events: list[dict[str, Any]] = []
        self._next_step = 1

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _append(self, event: dict[str, Any]) -> None:
        row = {
            "schema_version": 1,
            "run_id": self.run_id,
            "timestamp": self._now(),
            **event,
        }
        # One JSON object per line. Append-only preserves the full event history.
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._events.append(row)

    def plan(
        self,
        *,
        tool: str,
        target: dict[str, Any],
        decision: dict[str, Any] | None = None,
        parent_step_id: int | None = None,
    ) -> int:
        decision = decision if isinstance(decision, dict) else {}
        step_id = self._next_step
        self._next_step += 1

        self._append({
            "event": "decision",
            "step_id": step_id,
            "parent_step_id": parent_step_id,
            "tool": _clean(tool, 100),
            "target": {
                _clean(k, 80): _clean(v, 250)
                for k, v in target.items()
                if v is not None
            },
            "why_this_step": _clean(decision.get("why_this_step"), 500),
            "question_to_resolve": _clean(decision.get("question_to_resolve"), 500),
            "expected_evidence": _clean(decision.get("expected_evidence"), 500),
            "backtrack_reason": _clean(decision.get("backtrack_reason"), 400),
            "status": "PLANNED",
        })
        return step_id

    def finish(
        self,
        step_id: int,
        *,
        status: str,
        result: dict[str, Any],
        execution_status: str | None = None,
        new_evidence: bool = False,
        repeated_action: bool = False,
        recovery_attempt: bool = False,
    ) -> None:
        # Summarize metadata only. Never persist source text or full tool output.
        summary_data = {
            key: result.get(key)
            for key in (
                "status", "error", "message", "count", "total",
                "function_id", "field_id", "function_ids", "field_ids",
            )
            if result.get(key) is not None
        }
        if not summary_data:
            summary_data = {"result_type": type(result).__name__}

        self._append({
            "event": "result",
            "step_id": step_id,
            "status": status,
            "execution_status": execution_status or status,
            "new_evidence": bool(new_evidence),
            "repeated_action": bool(repeated_action),
            "recovery_attempt": bool(recovery_attempt),
            "result_summary": _clean(
                json.dumps(summary_data, ensure_ascii=False, default=str), 900
            ),
        })

    def render_recent(self) -> str:
        if not self._events:
            return ""

        recent = self._events[-self.recent_events:]
        lines = [
            "RECENT INVESTIGATION LEDGER (historical notes, not proof):"
        ]
        for event in recent:
            if event.get("event") == "decision":
                lines.append(
                    f"- Step {event['step_id']} [{event['status']}] "
                    f"{event['tool']} target={event.get('target', {})}; "
                    f"why={event.get('why_this_step') or 'not supplied'}; "
                    f"question={event.get('question_to_resolve') or 'not supplied'}"
                )
            elif event.get("event") == "result":
                lines.append(
                    f"  Result for step {event['step_id']}: "
                    f"{event['status']} ({event.get('execution_status', event['status'])}; "
                    f"new_evidence={event.get('new_evidence', False)}; "
                    f"repeated={event.get('repeated_action', False)}; "
                    f"recovery={event.get('recovery_attempt', False)}); "
                    f"{event.get('result_summary', '')}"
                )

        rendered = "\n".join(lines)
        if len(rendered) > self.max_summary_chars:
            rendered = rendered[-self.max_summary_chars:]
        return rendered
