from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def _load(value: dict[str, Any] | str | Path) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    result = json.loads(Path(value).read_text(encoding="utf-8-sig"))
    if not isinstance(result, dict):
        raise ValueError("RCA result JSON must contain an object.")
    return result


def _value(value: Any, default: str = "Not available") -> str:
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _cell(value: Any) -> str:
    return _value(value, "Not recorded").replace("|", r"\|").replace("\n", "<br>")


def _issue(result: dict[str, Any]) -> dict[str, Any]:
    state = result.get("state") if isinstance(result.get("state"), dict) else {}
    stored = state.get("issue") if isinstance(state.get("issue"), dict) else {}
    selected = result.get("selected_issue") if isinstance(result.get("selected_issue"), dict) else {}
    return {**stored, **selected}


def _functions(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    state = result.get("state") if isinstance(result.get("state"), dict) else {}
    value = state.get("read_functions")
    return value if isinstance(value, dict) else {}


def _history(result: dict[str, Any]) -> list[dict[str, Any]]:
    state = result.get("state") if isinstance(result.get("state"), dict) else {}
    return [item for item in state.get("tool_history", []) if isinstance(item, dict)] if isinstance(state.get("tool_history"), list) else []


def _fn(record: dict[str, Any], fallback: Any = None) -> dict[str, Any]:
    value = record.get("function")
    return value if isinstance(value, dict) else {"qualified_name": fallback}


def _src(record: dict[str, Any]) -> dict[str, Any]:
    value = record.get("source")
    return value if isinstance(value, dict) else {}


def _line(numbered_source: Any, line_number: Any) -> str | None:
    if not isinstance(numbered_source, str) or not isinstance(line_number, int):
        return None
    prefix = f"{line_number} |"
    for line in numbered_source.splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            return stripped[len(prefix):].strip()
    return None


def _outcome(result: dict[str, Any]) -> tuple[str, str]:
    status = str(result.get("status", "UNKNOWN")).upper()
    critic = result.get("critic") if isinstance(result.get("critic"), dict) else {}
    verdict = str(critic.get("verdict", "")).upper()
    if status == "ACCEPTED" and verdict != "REJECT":
        return "Accepted", "The configured workflow accepted the conclusion. This is not independent proof that it is correct in every runtime execution."
    if status in {"NOT_ACCEPTED", "REJECTED"} or verdict == "REJECT":
        return "Rejected", "The configured workflow did not accept the conclusion. It should not be treated as an established root cause."
    return "Inconclusive or failed", f"The traversal ended with status `{status}` and does not establish a root cause."


def _tool_finding(tool: str, item: dict[str, Any]) -> str:
    request = item.get("input") if isinstance(item.get("input"), dict) else {}
    data = item.get("result") if isinstance(item.get("result"), dict) else {}
    if tool == "read_function_source":
        function = _fn(data, request.get("function_id"))
        source = _src(data)
        return f"Source read status `{data.get('status', 'not recorded')}` for `{function.get('qualified_name') or request.get('function_id', 'function')}`, lines {source.get('start_line', '?')}-{source.get('actual_end_line', '?')}."
    if tool == "list_fields_read":
        fields = [field for field in data.get("fields", []) if isinstance(field, dict) and not field.get("ignored")]
        findings = [f"{field.get('access', {}).get('type', 'access')} `{field.get('qualified_name') or field.get('field_name')}` at line {field.get('access', {}).get('line', 'not returned')}" for field in fields]
        return f"Found {len(fields)} relevant field access(es): {'; '.join(findings) if findings else 'none returned'}."
    if tool == "find_field_writers":
        writers = [writer for writer in data.get("writers", []) if isinstance(writer, dict)]
        findings = []
        for writer in writers:
            sites = [site for site in writer.get("call_sites", []) if isinstance(site, dict)]
            lines = ", ".join(str(site.get("line", "?")) for site in sites) or "line not returned"
            findings.append(f"{writer.get('qualified_name') or writer.get('function_id')} at {writer.get('relative_path', 'file not returned')}:{lines}")
        return f"Found {len(writers)} writer(s) for `{data.get('qualified_name') or data.get('field_name') or request.get('field_id', 'field')}`: {'; '.join(findings) if findings else 'none returned'}."
    if tool in {"find_callers", "find_callees"}:
        key = "callers" if tool == "find_callers" else "project_callees"
        nodes = [node for node in data.get(key, []) if isinstance(node, dict)]
        names = ", ".join(str(node.get("qualified_name") or node.get("function_id")) for node in nodes)
        relation = "caller" if tool == "find_callers" else "project callee"
        return f"Found {len(nodes)} {relation}(s): {names or 'none returned'}."
    if tool == "propose_conclusion":
        return "Investigator submitted a proposal for deterministic verification and Critic review."
    return f"Recorded result status: `{data.get('status', 'not recorded')}`."


def _evidence_rows(result: dict[str, Any]) -> list[tuple[str, str, str, str, str]]:
    functions = _functions(result)
    history = _history(result)
    conclusion = result.get("conclusion") if isinstance(result.get("conclusion"), dict) else {}
    root_id = str(conclusion.get("root_cause_function_id", "")).removeprefix("Function:")
    rows: list[tuple[str, str, str, str, str]] = []
    for function_id, record in functions.items():
        if not isinstance(record, dict):
            continue
        function = _fn(record, function_id)
        source = _src(record)
        status = record.get("status", "not recorded")
        finding = f"Retained source status: `{status}`; source hash was not rechecked by this report."
        if str(function_id).removeprefix("Function:") == root_id or record.get("function_id") == root_id:
            snippet = _line(source.get("numbered_source"), conclusion.get("line"))
            if snippet is not None:
                finding += f" Stored source at cited line {conclusion.get('line')}: {snippet}"
        rows.append((
            f"Direct source: `{status}`",
            function.get("qualified_name") or function_id,
            function.get("relative_path") or source.get("relative_path") or "Not recorded",
            f"{source.get('start_line', '?')}-{source.get('actual_end_line', source.get('end_line', '?'))}",
            finding,
        ))

    for item in history:
        tool = str(item.get("tool", ""))
        data = item.get("result") if isinstance(item.get("result"), dict) else {}
        request = item.get("input") if isinstance(item.get("input"), dict) else {}
        if tool == "list_fields_read":
            function_id = str(data.get("function_id") or request.get("function_id") or "")
            record = functions.get(function_id.removeprefix("Function:"), {})
            function = _fn(record, function_id)
            for field in data.get("fields", []):
                if not isinstance(field, dict) or field.get("ignored"):
                    continue
                access = field.get("access") if isinstance(field.get("access"), dict) else {}
                rows.append((
                    f"Graph field access: {access.get('type', 'unknown')}",
                    function.get("qualified_name") or function_id or "Not recorded",
                    function.get("relative_path") or field.get("declaration_relative_path") or "Not recorded",
                    access.get("line", "Not returned"),
                    f"Graph result reports `{field.get('qualified_name') or field.get('field_name', 'field')}` as a {access.get('type', 'reported')} access; this is graph evidence, not source text.",
                ))
        elif tool == "find_field_writers":
            for writer in data.get("writers", []):
                if not isinstance(writer, dict):
                    continue
                sites = [site for site in writer.get("call_sites", []) if isinstance(site, dict)]
                rows.append((
                    "Graph field writer",
                    writer.get("qualified_name") or writer.get("function_id", "Not recorded"),
                    writer.get("relative_path", "Not recorded"),
                    ", ".join(str(site.get("line", "?")) for site in sites) or "Not returned",
                    f"Graph result reports a write to `{data.get('qualified_name') or data.get('field_name', 'field')}`; confirm it in retained source.",
                ))
        elif tool in {"find_callers", "find_callees"}:
            key = "callers" if tool == "find_callers" else "project_callees"
            relation = "caller" if tool == "find_callers" else "callee"
            for node in data.get(key, []):
                if isinstance(node, dict):
                    sites = [site for site in node.get("call_sites", []) if isinstance(site, dict)]
                    rows.append((
                        "Graph call relationship",
                        node.get("qualified_name") or node.get("function_id", "Not recorded"),
                        node.get("relative_path", "Not recorded"),
                        ", ".join(str(site.get("line", "?")) for site in sites) or "Not returned",
                        f"Graph result reports a {relation}; it does not establish runtime ordering.",
                    ))

    if not rows:
        rows.append(("No source or graph evidence recorded", "Not available", "Not available", "Not available", "The result contains no verified-source or graph-query evidence records."))
    if conclusion:
        root = next((record for key, record in functions.items() if str(key).removeprefix("Function:") == root_id), None)
        source_status = root.get("status") if isinstance(root, dict) else "not found in read_functions"
        rows.append((
            "Investigator conclusion (inference)",
            conclusion.get("root_cause_qualified_name") or root_id or "Not recorded",
            conclusion.get("relative_path", "Not recorded"),
            conclusion.get("line", "Not recorded"),
            f"Investigator cites this location; source status in the result: `{source_status}`. This remains an inference from supplied evidence.",
        ))
    return rows


def _render_report(
    result: dict[str, Any],
    result_path: Path | None,
    trace_directory: Path | None,
    trace_summary: dict[str, Any] | None,
) -> str:
    issue = _issue(result)
    functions = _functions(result)
    history = _history(result)
    conclusion = result.get("conclusion") if isinstance(result.get("conclusion"), dict) else {}
    verification = result.get("verification") if isinstance(result.get("verification"), dict) else {}
    critic = result.get("critic") if isinstance(result.get("critic"), dict) else {}
    metrics = result.get("execution_metrics") if isinstance(result.get("execution_metrics"), dict) else {}
    outcome, description = _outcome(result)
    error = issue.get("normalized_message") or issue.get("original_message") or issue.get("representative_error")
    if isinstance(error, dict):
        error = error.get("message") or error
    anchor_resolution = issue.get("anchor_resolution") if isinstance(issue.get("anchor_resolution"), dict) else {}
    anchor = anchor_resolution.get("selected_function") if isinstance(anchor_resolution.get("selected_function"), dict) else {}
    reported_function = issue.get("qualified_function") or anchor.get("qualified_name")
    root_name = conclusion.get("root_cause_qualified_name") or conclusion.get("root_cause_function_id")
    lines = ["# Root Cause Analysis Report", "", "## 1. Executive Summary", ""]
    if conclusion and outcome == "Accepted":
        lines.append(f"The reported issue was `{_value(error)}` in `{_value(reported_function)}`. The investigator identified `{_value(root_name)}` as the root-cause function and the configured workflow accepted the conclusion. This acceptance is not independent proof for every runtime execution.")
    elif conclusion and outcome == "Rejected":
        lines.append(f"The reported issue was `{_value(error)}` in `{_value(reported_function)}`. The investigator proposed `{_value(root_name)}`, but the workflow rejected it. It should not be treated as an established root cause.")
    else:
        lines.append(f"The investigation ended with status `{_value(result.get('status'), 'UNKNOWN')}` for `{_value(error)}`. The result is inconclusive or incomplete and does not establish a root cause.")
    lines.extend(["", f"**Workflow outcome:** {outcome}. {description}", "", "## 2. Reported Error", ""])
    lines.extend([
        f"- Fingerprint ID: {_value(issue.get('fingerprint_id'))}",
        f"- Occurrences: {_value(issue.get('occurrence_count'))}",
        f"- Reported function: {_value(reported_function)}",
        f"- Original/normalized error message: {_value(error)}",
        f"- Source file: {_value(anchor.get('relative_path') or issue.get('relative_path'))}",
        f"- Anchor resolution status: {_value(anchor_resolution.get('status') or issue.get('anchor_status'))}",
        "",
    ])
    if error and "unknown exception" in str(error).lower():
        lines.append("In plain English, the log says an exception occurred but does not name the triggering condition. The message is a symptom, not the underlying cause.")
    else:
        lines.append("The message describes the reported symptom; by itself it does not establish the underlying cause.")

    lines.extend(["", "## 3. Root Cause Identified", ""])
    if conclusion:
        root_id = str(conclusion.get("root_cause_function_id", "")).removeprefix("Function:")
        root = next((record for key, record in functions.items() if str(key).removeprefix("Function:") == root_id), {})
        source = _src(root) if isinstance(root, dict) else {}
        source_status = root.get("status", "not found in read_functions") if isinstance(root, dict) else "not found in read_functions"
        lines.extend([
            f"- Root-cause function: {_value(root_name)}",
            f"- Source location: {_value(conclusion.get('relative_path'))}:{_value(conclusion.get('line'))}",
            f"- Source verification status: `{source_status}`",
            f"- Confidence reported by investigator: {_value(conclusion.get('confidence'))}",
            f"- Suspicious value: {_value(conclusion.get('suspicious_value'))}",
            f"- Investigator's original explanation: {_value(conclusion.get('explanation'))}",
        ])
        snippet = _line(source.get("numbered_source"), conclusion.get("line"))
        if snippet is not None:
            lines.append(f"Stored source at cited line {conclusion.get('line')}: {snippet}")
        if outcome != "Accepted":
            lines.append("This proposal was not accepted and is not an established root cause.")
    else:
        lines.append("No conclusion object was recorded. No root cause is asserted by this report.")

    lines.extend(["", "## 4. How the Problem Happens", ""])
    mechanism = []
    if conclusion:
        root = next((record for key, record in functions.items() if str(key).removeprefix("Function:") == str(conclusion.get("root_cause_function_id", "")).removeprefix("Function:")), {})
        source = _src(root) if isinstance(root, dict) else {}
        snippet = _line(source.get("numbered_source"), conclusion.get("line"))
        if snippet:
            mechanism.append(f"Retained source for `{_value(root_name)}` at line {conclusion.get('line')} contains `{snippet}`.")
        for item in history:
            tool = str(item.get("tool", ""))
            if tool in {"list_fields_read", "find_field_writers", "find_callers", "find_callees"}:
                finding = _tool_finding(tool, item)
                if "none returned" not in finding:
                    mechanism.append(f"Recorded graph result: {finding}")
        if conclusion.get("explanation"):
            mechanism.append(f"The causal connection is the investigator's explanation, not an independent runtime observation: {_value(conclusion.get('explanation'))}")
    lines.extend(f"{index}. {item}" for index, item in enumerate(mechanism, 1))
    if not mechanism:
        lines.append("The saved result contains insufficient source or graph detail to describe a supported execution sequence.")

    lines.extend(["", "## 5. Evidence from Source Code", "", "| Evidence | Function | File | Line | What the evidence establishes |", "|---|---|---|---:|---|"])
    lines.extend("| " + " | ".join(_cell(value) for value in row) + " |" for row in _evidence_rows(result))

    lines.extend(["", "## 6. Investigation Path", ""])
    if not history:
        lines.append("No tool history was recorded.")
    else:
        for index, item in enumerate(history, 1):
            tool = str(item.get("tool", "unknown"))
            request = item.get("input") if isinstance(item.get("input"), dict) else {}
            target = request.get("function_id") or request.get("field_id") or "target not recorded"
            record = functions.get(str(target).removeprefix("Function:"), {})
            function = _fn(record, target)
            target_name = function.get("qualified_name") or target
            lines.append(f"{index}. **`{tool}`** on `{target_name}`: {_tool_finding(tool, item)}")

    verifier_passed = verification.get("passed")
    verifier_status = "Passed" if verifier_passed is True else "Failed" if verifier_passed is False else "Not recorded"
    verdict = str(critic.get("verdict", "Not recorded")).upper()
    try:
        rejection_count = int(result.get("critic_rejection_count", 0) or 0)
    except (TypeError, ValueError):
        rejection_count = 0
    critic_rejected = verdict == "REJECT" or rejection_count > 0
    verifier_reasons = verification.get("reasons") if isinstance(verification.get("reasons"), list) else []
    critic_reasons = critic.get("reasons") if isinstance(critic.get("reasons"), list) else []
    lines.extend([
        "", "## 7. Verification and Critic Review", "",
        f"- Deterministic verifier status: **{verifier_status}**",
        f"- Verifier reasons: {'; '.join(map(str, verifier_reasons)) if verifier_reasons else ('None recorded' if verifier_passed is True else 'Not recorded')}",
        f"- Critic verdict: **{verdict}**",
        f"- Critic reasons: {'; '.join(map(str, critic_reasons)) if critic_reasons else 'None recorded'}",
        f"- Critic rejection observed: **{'Yes' if critic_rejected else 'No' if critic else 'Not recorded'}**",
        "",
        "Verifier passage means the configured checks passed for the supplied evidence and citation. Critic acceptance means the supplied case was accepted by that review step; neither proves every runtime execution.",
    ])

    source_text = "\n".join(str(_src(record).get("source", "")) for record in functions.values() if isinstance(record, dict))
    verified_count = sum(1 for record in functions.values() if isinstance(record, dict) and record.get("status") == "SOURCE_VERIFIED")
    lines.extend(["", "## 8. Confidence and Limitations", ""])
    lines.append(f"- Investigator-stated confidence: {_value(conclusion.get('confidence')) if conclusion else 'Not available'}; this is not a calibrated probability.")
    lines.append(f"- Source records marked `SOURCE_VERIFIED`: {verified_count}.")
    lines.append("- Runtime reproduction or execution evidence: not represented in this RCA result.")
    sync_terms = ("mutex", "lock_guard", "unique_lock", "shared_lock", "atomic", "critical_section", "synchronized")
    if any(term in source_text.lower() for term in sync_terms):
        lines.append("- Synchronization inspection: synchronization-like text appears in retained excerpts; its adequacy is not assessed.")
    elif source_text:
        lines.append("- Synchronization inspection: no synchronization primitive is shown in retained excerpts. This does not establish synchronization is absent elsewhere.")
    else:
        lines.append("- Synchronization inspection: unavailable because no source excerpts were retained.")
    if error and any(term in str(error).lower() for term in ("unknown exception", "exception occured", "exception occurred")):
        lines.append("- Error-to-explanation match: the message is generic and does not name the proposed cause; the connection is an investigator inference.")
    else:
        lines.append("- Error-to-explanation match: not established by message text alone.")
    unverified = [str(_fn(record, key).get("qualified_name") or key) for key, record in functions.items() if isinstance(record, dict) and record.get("status") != "SOURCE_VERIFIED"]
    lines.append("- Missing source information: " + (", ".join(unverified) if unverified else "No retained function is marked unverified; unqueried code is outside this result."))
    if not functions and not history:
        lines.append("- No source or graph evidence recorded in the result.")
    else:
        lines.append("- Graph coverage is limited to functions and relationships actually returned in the recorded tool history.")
    if verifier_passed is False:
        lines.append("- The deterministic verifier failed; its reasons limit confidence in the proposal.")
    if critic_rejected:
        lines.append("- The Critic rejected at least one proposal; review the reasons above.")

    lines.extend(["", "## 9. Recommended Next Steps", ""])
    next_steps = []
    if outcome != "Accepted":
        next_steps.append("Address verifier or Critic concerns and gather additional verified evidence before treating any proposal as a root cause.")
    if conclusion:
        next_steps.append(f"Review `{_value(conclusion.get('relative_path'))}` around line `{_value(conclusion.get('line'))}` in the current source revision.")
    if any(term in str(conclusion.get("explanation", "")).lower() for term in ("race", "concurr", "shared state", "synchroniz", "overlap")):
        next_steps.append("If concurrency is part of the proposed mechanism, inspect synchronization across relevant accesses and validate with a targeted concurrent test or race detector.")
    else:
        next_steps.append("Validate the proposed value flow or behavior with an appropriate test or runtime observation.")
    lines.extend(f"{index}. {step}" for index, step in enumerate(dict.fromkeys(next_steps), 1))

    lines.extend(["", "## 10. Run Metrics", ""])
    for label, key, suffix in (
        ("Start time (UTC)", "started_at_utc", ""),
        ("Completion time (UTC)", "completed_at_utc", ""),
        ("Elapsed duration (seconds)", "elapsed_seconds", ""),
        ("Investigator calls", "investigator_calls", ""),
        ("Critic calls", "critic_calls", ""),
        ("Budget-accounted model calls", "bedrock_calls", ""),
        ("Budget-accounted input tokens", "input_tokens", ""),
        ("Budget-accounted output tokens", "output_tokens", ""),
        ("Budget-accounted total tokens", "total_tokens", ""),
        ("Budget estimated cost (USD)", "estimated_cost_usd", ""),
    ):
        lines.append(f"- {label}: {_value(metrics.get(key))}{suffix}")
    trace_summary = trace_summary if isinstance(trace_summary, dict) else {}
    trace_metrics = (
        ("Provider", "provider"),
        ("Model", "model"),
        ("Total model calls (actual HTTP attempts)", "actual_api_calls"),
        ("Actual HTTP API attempts in trace", "actual_api_calls"),
        ("Trace provider-reported input tokens", "total_input_tokens"),
        ("Trace provider-reported output tokens", "total_output_tokens"),
        ("Trace provider-reported total tokens", "provider_reported_total_tokens"),
        ("Trace estimated cost (USD)", "estimated_total_cost_usd"),
    )
    for label, key in trace_metrics:
        lines.append(f"- {label}: {_value(trace_summary.get(key))}")
    lines.append(f"- JSON result: `{result_path}`" if result_path else "- JSON result path: Not available")
    lines.append(f"- LLM trace directory: `{trace_directory}`" if trace_directory else "- LLM trace directory: Not available")
    return "\n".join(lines) + "\n"


def generate_human_report(
    result_json: dict[str, Any] | str | Path,
    output_path: str | Path,
    *,
    result_path: str | Path | None = None,
    trace_directory: str | Path | None = None,
    trace_summary: dict[str, Any] | None = None,
) -> Path:
    """Generate a deterministic Markdown report without calling an LLM."""
    if isinstance(result_json, dict):
        result = result_json
        source_path = Path(result_path) if result_path is not None else None
    else:
        source_path = Path(result_json)
        result = _load(source_path)
    trace_path = Path(trace_directory) if trace_directory is not None else None
    if trace_summary is None and trace_path is not None:
        try:
            trace_summary = _load(trace_path / "llm_trace_summary.json")
        except (OSError, json.JSONDecodeError, ValueError):
            trace_summary = None
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(_render_report(result, source_path, trace_path, trace_summary), encoding="utf-8")
    return destination