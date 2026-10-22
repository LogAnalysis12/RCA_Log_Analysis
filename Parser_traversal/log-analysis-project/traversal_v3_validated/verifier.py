from __future__ import annotations
from typing import Any

REQUIRED_ROUTE = {
    "FUNCTION_PARAMETER": "find_callers",
    "MEMBER_FIELD": "find_field_writers",
    "GLOBAL_FIELD": "find_field_writers",
    "RETURN_VALUE": "find_callees",
}

def verify_conclusion(conclusion: dict[str, Any], state) -> dict[str, Any]:
    reasons: list[str] = []
    function_id = conclusion.get("root_cause_function_id", "")
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
        if not isinstance(line, int) or not isinstance(start, int) or not isinstance(end, int) or not start <= line <= end:
            reasons.append("The cited line is outside the verified source range.")

    origin = conclusion.get("value_origin", "UNKNOWN")
    required_tool = REQUIRED_ROUTE.get(origin)
    used_tools = [entry.get("tool") for entry in state.tool_history]
    if required_tool and required_tool not in used_tools:
        reasons.append(f"Value origin {origin} requires {required_tool} before conclusion.")

    if conclusion.get("external_boundary"):
        external_seen = any(
            entry.get("tool") == "find_callees"
            and entry.get("result", {}).get("external_boundary_present")
            for entry in state.tool_history
        )
        if not external_seen:
            reasons.append("External boundary was claimed but no external callee evidence was retrieved.")

    return {
        "passed": not reasons,
        "reasons": reasons,
        "continuation_instruction": reasons[0] if reasons else "",
    }
