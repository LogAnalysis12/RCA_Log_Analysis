from __future__ import annotations

from typing import Any

from traversal.compact_results import canonical_function_id


REQUIRED_ROUTE = {
    "FUNCTION_PARAMETER": "find_callers",
    "MEMBER_FIELD": "find_field_writers",
    "GLOBAL_FIELD": "find_field_writers",
    "RETURN_VALUE": "find_callees",
}


def _history_result(state, tool_name: str, function_id: str):
    """Find the latest recorded result for a tool/function pair."""
    target = canonical_function_id(function_id)
    for item in reversed(state.tool_history):
        if item.get("tool") != tool_name:
            continue
        tool_input = item.get("input") or {}
        if canonical_function_id(tool_input.get("function_id")) == target:
            result = item.get("result")
            return result if isinstance(result, dict) else None
    return None


def _function_source(state, function_id: str):
    return state.read_functions.get(canonical_function_id(function_id))


def _source_text(result) -> str:
    if not result:
        return ""
    source = result.get("source") or {}
    text = source.get("source", "")
    return text if isinstance(text, str) else ""


def _parameter_name(value: str):
    import re
    match = re.match(r"^\s*([A-Za-z_]\w*)\s*(?:\(|$)", value or "")
    return match.group(1) if match else None


def _normalize_signature(value: str) -> str:
    import re
    value = re.sub(r"__attribute__\s*\(\(.*?\)\)", "", value or "")
    return re.sub(r"\s+", "", value)


def _is_passthrough_caller(state, target_function, caller, parameter_name: str) -> bool:
    """Detect a caller with the same signature forwarding the parameter unchanged."""
    import re

    target_signature = _normalize_signature(target_function.get("signature", ""))
    caller_signature = _normalize_signature(caller.get("signature", ""))
    if not target_signature or target_signature != caller_signature:
        return False

    caller_id = canonical_function_id(caller.get("function_id"))
    caller_source = _function_source(state, caller_id)
    if not caller_source or caller_source.get("status") != "SOURCE_VERIFIED":
        return False

    target_name = (target_function.get("qualified_name") or "").split("::")[-1]
    sites = [s for s in caller.get("call_sites", []) if isinstance(s, dict)]
    forwards = any(
        target_name
        and re.search(
            rf"\b{re.escape(target_name)}\s*\([^)]*\b{re.escape(parameter_name)}\b",
            site.get("expression", ""),
        )
        for site in sites
    )
    if not forwards:
        return False

    source_text = _source_text(caller_source)
    assigns_parameter = bool(
        re.search(rf"\b{re.escape(parameter_name)}\s*=(?!=)", source_text)
        or re.search(
            rf"\bdelete\s+(?:\[\s*\]\s*)?{re.escape(parameter_name)}\b",
            source_text,
        )
    )
    return not assigns_parameter


def _parameter_causality_reasons(conclusion, state, root_function_id: str) -> list[str]:
    """
    Require provenance tracing when an anchor-function conclusion blames a
    parameter that is forwarded unchanged through another project function.
    """
    import re

    if conclusion.get("value_origin") != "FUNCTION_PARAMETER":
        return []

    anchor = state.anchor or {}
    anchor_id = canonical_function_id(anchor.get("function_id"))
    if root_function_id != anchor_id:
        return []

    parameter = _parameter_name(conclusion.get("suspicious_value", ""))
    if not parameter:
        return []

    anchor_result = _function_source(state, anchor_id)
    target_function = (anchor_result or {}).get("function") or anchor
    direct_result = _history_result(state, "find_callers", anchor_id) or {}
    callers = [
        c for c in direct_result.get("callers", [])
        if isinstance(c, dict)
    ]
    reasons = []

    for caller in callers:
        caller_id = canonical_function_id(caller.get("function_id"))
        caller_source = _function_source(state, caller_id)

        if not caller_source or caller_source.get("status") != "SOURCE_VERIFIED":
            reasons.append(
                f"Read and verify caller source for {caller.get('qualified_name', caller_id)} "
                "before concluding that the parameter origin is understood."
            )
            continue

        if not _is_passthrough_caller(state, target_function, caller, parameter):
            continue

        upstream = _history_result(state, "find_callers", caller_id)
        if upstream is None:
            reasons.append(
                f"{caller.get('qualified_name', caller_id)} forwards parameter "
                f"'{parameter}' unchanged. Call find_callers on that function "
                "and trace the parameter to its origin before concluding."
            )
            continue

        parents = [
            p for p in upstream.get("callers", [])
            if isinstance(p, dict)
        ]

        for parent in parents:
            sites = [
                s for s in parent.get("call_sites", [])
                if isinstance(s, dict)
            ]
            is_spawn_parent = any(
                str(s.get("relationship_type", "")).upper() == "SPAWNS"
                or str(s.get("call_type", "")).upper() == "THREAD_CREATE"
                for s in sites
            )
            if not is_spawn_parent:
                continue

            parent_id = canonical_function_id(parent.get("function_id"))
            parent_name = parent.get("qualified_name", parent_id)
            parent_source = _function_source(state, parent_id)

            if not parent_source or parent_source.get("status") != "SOURCE_VERIFIED":
                reasons.append(
                    f"Read and verify spawning function {parent_name}, then inspect "
                    "the source of its relevant spawned workers."
                )
                continue

            callee_result = _history_result(state, "find_callees", parent_id)
            if callee_result is None:
                reasons.append(
                    f"Inspect the project callees and SPAWNS relationships of "
                    f"{parent_name} before concluding about this shared parameter."
                )
                continue

            spawned = []
            for callee in callee_result.get("project_callees", []):
                if not isinstance(callee, dict):
                    continue
                callee_sites = [
                    s for s in callee.get("call_sites", [])
                    if isinstance(s, dict)
                ]
                if any(
                    str(s.get("relationship_type", "")).upper() == "SPAWNS"
                    or str(s.get("call_type", "")).upper() == "THREAD_CREATE"
                    for s in callee_sites
                ):
                    spawned.append(callee)

            missing = [
                c.get("qualified_name", c.get("function_id", "unknown"))
                for c in spawned
                if (
                    not _function_source(state, c.get("function_id"))
                    or _function_source(state, c.get("function_id")).get("status")
                    != "SOURCE_VERIFIED"
                )
            ]
            if missing:
                reasons.append(
                    "Read and verify the relevant spawned worker source(s): "
                    + ", ".join(missing)
                    + ". Do this before accepting a root cause for the shared parameter."
                )

    # If another verified function changes the same parameter, an anchor
    # conclusion must reconcile that state change rather than considering only
    # the error handler.
    anchor_signature = _normalize_signature(target_function.get("signature", ""))
    mutators = []

    for function_id, result in state.read_functions.items():
        if canonical_function_id(function_id) == anchor_id:
            continue

        function = result.get("function") or {}
        if _normalize_signature(function.get("signature", "")) != anchor_signature:
            continue

        source_text = _source_text(result)
        changes_parameter = bool(
            re.search(
                rf"\bdelete\s+(?:\[\s*\]\s*)?{re.escape(parameter)}\b",
                source_text,
            )
            or re.search(rf"\b{re.escape(parameter)}\s*=(?!=)", source_text)
        )

        if changes_parameter:
            mutators.append(function.get("qualified_name") or function_id)

    explanation = (conclusion.get("explanation") or "").lower()
    unacknowledged = [name for name in mutators if name.lower() not in explanation]

    if unacknowledged:
        reasons.append(
            "Verified source for " + ", ".join(unacknowledged)
            + f" changes parameter '{parameter}'. Reconcile this state change "
            "before attributing the underlying cause to the observing function."
        )

    return reasons


def verify_conclusion(conclusion: dict[str, Any], state) -> dict[str, Any]:
    reasons: list[str] = []
    function_id = canonical_function_id(conclusion.get("root_cause_function_id"))
    source_result = state.read_functions.get(function_id)

    if not source_result:
        reasons.append("The blamed function source was not read.")
    elif source_result.get("status") != "SOURCE_VERIFIED":
        reasons.append("The blamed function source was not hash-verified.")
    else:
        function = source_result.get("function", {})
        source = source_result.get("source", {})
        if conclusion.get("relative_path") != function.get("relative_path"):
            reasons.append("The cited file differs from the verified source file.")
        line = conclusion.get("line")
        start = source.get("start_line")
        end = source.get("actual_end_line")
        if not all(isinstance(value, int) for value in (line, start, end)) or not start <= line <= end:
            reasons.append("The cited line is outside the verified source range.")

    used_tools = [item.get("tool") for item in state.tool_history]
    value_origin = conclusion.get("value_origin", "UNKNOWN")
    required_tool = REQUIRED_ROUTE.get(value_origin)
    if required_tool and required_tool not in used_tools:
        reasons.append(f"Value origin {value_origin} requires {required_tool} before conclusion.")

    suspicious_value = (conclusion.get("suspicious_value") or "").strip().lower()
    if suspicious_value in {"unknown exception", "exception", "error"}:
        reasons.append(
            "An exception message is diagnostic output, not by itself the suspicious application value."
        )

    if conclusion.get("external_boundary"):
        external_seen = any(
            item.get("tool") == "find_callees"
            and item.get("result", {}).get("external_boundary_present", False)
            for item in state.tool_history
        )
        if not external_seen:
            reasons.append("External boundary was claimed without external callee evidence.")
        explanation = (conclusion.get("explanation") or "").lower()
        if any(
            phrase in explanation
            for phrase in (
                "root cause is the external",
                "external api's failure",
                "external api is the root cause",
                "bug in the external",
            )
        ):
            reasons.append(
                "Unavailable external code may be identified as a failure boundary, not as a proven internal root cause."
            )

    # A tool having been called is not proof that parameter provenance
    # was traced far enough to support a root-cause conclusion.
    reasons.extend(_parameter_causality_reasons(conclusion, state, function_id))

    return {
        "passed": not reasons,
        "reasons": reasons,
        "continuation_instruction": reasons[0] if reasons else "",
    }
