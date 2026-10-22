from __future__ import annotations

import json
from collections import Counter
from typing import Any, Callable

from traversal.budget import BudgetExceeded
from traversal.compact_results import (
    canonical_function_id,
    compact_tool_result,
    conclusion_key,
    evidence_signature,
)
from traversal.critic import run_critic
from traversal.prompts import INVESTIGATOR_SYSTEM
from traversal.schemas import InvestigationState
from traversal.tool_definitions import TOOLS
from traversal.verifier import verify_conclusion


def _add_usage(total: dict[str, int], usage: dict[str, int]) -> None:
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        total[key] = total.get(key, 0) + int(usage.get(key, 0))


def _tool_key(name: str, data: dict[str, Any]) -> str:
    normalized = dict(data)
    for key in ("function_id", "exclude_function_id"):
        if key in normalized:
            normalized[key] = canonical_function_id(normalized[key])
    return f"{name}:{json.dumps(normalized, sort_keys=True, default=str)}"


def _critic_evidence(state, maximum_chars: int) -> list[dict[str, Any]]:
    evidence = []
    for function_id, result in state.read_functions.items():
        function = result.get("function", {})
        source = result.get("source", {})
        numbered = source.get("numbered_source", "")
        evidence.append(
            {
                "function_id": canonical_function_id(function_id),
                "qualified_name": function.get("qualified_name", ""),
                "relative_path": function.get("relative_path", ""),
                "start_line": source.get("start_line"),
                "end_line": source.get("actual_end_line"),
                "source_verified": result.get("status") == "SOURCE_VERIFIED",
                "numbered_source": numbered[:maximum_chars],
                "source_truncated": len(numbered) > maximum_chars,
            }
        )
    return evidence


def run_investigation(
    issue: dict[str, Any],
    tools,
    backend_factory: Callable,
    max_steps: int = 8,
    max_critic_rounds: int = 3,
    stall_retries: int = 1,
    repeated_tool_limit: int = 1,
    runtime_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = runtime_config or {}
    anchor = issue["anchor_resolution"]["selected_function"]
    state = InvestigationState(issue=issue, anchor=anchor)
    backend = backend_factory(system_prompt=INVESTIGATOR_SYSTEM, tools=TOOLS)
    repeats: Counter[str] = Counter()
    reviewed: dict[str, frozenset[str]] = {}
    critic_rejections = 0
    stalls = 0

    initial = {
        "error_cluster": {
            "fingerprint_id": issue.get("fingerprint_id"),
            "occurrence_count": issue.get("occurrence_count"),
            "qualified_function": issue.get("qualified_function"),
            "normalized_message": issue.get("normalized_message"),
            "representative_error": issue.get("representative_error"),
        },
        "resolved_anchor": anchor,
        "instruction": (
            "Read the anchor source first. Trace project-controlled values through "
            "verified callers, callees, or field writers. An external call is a "
            "failure boundary, not automatically a proven root cause."
        ),
    }

    try:
        step = backend.send(json.dumps(initial, ensure_ascii=False, default=str))
        _add_usage(state.total_usage, step.usage)

        for step_number in range(1, max_steps + 1):
            if not step.tool_calls:
                stalls += 1
                if stalls > stall_retries:
                    return _result("STALLED", state, reviewed, critic_rejections)
                step = backend.send("Call one tool now. Do not return prose without a tool call.")
                _add_usage(state.total_usage, step.usage)
                continue

            stalls = 0
            ordinary_results: list[dict[str, Any]] = []
            proposal_call = None
            proposal = None

            for call in step.tool_calls:
                if call.name == "propose_conclusion":
                    if proposal_call is None:
                        proposal_call, proposal = call, dict(call.input)
                    else:
                        ordinary_results.append(
                            {
                                "tool_use_id": call.id,
                                "content": {"error": "Only one conclusion is allowed per turn."},
                                "is_error": True,
                            }
                        )
                    continue

                key = _tool_key(call.name, call.input)
                repeats[key] += 1
                if repeats[key] > repeated_tool_limit:
                    result = {
                        "status": "TOOL_ERROR",
                        "error": (
                            "Repeated identical tool request blocked after "
                            f"{repeated_tool_limit} allowed attempts."
                        ),
                        "continuation_instruction": (
                            "Do not retry this exact tool request. Use the "
                            "verified source and tool results already present "
                            "in the conversation. Investigate a different "
                            "unresolved causal question, or call "
                            "propose_conclusion if the evidence supports one."
                        ),
                    }
                else:
                    result = _execute_tool(tools, call.name, call.input, config)
                    if call.name == "read_function_source" and result.get("status") == "SOURCE_VERIFIED":
                        state.read_functions[
                            canonical_function_id(result.get("function_id"))
                        ] = result

                state.tool_history.append(
                    {"step": step_number, "tool": call.name, "input": call.input, "result": result}
                )
                model_result = (
                    compact_tool_result(call.name, result, config)
                    if config.get("compact_tool_results", True)
                    else result
                )
                ordinary_results.append(
                    {
                        "tool_use_id": call.id,
                        "content": model_result,
                        "is_error": "error" in result,
                    }
                )

            if proposal_call is None:
                step = backend.send_tool_results(ordinary_results)
                _add_usage(state.total_usage, step.usage)
                continue

            proposal["root_cause_function_id"] = canonical_function_id(
                proposal.get("root_cause_function_id")
            )
            key = conclusion_key(proposal)
            current_evidence = evidence_signature(state)
            previous_evidence = reviewed.get(key)
            if (
                config.get("prevent_equivalent_conclusions", True)
                and previous_evidence is not None
                and current_evidence.issubset(previous_evidence)
            ):
                rejection = _tool_result(
                    proposal_call.id,
                    "LOCAL_DUPLICATE_CHECK",
                    ["Equivalent conclusion repeated without new evidence."],
                )
                step = backend.send_tool_results(ordinary_results + [rejection])
                _add_usage(state.total_usage, step.usage)
                continue
            reviewed[key] = current_evidence

            state.tool_history.append(
                {
                    "step": step_number,
                    "tool": "propose_conclusion",
                    "input": proposal,
                    "result": {"submitted_for_review": True},
                }
            )
            verification = verify_conclusion(proposal, state)
            if not verification["passed"]:
                instruction = verification["continuation_instruction"]
                state.critic_feedback.append(instruction)
                rejection = _tool_result(
                    proposal_call.id,
                    "DETERMINISTIC_VERIFIER",
                    verification["reasons"],
                    instruction,
                )
                step = backend.send_tool_results(ordinary_results + [rejection])
                _add_usage(state.total_usage, step.usage)
                continue

            critic = run_critic(
                backend_factory,
                issue,
                proposal,
                verification,
                _critic_evidence(
                    state, int(config.get("max_source_characters_per_function", 12_000))
                ),
                state.tool_history,
            )
            _add_usage(state.total_usage, critic.get("usage", {}))
            if critic.get("verdict") == "ACCEPT":
                return _result(
                    "ACCEPTED", state, reviewed, critic_rejections,
                    conclusion=proposal, verification=verification, critic=critic,
                )

            critic_rejections += 1
            instruction = critic.get("continuation_instruction") or "Gather new verified evidence."
            state.critic_feedback.append(instruction)
            if critic_rejections > max_critic_rounds:
                return _result(
                    "NOT_ACCEPTED", state, reviewed, critic_rejections,
                    conclusion=proposal, verification=verification, critic=critic,
                )
            rejection = _tool_result(
                proposal_call.id,
                "INDEPENDENT_CRITIC",
                critic.get("reasons", []),
                instruction,
            )
            step = backend.send_tool_results(ordinary_results + [rejection])
            _add_usage(state.total_usage, step.usage)

        return _result("MAX_STEPS_REACHED", state, reviewed, critic_rejections)

    except BudgetExceeded as error:
        return _result(
            "BUDGET_EXCEEDED", state, reviewed, critic_rejections,
            budget_reason=error.reason,
        )
    except Exception as error:
        return _result(
            "INVESTIGATION_FAILED", state, reviewed, critic_rejections,
            error_type=type(error).__name__, error_message=str(error),
        )


def _execute_tool(tools, name: str, data: dict[str, Any], config: dict[str, Any]):
    if name == "read_function_source":
        return tools.read_function_source(data["function_id"])
    if name == "find_callers":
        return tools.find_callers(data["function_id"], limit=int(config.get("max_callers", 30)))
    if name == "find_callees":
        return tools.find_callees(data["function_id"], limit=int(config.get("max_callees", 40)))
    if name == "find_spawned_workers":
        return tools.find_spawned_workers(
            data["function_id"],
            limit=int(config.get("max_spawned_workers", 100)),
        )
    if name == "list_fields_read":
        return tools.find_fields_read(data["function_id"], include_ignored=False)
    if name == "find_field_writers":
        return tools.find_field_writers(
            data["field_id"],
            data.get("exclude_function_id"),
            limit=int(config.get("max_field_writers", 30)),
        )
    return {"error": f"Unknown tool: {name}"}


def _tool_result(
    tool_use_id: str,
    source: str,
    reasons: list[str],
    instruction: str = "",
) -> dict[str, Any]:
    return {
        "tool_use_id": tool_use_id,
        "content": {
            "verdict": "REJECT",
            "source": source,
            "reasons": reasons,
            "continuation_instruction": instruction or (reasons[0] if reasons else ""),
        },
        "is_error": True,
    }


def _result(status: str, state, reviewed, critic_rejections: int, **extra):
    return {
        "status": status,
        "reviewed_conclusion_count": len(reviewed),
        "critic_rejection_count": critic_rejections,
        **extra,
        "state": state.to_dict(),
    }
