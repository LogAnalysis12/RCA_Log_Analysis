from __future__ import annotations

import json
from typing import Any

from traversal.prompts import CRITIC_SYSTEM


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(text)


def run_critic(
    backend_factory,
    issue: dict[str, Any],
    conclusion: dict[str, Any],
    verification: dict[str, Any],
    evidence: list[dict[str, Any]],
    tool_history: list[dict[str, Any]],
) -> dict[str, Any]:
    backend = backend_factory(system_prompt=CRITIC_SYSTEM, tools=[])
    payload = {
        "issue": {
            "fingerprint_id": issue.get("fingerprint_id"),
            "qualified_function": issue.get("qualified_function"),
            "normalized_message": issue.get("normalized_message"),
            "representative_error": issue.get("representative_error"),
        },
        "proposed_conclusion": conclusion,
        "deterministic_verification": verification,
        "verified_evidence": evidence,
        "tool_history_summary": [
            {"tool": item.get("tool"), "input": item.get("input")}
            for item in tool_history
        ],
        "required_response": {
            "verdict": "ACCEPT|REJECT|NEEDS_MORE_EVIDENCE",
            "reasons": ["string"],
            "continuation_instruction": "string",
            "confidence": 0.0,
        },
    }
    step = backend.send(json.dumps(payload, ensure_ascii=False, default=str))
    result = _extract_json(step.text)
    result["usage"] = step.usage
    return result
