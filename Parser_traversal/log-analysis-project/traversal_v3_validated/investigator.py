from __future__ import annotations

import json
from collections import Counter
from typing import Any, Callable

from traversal.critic import run_critic
from traversal.prompts import INVESTIGATOR_SYSTEM
from traversal.schemas import InvestigationState
from traversal.tool_definitions import TOOLS
from traversal.verifier import verify_conclusion


class InvestigationError(RuntimeError):
    pass


def _add_usage(total: dict[str, int], usage: dict[str, int]) -> None:
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        total[key] = total.get(key, 0) + int(usage.get(key, 0))


def _tool_key(name: str, data: dict[str, Any]) -> str:
    return f"{name}:{json.dumps(data, sort_keys=True, default=str)}"


def run_investigation(
    issue: dict[str, Any],
    tools,
    backend_factory: Callable,
    max_steps: int = 18,
    max_critic_rounds: int = 2,
    stall_retries: int = 2,
    repeated_tool_limit: int = 2,
) -> dict[str, Any]:
    anchor = issue["anchor_resolution"]["selected_function"]
    state = InvestigationState(issue=issue, anchor=anchor)
    backend = backend_factory(system_prompt=INVESTIGATOR_SYSTEM, tools=TOOLS)
    repeat_counts: Counter[str] = Counter()

    initial = {
        "error_cluster": {
            "fingerprint_id": issue.get("fingerprint_id"),
            "occurrence_count": issue.get("occurrence_count"),
            "qualified_function": issue.get("qualified_function"),
            "normalized_message": issue.get("normalized_message"),
            "representative_error": issue.get("representative_error"),
        },
        "resolved_anchor": anchor,
        "instruction": "Start by calling read_function_source for the anchor Function ID.",
    }
    step = backend.send(json.dumps(initial, ensure_ascii=False, default=str))
    _add_usage(state.total_usage, step.usage)
    stalls = 0
    critic_rounds = 0

    for step_number in range(1, max_steps + 1):
        if not step.tool_calls:
            stalls += 1
            if stalls > stall_retries:
                return {"status": "STALLED", "state": state.to_dict()}
            step = backend.send("Call one tool now. Do not return prose without a tool call.")
            _add_usage(state.total_usage, step.usage)
            continue
        stalls = 0

        normal_results: list[dict[str, Any]] = []
        proposal_call = None
        proposal = None

        for call in step.tool_calls:
            if call.name == "propose_conclusion":
                if proposal_call is not None:
                    normal_results.append({
                        "tool_use_id": call.id,
                        "content": {"error": "Only one conclusion may be proposed per turn."},
                        "is_error": True,
                    })
                    continue
                proposal_call = call
                proposal = call.input
                continue

            key = _tool_key(call.name, call.input)
            repeat_counts[key] += 1
            if repeat_counts[key] > repeated_tool_limit:
                result = {"error": "Repeated identical tool request blocked. Choose a different evidence path."}
            elif call.name == "read_function_source":
                result = tools.read_function_source(call.input["function_id"])
                if result.get("status") == "SOURCE_VERIFIED":
                    state.read_functions[result["function_id"]] = result
            elif call.name == "find_callers":
                result = tools.find_callers(call.input["function_id"])
            elif call.name == "find_callees":
                result = tools.find_callees(call.input["function_id"])
            elif call.name == "list_fields_read":
                result = tools.find_fields_read(call.input["function_id"], include_ignored=False)
            elif call.name == "find_field_writers":
                result = tools.find_field_writers(
                    call.input["field_id"],
                    call.input.get("exclude_function_id"),
                )
            else:
                result = {"error": f"Unknown tool: {call.name}"}

            state.tool_history.append({
                "step": step_number,
                "tool": call.name,
                "input": call.input,
                "result": result,
            })
            normal_results.append({
                "tool_use_id": call.id,
                "content": result,
                "is_error": "error" in result,
            })

        if proposal_call is None:
            step = backend.send_tool_results(normal_results)
            _add_usage(state.total_usage, step.usage)
            continue

        state.tool_history.append({
            "step": step_number,
            "tool": "propose_conclusion",
            "input": proposal,
            "result": {"submitted_for_review": True},
        })
        verification = verify_conclusion(proposal, state)

        if not verification["passed"]:
            instruction = verification["continuation_instruction"]
            state.critic_feedback.append(instruction)
            proposal_result = {
                "tool_use_id": proposal_call.id,
                "content": {
                    "verdict": "REJECT",
                    "source": "DETERMINISTIC_VERIFIER",
                    "reasons": verification["reasons"],
                    "continuation_instruction": instruction,
                },
                "is_error": True,
            }
            step = backend.send_tool_results(normal_results + [proposal_result])
            _add_usage(state.total_usage, step.usage)
            continue

        critic_result = run_critic(
            backend_factory,
            issue,
            proposal,
            verification,
            state.evidence,
            state.tool_history,
        )
        _add_usage(state.total_usage, critic_result.get("usage", {}))
        verdict = critic_result.get("verdict", "NEEDS_MORE_EVIDENCE")

        if verdict == "ACCEPT":
            return {
                "status": "ACCEPTED",
                "conclusion": proposal,
                "verification": verification,
                "critic": critic_result,
                "state": state.to_dict(),
            }

        critic_rounds += 1
        instruction = critic_result.get("continuation_instruction") or "Gather additional verified source evidence."
        state.critic_feedback.append(instruction)
        if critic_rounds > max_critic_rounds:
            return {
                "status": "NOT_ACCEPTED",
                "conclusion": proposal,
                "verification": verification,
                "critic": critic_result,
                "state": state.to_dict(),
            }

        proposal_result = {
            "tool_use_id": proposal_call.id,
            "content": {
                "verdict": verdict,
                "source": "INDEPENDENT_CRITIC",
                "reasons": critic_result.get("reasons", []),
                "continuation_instruction": instruction,
            },
            "is_error": True,
        }
        step = backend.send_tool_results(normal_results + [proposal_result])
        _add_usage(state.total_usage, step.usage)

    return {"status": "MAX_STEPS_REACHED", "state": state.to_dict()}
