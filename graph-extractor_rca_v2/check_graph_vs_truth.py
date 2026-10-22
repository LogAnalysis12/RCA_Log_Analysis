#!/usr/bin/env python3
"""Compare extracted RCA evidence with the separate ThreadDemo ground truth.

The extractor never reads ground_truth.json. This checker is a separate acceptance
step and is run only after extraction. It checks each declared ground-truth edge
against neutral CSV/JSONL artifacts, including expected absences for NO_LOCK.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Any


def load_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as stream:
        for line_no, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at {path}:{line_no}: {exc}") from exc
    return rows


def norm(value: Any) -> str:
    value = str(value or "").replace("->", "::").replace(".", "::")
    value = re.sub(r"\s+", "", value)
    while "::::" in value:
        value = value.replace("::::", "::")
    return value.strip(":").lower()


def same_target(actual: str, expected: str) -> bool:
    a, e = norm(actual), norm(expected)
    if not a or not e:
        return False
    return a == e or a.endswith("::" + e) or e.endswith("::" + a) or ("lambda::" + e) in a or ("lambda::" + e) in norm(actual.replace("lambda->", "lambda::"))


def same_file(actual: Any, expected: Any) -> bool:
    a = str(actual or "").replace("\\", "/").lower().lstrip("./")
    e = str(expected or "").replace("\\", "/").lower().lstrip("./")
    return bool(a and e and (a == e or a.endswith("/" + e)))


def near(actual: Any, expected: int, tolerance: int) -> bool:
    try:
        return abs(int(actual or 0) - expected) <= tolerance
    except (TypeError, ValueError):
        return False


def function_matches(row: dict[str, Any], expected_function: str) -> bool:
    values = [row.get("qualified_name"), row.get("function_name"), row.get("function", ""), row.get("spawner_function_name")]
    return any(same_target(v, expected_function) for v in values if v)


def collect_candidates(edge: dict[str, Any], artifacts: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    kind = str(edge.get("kind", "")).upper()
    if kind in {"READ", "WRITE", "READ_WRITE", "POSSIBLE_WRITE"}:
        rows = artifacts["variable_accesses"] + artifacts["data_access"]
        want_access = kind
        result = []
        for row in rows:
            row_kind = str(row.get("access_type", "")).upper()
            function = row.get("function_name") or row.get("qualified_name") or ""
            target = row.get("variable_name") or row.get("qualified_name") or row.get("field_name") or row.get("chain") or row.get("name") or ""
            if row_kind == want_access and function_matches({"qualified_name": function}, edge.get("function", "")) and same_target(target, edge.get("target", "")) and same_file(row.get("relative_path"), edge.get("file", "")) and near(row.get("line"), int(edge.get("line", 0)), 1):
                result.append(row)
        return result
    if kind in {"DELETE", "NEW", "FREE", "RESET", "CLEAR", "ERASE", "RELEASE", "MOVE", "MAKE_UNIQUE", "MAKE_SHARED"}:
        rows = artifacts["lifetime_events"]
        result = []
        for row in rows:
            target = row.get("target_name", "")
            if str(row.get("kind", "")).upper() == kind and function_matches({"qualified_name": row.get("function_name", "")}, edge.get("function", "")) and same_target(target, edge.get("target", "")) and same_file(row.get("relative_path"), edge.get("file", "")) and near(row.get("line"), int(edge.get("line", 0)), 1):
                result.append(row)
            # The core current output stores function_id only on the event; use
            # source function index to resolve it in the common check path below.
        return result
    if kind == "SPAWN":
        return [row for row in artifacts["thread_spawns"]
                if same_target(row.get("spawner_function_name", ""), edge.get("function", ""))
                and (same_target(row.get("target_text", ""), edge.get("target", ""))
                     or same_target(row.get("target_body_calls", ""), edge.get("target", "")))
                and same_file(row.get("relative_path"), edge.get("file", ""))
                and near(row.get("line"), int(edge.get("line", 0)), 1)]
    if kind == "LOCK":
        return [row for row in artifacts["lock_usage"]
                if same_target(row.get("function_name", ""), edge.get("function", ""))
                and same_target(row.get("mutex_text", ""), edge.get("target", ""))
                and str(row.get("event", "")).upper() in {"RAII_ACQUIRE_AND_SCOPE_RELEASE", "ACQUIRE"}
                and same_file(row.get("relative_path"), edge.get("file", ""))
                and near(row.get("line"), int(edge.get("line", 0)), 1)]
    if kind == "RETURN_EMPTY":
        return [row for row in artifacts["control_flow_events"]
                if str(row.get("kind", "")).upper() == "EMPTY_OR_NULL_RETURN"
                and function_matches({"qualified_name": row.get("function_name", "")}, edge.get("function", ""))
                and same_file(row.get("relative_path"), edge.get("file", ""))
                and near(row.get("line"), int(edge.get("line", 0)), 1)]
    if kind == "NO_LOCK":
        # A negative fact cannot be represented as an ordinary LOCK relationship.
        # Verify the affected function exists, no matching mutex lock event exists
        # in that function's source range, and the expected data access is observed.
        function_rows = [f for f in artifacts["functions"] if function_matches(f, edge.get("function", "")) and same_file(f.get("relative_path", ""), edge.get("file", ""))]
        if not function_rows:
            return []
        access_target = str(edge.get("access_target", "") or "")
        nearby_access = [r for r in artifacts["variable_accesses"] + artifacts["data_access"]
                         if function_matches({"qualified_name": r.get("function_name", "") or r.get("qualified_name", "")}, edge.get("function", ""))
                         and (not access_target or same_target(r.get("variable_name") or r.get("field_name") or r.get("chain") or r.get("name") or "", access_target))
                         and same_file(r.get("relative_path", ""), edge.get("file", ""))
                         and near(r.get("line"), int(edge.get("line", 0)), 2)]
        fn_ranges = [(int(f.get("start_line", 0) or 0), int(f.get("end_line", 0) or 0)) for f in function_rows]
        in_function_locks = [r for r in artifacts["lock_usage"]
                             if same_target(r.get("function_name", ""), edge.get("function", ""))
                             and same_file(r.get("relative_path", ""), edge.get("file", ""))
                             and any(a <= int(r.get("line", 0) or 0) <= b for a, b in fn_ranges)
                             and same_target(r.get("mutex_text", ""), edge.get("target", ""))]
        if nearby_access and not in_function_locks:
            return [{"expected_absence_confirmed": True, "function": edge.get("function"), "mutex": edge.get("target"), "line": edge.get("line")}]
        return []
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description="Check extracted graph evidence against ThreadDemo ground truth.")
    parser.add_argument("--output-directory", required=True)
    parser.add_argument("--ground-truth", required=True)
    parser.add_argument("--report", default="")
    args = parser.parse_args()
    out = Path(args.output_directory).resolve()
    truth_path = Path(args.ground_truth).resolve()
    truth = json.loads(truth_path.read_text(encoding="utf-8"))

    artifacts = {
        "functions": load_jsonl(out / "functions.jsonl"),
        "data_access": load_csv(out / "data_access.csv"),
        "variable_accesses": load_csv(out / "variable_accesses.csv"),
        "lifetime_events": load_csv(out / "lifetime_events.csv"),
        "thread_spawns": load_csv(out / "thread_spawns.csv"),
        "lock_usage": load_csv(out / "lock_usage.csv"),
        "control_flow_events": load_csv(out / "control_flow_events.csv"),
    }
    function_name_by_id = {str(row.get("function_id", "")): str(row.get("qualified_name", "")) for row in artifacts["functions"]}
    for artifact_key in ("data_access", "variable_accesses", "lifetime_events", "lock_usage", "control_flow_events"):
        for record in artifacts[artifact_key]:
            record.setdefault("function_name", function_name_by_id.get(str(record.get("function_id", "")), ""))
    rows = []
    for scenario in truth.get("scenarios", []):
        for edge in scenario.get("edges", []):
            candidates = collect_candidates(edge, artifacts)
            found = bool(candidates)
            rows.append({
                "scenario": edge.get("scenario", scenario.get("id", "")),
                "role": edge.get("role", ""), "function": edge.get("function", ""),
                "kind": edge.get("kind", ""), "target": edge.get("target", ""),
                "expected_file": edge.get("file", ""), "expected_line": edge.get("line", ""),
                "status": "FOUND" if found else "MISSING",
                "evidence_mode": "EXPECTED_ABSENCE_CHECK" if edge.get("kind") == "NO_LOCK" and found else "EXTRACTED_RECORD",
                "candidate_count": len(candidates),
                "candidate_details": json.dumps(candidates[:3], ensure_ascii=False, default=str),
            })
    report_path = Path(args.report).resolve() if args.report else out / "ground_truth_check.csv"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]) if rows else ["scenario", "role", "function", "kind", "target", "status"])
        writer.writeheader()
        writer.writerows(rows)
    counts = {status: sum(row["status"] == status for row in rows) for status in ("FOUND", "MISSING")}
    print(json.dumps({"ground_truth_edges": len(rows), **counts, "report": str(report_path)}, indent=2))
    return 0 if counts["MISSING"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
