from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from traversal.budget import BudgetExceeded
from traversal.compact_results import (
    canonical_function_id,
    compact_tool_result,
    conclusion_key,
    evidence_signature,
)
from traversal.critic import run_critic
from traversal.function_memory import ContextBuilder, FunctionUnderstandingMemory
from traversal.investigation_ledger import InvestigationLedger
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


def _cached_source_replay(
    key: str,
    cached_source: dict[str, Any] | None,
    replayed_cache_keys: set[str],
) -> dict[str, Any] | None:
    """Return verified source from cache at most once per tool request."""
    if (
        cached_source is None
        or cached_source.get("status") != "SOURCE_VERIFIED"
        or key in replayed_cache_keys
    ):
        return None

    replayed_cache_keys.add(key)
    return {
        **cached_source,
        "cache_hit": True,
        "continuation_instruction": (
            "This source was already verified earlier and is being returned "
            "from cache. Do not request it again. Use the source and the "
            "existing investigation evidence to investigate a different "
            "unresolved causal question. Prefer the relevant shared-state "
            "writer, worker-launch relationship, or synchronization/order "
            "evidence. Propose a conclusion only if the causal chain is "
            "adequately supported."
        ),
    }


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
    memory = None
    context_builder = None
    if config.get("function_memory_enabled", True):
        repo_hint = config.get("repository_id") or config.get("source_root") or anchor.get("relative_path") or "default"
        repo_path = Path(str(repo_hint))
        repository_id = str(config.get("repository_id") or repo_path.name or "default")
        memory = FunctionUnderstandingMemory(
            config.get("function_memory_path", "output/function_memory.jsonl"),
            repository_id,
            max_records=int(config.get("function_memory_max_records", 5000)),
        )
        context_builder = ContextBuilder(
            memory,
            max_chars=int(config.get("function_memory_chars", 2400)),
            max_functions=int(config.get("function_memory_max_functions", 6)),
        )
    ledger = None
    if config.get("investigation_ledger_enabled", True):
        ledger = InvestigationLedger(
            config.get("investigation_ledger_path", "output/investigation_ledger.jsonl"),
            max_summary_chars=int(config.get("investigation_ledger_summary_chars", 4000)),
            recent_events=int(config.get("investigation_ledger_recent_events", 12)),
        )

    backend = backend_factory(system_prompt=INVESTIGATOR_SYSTEM, tools=TOOLS)
    repeats: Counter[str] = Counter()
    replayed_cache_keys: set[str] = set()
    reviewed: dict[str, frozenset[str]] = {}
    critic_rejections = 0
    stalls = 0
    seen_evidence: set[str] = set()
    tool_turn_counts: Counter[str] = Counter()
    no_progress_steps = 0
    no_progress_limit = max(2, int(config.get("no_progress_limit", 5)))
    no_progress_prompt_after = max(1, int(config.get("no_progress_prompt_after", 2)))

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
                stop_reason = (getattr(step, "stop_reason", "") or "").lower()
                recovery_reason = "length" if stop_reason == "length" else (
                    "stop" if stop_reason == "stop" else (stop_reason or "unknown")
                )
                checkpoint = _build_recovery_checkpoint(
                    issue, anchor, state, repeats, recovery_reason
                )
                if stalls > stall_retries:
                    return _result(
                        "STALLED", state, reviewed, critic_rejections,
                        recovery_reason=recovery_reason,
                        recovery_checkpoint=checkpoint,
                        recovery_attempts=stalls - 1,
                    )
                recovery_prompt = _no_tool_recovery_prompt(
                    recovery_reason, checkpoint, getattr(step, "text", "") or ""
                )
                step = backend.send(recovery_prompt)
                _add_usage(state.total_usage, step.usage)
                continue

            stalls = 0
            turn_fingerprint = _tool_turn_fingerprint(step.tool_calls)
            tool_turn_counts[turn_fingerprint] += 1
            repeated_turn = tool_turn_counts[turn_fingerprint] > 1
            step_gained_new_evidence = False
            ordinary_results: list[dict[str, Any]] = []
            proposal_call = None
            proposal = None

            for call in step.tool_calls:
                call_input = call.input if isinstance(call.input, dict) else {}
                if "_invalid_json" in call_input:
                    raw_arguments = str(call_input.get("_invalid_json", ""))[:1000]
                    checkpoint = _build_recovery_checkpoint(
                        issue, anchor, state, repeats, "invalid_tool_arguments"
                    )
                    invalid_result = {
                        "status": "INVALID_TOOL_ARGUMENTS",
                        "error": "The model returned malformed JSON for tool arguments.",
                        "tool": call.name,
                        "raw_arguments": raw_arguments,
                        "continuation_instruction": _tool_failure_recovery_instruction(
                            call.name,
                            {"status": "INVALID_TOOL_ARGUMENTS", "error": "Malformed JSON tool arguments."},
                            checkpoint,
                        ),
                        "recovery_checkpoint": checkpoint,
                    }
                    state.tool_history.append({
                        "step": step_number, "tool": call.name,
                        "input": {"_invalid_json": raw_arguments},
                        "result": invalid_result,
                    })
                    ordinary_results.append({
                        "tool_use_id": call.id,
                        "content": invalid_result,
                        "is_error": True,
                    })
                    continue
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

                # Remove rationale metadata before passing arguments to graph tools.
                tool_input = dict(call.input)
                decision = tool_input.pop("decision", {})
                if not isinstance(decision, dict):
                    decision = {}

                ledger_step_id = None
                if ledger is not None:
                    ledger_step_id = ledger.plan(
                        tool=call.name,
                        target=tool_input,
                        decision=decision,
                    )

                key = _tool_key(call.name, tool_input)
                repeats[key] += 1

                cached_source = None
                if call.name == "read_function_source":
                    function_id = canonical_function_id(
                        tool_input.get("function_id")
                    )
                    cached_source = state.read_functions.get(function_id)

                cached_replay = _cached_source_replay(
                    key,
                    cached_source,
                    replayed_cache_keys,
                )

                if cached_replay is not None:
                    result = cached_replay
                elif cached_source is not None:
                    # This identical source request has already received its
                    # one cache replay. Do not return the same evidence again.
                    result = {
                        "status": "TOOL_ERROR",
                        "error": (
                            "Verified source was already returned from cache "
                            "for this identical request."
                        ),
                        "continuation_instruction": (
                            "Do not retry this request. Use the verified "
                            "source and existing investigation evidence to "
                            "investigate a different unresolved causal "
                            "question, or propose a supported conclusion."
                        ),
                    }
                elif repeats[key] > repeated_tool_limit:
                    result = {
                        "status": "TOOL_ERROR",
                        "error": (
                            "Repeated identical tool request blocked after "
                            f"{repeated_tool_limit} allowed attempts."
                        ),
                        "continuation_instruction": (
                            "Do not retry this exact request. Use evidence "
                            "already collected and investigate a different "
                            "unresolved causal question. Propose a conclusion "
                            "only when supported by evidence."
                        ),
                    }
                else:
                    result = _execute_tool(
                        tools, call.name, tool_input, config
                    )
                    if (
                        call.name == "read_function_source"
                        and result.get("status") == "SOURCE_VERIFIED"
                    ):
                        state.read_functions[
                            canonical_function_id(result.get("function_id"))
                        ] = result
                        if memory is not None:
                            memory.remember_source(result)

                evidence_fingerprint = _evidence_fingerprint(call.name, result)
                if evidence_fingerprint is not None and evidence_fingerprint not in seen_evidence:
                    seen_evidence.add(evidence_fingerprint)
                    step_gained_new_evidence = True

                state.tool_history.append(
                    {"step": step_number, "tool": call.name, "input": tool_input, "result": result}
                )

                if ledger is not None and ledger_step_id is not None:
                    if repeats[key] > repeated_tool_limit:
                        ledger_status = "BLOCKED"
                    elif "error" in result or result.get("status") in ("FAILED", "TOOL_ERROR"):
                        ledger_status = "FAILED"
                    else:
                        ledger_status = "COMPLETED"
                    ledger.finish(
                        ledger_step_id,
                        status=ledger_status,
                        result=result,
                    )

                model_result = (
                    compact_tool_result(call.name, result, config)
                    if config.get("compact_tool_results", True)
                    else result
                )
                if _is_tool_failure(result):
                    checkpoint = _build_recovery_checkpoint(
                        issue, anchor, state, repeats, "tool_execution_failure"
                    )
                    model_result = {
                        "tool_result": model_result,
                        "recovery_instruction": _tool_failure_recovery_instruction(
                            call.name, result, checkpoint
                        ),
                        "recovery_checkpoint": checkpoint,
                    }
                # Store bounded graph observations only against a function whose source
                # has already been verified during this run. Do not persist conclusions.
                memory_function_id = canonical_function_id(
                    result.get("function_id") or call.input.get("function_id")
                )
                source_record = state.read_functions.get(memory_function_id)
                if memory is not None and source_record and call.name != "read_function_source":
                    observation = json.dumps(result, ensure_ascii=False, sort_keys=True, default=str)[:360]
                    memory.remember_source(
                        source_record,
                        evidence=[{
                            "tool": call.name,
                            "status": result.get("status", "UNKNOWN"),
                            "observation": observation,
                            "verified": False,
                        }],
                    )
                extra_context = []
                if context_builder is not None and memory_function_id:
                    memory_context = context_builder.build(active_function_id=memory_function_id)
                    if memory_context:
                        extra_context.append(memory_context)
                if ledger is not None:
                    ledger_context = ledger.render_recent()
                    if ledger_context:
                        extra_context.append(ledger_context)

                if extra_context:
                    context_text = "\n\n".join(extra_context)
                    if isinstance(model_result, str):
                        model_result = model_result + "\n\n" + context_text
                    else:
                        model_result = {
                            "tool_result": model_result,
                            "investigation_context": context_text,
                        }
                ordinary_results.append(
                    {
                        "tool_use_id": call.id,
                        "content": model_result,
                        "is_error": "error" in result,
                    }
                )

            if proposal_call is None:
                if step_gained_new_evidence:
                    no_progress_steps = 0
                else:
                    no_progress_steps += 1

                if no_progress_steps >= no_progress_prompt_after or repeated_turn:
                    checkpoint = _build_recovery_checkpoint(
                        issue, anchor, state, repeats, "no_new_evidence_or_repeated_tool_turn",
                        max_chars=1600,
                    )
                    guidance = _no_progress_recovery_prompt(
                        checkpoint,
                        repeated_turn=repeated_turn,
                        no_progress_steps=no_progress_steps,
                    )
                    if ordinary_results:
                        last_result = ordinary_results[-1]
                        previous_content = last_result.get("content", "")
                        last_result["content"] = _guidance_first_tool_result_content(
                            guidance, previous_content
                        )

                if no_progress_steps >= no_progress_limit:
                    checkpoint = _build_recovery_checkpoint(
                        issue, anchor, state, repeats, "no_progress_limit_reached"
                    )
                    return _result(
                        "STALLED", state, reviewed, critic_rejections,
                        recovery_reason="no_progress_limit_reached",
                        recovery_checkpoint=checkpoint,
                        no_progress_steps=no_progress_steps,
                        repeated_tool_turn=repeated_turn,
                    )

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
    if name == "find_worker_launchers":
        return tools.find_worker_launchers(
            data["function_id"],
            limit=int(config.get("max_worker_launchers", 100)),
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


def _build_recovery_checkpoint(
    issue: dict[str, Any],
    anchor: Any,
    state: Any,
    repeats: Counter[str],
    reason: str,
    *,
    max_chars: int = 6500,
) -> dict[str, Any]:
    """Build a bounded, deterministic continuation checkpoint from controller state."""
    def short_json(value: Any, limit: int = 700) -> str:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            text = str(value)
        return text if len(text) <= limit else text[:limit] + "...[truncated]"

    def source_summary(source: Any) -> dict[str, Any]:
        if not isinstance(source, dict):
            return {"summary": short_json(source, 180)}
        keys = (
            "qualified_name", "function_name", "signature", "relative_path",
            "start_line", "end_line", "source_sha256", "status",
        )
        return {key: source[key] for key in keys if key in source}

    def action_summary(action: Any) -> dict[str, Any]:
        if not isinstance(action, dict):
            return {"summary": short_json(action, 200)}
        result = action.get("result") or {}
        if not isinstance(result, dict):
            result = {"summary": short_json(result, 360)}
        result_keys = (
            "status", "qualified_name", "function_name", "relative_path",
            "start_line", "end_line", "caller_count", "callers",
            "project_callees", "field_name", "field_access_count", "fields",
            "writer_count", "writers", "error", "continuation_instruction",
        )
        compact_result = {key: result[key] for key in result_keys if key in result}
        # Bound arrays and nested graph output so recovery does not reproduce huge results.
        for key in ("callers", "project_callees", "fields", "writers"):
            if isinstance(compact_result.get(key), list):
                compact_result[key] = compact_result[key][:5]
        return {
            "step": action.get("step"),
            "tool": action.get("tool"),
            "input": short_json(action.get("input", {}), 260),
            "result": short_json(compact_result or result, 780),
        }

    representative = issue.get("representative_error") or {}
    if not isinstance(representative, dict):
        representative = {"summary": short_json(representative, 300)}
    incident = {
        "fingerprint_id": issue.get("fingerprint_id"),
        "occurrence_count": issue.get("occurrence_count"),
        "qualified_function": issue.get("qualified_function"),
        "normalized_message": issue.get("normalized_message"),
        "representative_error": {
            key: representative[key]
            for key in ("level", "message", "qualified_function", "relative_path", "line")
            if key in representative
        },
    }

    raw_sources = getattr(state, "read_functions", {}) or {}
    verified_sources = [source_summary(value) for value in raw_sources.values()]
    verified_sources = verified_sources[-8:]

    raw_history = getattr(state, "tool_history", []) or []
    recent_actions = [action_summary(item) for item in raw_history[-10:]]
    repeated = [
        {"action": str(key)[:300], "count": count}
        for key, count in repeats.most_common(8)
        if count > 1
    ]

    checkpoint: dict[str, Any] = {
        "checkpoint_version": 1,
        "recovery_reason": reason,
        "root_cause_status": "NOT_ESTABLISHED",
        "anchor": source_summary(anchor),
        "incident": incident,
        "verified_source_functions": verified_sources,
        "recent_tool_actions": recent_actions,
        "repeated_actions": repeated,
        "unresolved_questions": [
            "Connect the verified state mutation to the observed error through relevant control-flow and synchronization evidence.",
            "Confirm that the log's failing function and source anchor refer to the same incident path.",
            "Separate verified causal evidence from hypotheses and decide whether the root cause is established.",
        ],
    }

    # Keep recent evidence first, but remove older action summaries if the checkpoint is too large.
    while len(json.dumps(checkpoint, ensure_ascii=False, default=str)) > max_chars and checkpoint["recent_tool_actions"]:
        checkpoint["recent_tool_actions"].pop(0)
    while len(json.dumps(checkpoint, ensure_ascii=False, default=str)) > max_chars and checkpoint["verified_source_functions"]:
        checkpoint["verified_source_functions"].pop(0)
    if len(json.dumps(checkpoint, ensure_ascii=False, default=str)) > max_chars:
        checkpoint["repeated_actions"] = checkpoint["repeated_actions"][:3]
        checkpoint["unresolved_questions"] = checkpoint["unresolved_questions"][:2]
    return checkpoint


def _no_tool_recovery_prompt(reason: str, checkpoint: dict[str, Any], previous_text: str = "") -> str:
    """Return a situation-specific instruction for a model turn without a usable tool call."""
    if reason == "length":
        instruction = (
            "RECOVERY CASE: RESPONSE TRUNCATED (finish_reason=length). The previous completion was cut off and "
            "did not provide a usable tool call. Do not continue or defend the partial prose. Use the checkpoint "
            "below as the authoritative investigation state. Choose ONE concise next action: call the tool that "
            "best resolves the highest-priority unresolved question, or call propose_conclusion only if the evidence "
            "already supports a causal conclusion. Do not repeat completed actions unless you state what new evidence "
            "the repeat is expected to establish. Keep arguments valid JSON and the response short."
        )
    elif reason == "stop":
        instruction = (
            "RECOVERY CASE: PROSE WITHOUT A TOOL CALL (finish_reason=stop). Your previous turn ended normally but "
            "did not execute an investigation action. Continue from the checkpoint; do not restart from the anchor "
            "or restate the investigation. Identify ONE unresolved causal question and invoke the most appropriate "
            "available tool. Use propose_conclusion only when verified evidence supports it. Avoid already-completed "
            "requests unless they resolve a specific unanswered question."
        )
    else:
        instruction = (
            f"RECOVERY CASE: NO USABLE TOOL CALL (finish_reason={reason or 'unknown'}). Preserve the current "
            "investigation state shown below. Do not restart or repeat completed work. Choose ONE valid tool action "
            "that addresses an unresolved causal question, or propose_conclusion only if the verified evidence is "
            "sufficient. Keep the next response short and arguments valid."
        )
    text = previous_text.strip()
    if text:
        instruction += "\nPartial/non-actionable previous response (not verified evidence): " + text[:240]
    return instruction + "\n\nCURRENT INVESTIGATION CHECKPOINT (JSON):\n" + json.dumps(
        checkpoint, ensure_ascii=False, default=str
    )


def _tool_failure_recovery_instruction(
    tool_name: str, result: dict[str, Any], checkpoint: dict[str, Any]
) -> str:
    """Explain a failed tool action and guide correction without restarting investigation."""
    error_text = str(result.get("error", result.get("message", result.get("status", "tool failed"))))
    lowered = error_text.lower()
    argument_error = any(
        marker in lowered
        for marker in ("argument", "invalid json", "missing required", "required field", "unexpected keyword", "parameter")
    ) or result.get("status") in {"INVALID_ARGUMENTS", "INVALID_TOOL_ARGUMENTS"}
    if argument_error:
        prefix = (
            f"RECOVERY CASE: INVALID ARGUMENTS FOR {tool_name}. Read the validation error, compare the tool schema, "
            "and correct only the invalid or missing arguments. Do not repeat the malformed arguments unchanged. "
            "Continue from the checkpoint; do not restart the RCA."
        )
    else:
        prefix = (
            f"RECOVERY CASE: TOOL ACTION {tool_name} FAILED. Use the returned error to decide whether to correct "
            "the request or choose a different tool. Do not claim this failed action produced evidence, and do not "
            "repeat it unchanged. Continue from the checkpoint rather than restarting the RCA."
        )
    return prefix + "\nTool error: " + error_text[:700] + "\nCURRENT INVESTIGATION CHECKPOINT (JSON):\n" + json.dumps(
        checkpoint, ensure_ascii=False, default=str
    )


def _is_tool_failure(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    return (
        "error" in result
        or result.get("status") in {
            "FAILED", "TOOL_ERROR", "INVALID_ARGUMENTS", "INVALID_TOOL_ARGUMENTS"
        }
    )


def _stable_evidence_payload(value: Any) -> Any:
    """Remove transient controller metadata before comparing tool evidence."""
    volatile = {
        "cache_hit", "continuation_instruction", "investigation_context",
        "recovery_checkpoint", "recovery_instruction", "controller_guidance",
    }
    if isinstance(value, dict):
        return {
            str(key): _stable_evidence_payload(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key) not in volatile
        }
    if isinstance(value, (list, tuple)):
        return [_stable_evidence_payload(item) for item in value]
    return value


def _evidence_fingerprint(tool_name: str, result: Any) -> str | None:
    """Return a stable fingerprint for successful tool evidence, or None on failure."""
    if not isinstance(result, dict):
        return None
    if "error" in result or result.get("status") in {
        "FAILED", "TOOL_ERROR", "INVALID_ARGUMENTS", "INVALID_TOOL_ARGUMENTS",
    }:
        return None
    payload = {"tool": tool_name, "result": _stable_evidence_payload(result)}
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        return None


def _tool_turn_fingerprint(tool_calls: Any) -> str:
    """Fingerprint a whole assistant tool-call turn, ignoring rationale metadata."""
    actions = []
    for call in tool_calls or []:
        raw_input = getattr(call, "input", {})
        tool_input = dict(raw_input) if isinstance(raw_input, dict) else {"_input": str(raw_input)}
        tool_input.pop("decision", None)
        actions.append({
            "tool": getattr(call, "name", ""),
            "input": _stable_evidence_payload(tool_input),
        })
    return json.dumps(actions, ensure_ascii=False, sort_keys=True, default=str)


def _no_progress_recovery_prompt(
    checkpoint: dict[str, Any], *, repeated_turn: bool, no_progress_steps: int
) -> str:
    """Guide the model away from a repeated tool loop using durable investigation state."""
    if repeated_turn:
        reason = (
            "LOOP DETECTED: this exact tool-call sequence has already occurred and the results added no new evidence."
        )
    else:
        reason = (
            f"NO NEW EVIDENCE: {no_progress_steps} consecutive tool-call turns have not established a new fact."
        )
    return (
        "INVESTIGATION CONTINUATION REQUIRED. " + reason + "\n"
        "Use the checkpoint as the authoritative record of work already completed. Do not repeat the same tool-call "
        "sequence. Select a different tool or target that answers one outstanding causal question. Prioritize the "
        "verified control/data-flow link between the mutation and the logged failure, synchronization/order evidence, "
        "and confirming that the log location and anchor match. A negative result is evidence only if it is relevant "
        "to a stated unresolved question. Propose a conclusion only if the causal chain is adequately supported. "
        "Return one concise, valid tool call.\n\n"
        "CURRENT INVESTIGATION CHECKPOINT (JSON):\n"
        + json.dumps(checkpoint, ensure_ascii=False, default=str)
    )


def _guidance_first_tool_result_content(guidance: str, tool_result: Any) -> dict[str, Any]:
    """Put recovery guidance first because history compaction truncates tool results from the end."""
    return {
        "controller_guidance": guidance,
        "tool_result": tool_result,
    }


def _result(status: str, state, reviewed, critic_rejections: int, **extra):
    return {
        "status": status,
        "reviewed_conclusion_count": len(reviewed),
        "critic_rejection_count": critic_rejections,
        **extra,
        "state": state.to_dict(),
    }
