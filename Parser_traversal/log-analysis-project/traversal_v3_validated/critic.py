from __future__ import annotations
import json
from typing import Any
from traversal.prompts import CRITIC_SYSTEM


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(text)


def run_critic(backend_factory, issue, conclusion, verification, evidence, tool_history):
    backend = backend_factory(system_prompt=CRITIC_SYSTEM, tools=[])
    payload = {
        "issue": issue,
        "proposed_conclusion": conclusion,
        "deterministic_verification": verification,
        "evidence": evidence,
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
    result = backend.send(json.dumps(payload, ensure_ascii=False, default=str))
    try:
        parsed = _extract_json(result.text)
    except Exception as error:
        parsed = {
            "verdict": "NEEDS_MORE_EVIDENCE",
            "reasons": [f"Critic returned invalid JSON: {error}"],
            "continuation_instruction": "Return a strict JSON criticism based only on supplied evidence.",
            "confidence": 0.0,
        }
    parsed["usage"] = result.usage
    return parsed
