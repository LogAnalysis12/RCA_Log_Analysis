from __future__ import annotations

import json
from typing import Any


def canonical_function_id(value: str | None) -> str:
    value = (value or "").strip()
    return value.split(":", 1)[1] if value.startswith("Function:") else value


def conclusion_key(conclusion: dict[str, Any]) -> str:
    stable = {
        "function_id": canonical_function_id(conclusion.get("root_cause_function_id")),
        "relative_path": conclusion.get("relative_path", ""),
        "line": conclusion.get("line"),
        "suspicious_value": conclusion.get("suspicious_value", ""),
        "value_origin": conclusion.get("value_origin", ""),
        "external_boundary": bool(conclusion.get("external_boundary", False)),
    }
    return json.dumps(stable, sort_keys=True, ensure_ascii=False)


def evidence_signature(state) -> frozenset[str]:
    evidence = {
        f"SOURCE:{canonical_function_id(function_id)}"
        for function_id in state.read_functions
    }
    for item in state.tool_history:
        tool = item.get("tool", "")
        if not tool or tool == "propose_conclusion":
            continue
        tool_input = dict(item.get("input", {}))
        if "function_id" in tool_input:
            tool_input["function_id"] = canonical_function_id(tool_input["function_id"])
        if "exclude_function_id" in tool_input:
            tool_input["exclude_function_id"] = canonical_function_id(
                tool_input["exclude_function_id"]
            )
        evidence.add(
            f"TOOL:{tool}:{json.dumps(tool_input, sort_keys=True, default=str)}"
        )
    return frozenset(evidence)


def _truncate_text(value: str, maximum: int) -> tuple[str, bool]:
    if len(value) <= maximum:
        return value, False
    return value[:maximum] + "\n...[TRUNCATED]", True


def compact_tool_result(
    tool: str,
    result: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    source_limit = int(config.get("max_source_characters_per_function", 12_000))
    result_limit = int(config.get("max_tool_result_characters", 16_000))

    # Preserve tool errors before applying tool-specific result shaping.
    # Otherwise, a blocked read_function_source request can look like an
    # empty source result and encourage the model to repeat the request.
    if "error" in result:
        return {
            "status": result.get("status", "TOOL_ERROR"),
            "error": str(result.get("error", "Unknown tool error")),
            "continuation_instruction": result.get(
                "continuation_instruction",
                "Do not repeat this failed request. Use the existing evidence "
                "and choose a different investigation action, or propose a "
                "conclusion if the verified evidence is sufficient.",
            ),
        }

    if tool == "read_function_source":
        function = result.get("function") or {}
        source = result.get("source") or {}
        numbered_source, truncated = _truncate_text(
            source.get("numbered_source", ""), source_limit
        )
        compact = {
            "status": result.get("status"),
            "function_id": canonical_function_id(result.get("function_id")),
            "qualified_name": function.get("qualified_name", ""),
            "signature": function.get("signature", ""),
            "relative_path": function.get("relative_path", ""),
            "start_line": source.get("start_line"),
            "end_line": source.get("actual_end_line"),
            "numbered_source": numbered_source,
            "source_truncated": truncated,
        }

        # Preserve the cache signal and continuation guidance. Without these
        # fields, the model sees the source but is not told it was a replay.
        if result.get("cache_hit"):
            compact["cache_hit"] = True

        continuation = result.get("continuation_instruction")
        if continuation:
            compact["continuation_instruction"] = continuation

    elif tool == "find_callers":
        compact = {
            "status": result.get("status"),
            "target_function_id": canonical_function_id(result.get("target_function_id")),
            "caller_count": result.get("caller_count", 0),
            "callers": result.get("callers", []),
            "truncated": result.get("truncated", False),
        }
    elif tool == "find_callees":
        compact = {
            "status": result.get("status"),
            "caller_function_id": canonical_function_id(result.get("caller_function_id")),
            "project_callees": result.get("project_callees", []),
            "external_callees": result.get("external_callees", []),
            "external_boundary_present": result.get("external_boundary_present", False),
            "truncated": result.get("truncated", False),
        }
    elif tool == "find_spawned_workers":
        compact = {
            "status": result.get("status"),
            "launcher_function_id": canonical_function_id(result.get("launcher_function_id")),
            "launcher_qualified_name": result.get("launcher_qualified_name", ""),
            "spawned_worker_count": result.get("spawned_worker_count", 0),
            "spawned_workers": result.get("spawned_workers", []),
            "truncated": result.get("truncated", False),
        }
    elif tool == "list_fields_read":
        compact = {
            "status": result.get("status"),
            "function_id": canonical_function_id(result.get("function_id")),
            "field_access_count": result.get("field_access_count", 0),
            "fields": result.get("fields", []),
            "truncated": result.get("truncated", False),
        }
    elif tool == "find_field_writers":
        compact = {
            "status": result.get("status"),
            "field_id": result.get("field_id"),
            "field_name": result.get("field_name", ""),
            "writer_count": result.get("writer_count", 0),
            "writers": result.get("writers", []),
            "truncated": result.get("truncated", False),
        }
    else:
        compact = result

    encoded = json.dumps(compact, ensure_ascii=False, default=str)
    if len(encoded) <= result_limit:
        return compact
    return {
        "status": compact.get("status", "OK"),
        "result_truncated": True,
        "summary": encoded[:result_limit],
    }
