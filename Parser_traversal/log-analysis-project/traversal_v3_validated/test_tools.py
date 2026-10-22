from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from traversal.neptune_tools import NeptuneTraversalTools


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def resolve_path(root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def selected_issue(issues: list[dict[str, Any]]) -> dict[str, Any]:
    return next(
        issue for issue in issues
        if issue.get("anchor_resolution", {}).get("status") == "RESOLVED"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="./config.json")
    parser.add_argument("--issues", default="./output/task31_anchors/resolved_issues.json")
    parser.add_argument("--output", default="./output/traversal_tool_validation_v3.json")
    args = parser.parse_args()
    try:
        config_path = Path(args.config).resolve()
        root = config_path.parent
        config = load_json(config_path)
        n = config["neptune"]
        t = config.get("traversal", {})
        tools = NeptuneTraversalTools(
            endpoint=n["endpoint"],
            region=n.get("region", "us-east-1"),
            port=n.get("port", 8182),
            profile=n.get("profile") or None,
            source_root=resolve_path(root, config["source_root"]),
            ignored_fields=t.get("ignored_fields", []),
            ignored_project_callees=t.get("ignored_project_callees", []),
            ignored_external_prefixes=t.get("ignored_external_prefixes", []),
        )
        issue = selected_issue(load_json(resolve_path(root, args.issues)))
        anchor = issue["anchor_resolution"]["selected_function"]
        function_id = anchor["function_id"]

        engine = tools.get_engine_status()
        source = tools.read_function_source(function_id)
        callers = tools.find_callers(function_id, t.get("max_callers", 100))
        callees = tools.find_callees(function_id, t.get("max_callees", 200))
        fields = tools.find_fields_read(function_id, include_ignored=False)

        relevant_external_names = [x["qualified_name"] for x in callees["external_callees"]]
        mcd_preserved = any(name.startswith("asam::mcd::MCDLogicalLink") for name in relevant_external_names)
        project_dedup_ok = callees["unique_project_callee_count"] <= callees["project_call_occurrence_count"]
        external_dedup_ok = callees["unique_external_symbol_count"] <= callees["external_call_occurrence_count"]
        call_sites_preserved = all(x["call_occurrence_count"] == len(x["call_sites"]) for x in callees["all_project_callees_for_audit"] + callees["all_external_callees_for_audit"])

        validation = {
            "neptune_healthy": engine["status"] == "healthy",
            "source_verified": source["status"] == "SOURCE_VERIFIED",
            "callers_deduplicated": callers["unique_caller_count"] <= callers["call_occurrence_count"],
            "project_callees_deduplicated": project_dedup_ok,
            "external_callees_deduplicated": external_dedup_ok,
            "all_call_sites_preserved": call_sites_preserved,
            "project_noise_filtered": len(callees["ignored_project_callees"]) > 0,
            "external_noise_filtered": len(callees["ignored_external_callees"]) > 0,
            "mcd_logical_link_boundary_preserved": mcd_preserved,
            "logging_fields_filtered": fields["relevant_field_access_count"] == 0 and fields["ignored_field_access_count"] == 7,
        }
        summary = {
            "caller_occurrences": callers["call_occurrence_count"],
            "unique_callers": callers["unique_caller_count"],
            "project_call_occurrences": callees["project_call_occurrence_count"],
            "unique_project_callees": callees["unique_project_callee_count"],
            "relevant_project_callees": callees["relevant_project_callee_count"],
            "ignored_project_callees": len(callees["ignored_project_callees"]),
            "external_call_occurrences": callees["external_call_occurrence_count"],
            "unique_external_symbols": callees["unique_external_symbol_count"],
            "relevant_external_callees": callees["relevant_external_callee_count"],
            "ignored_external_callees": len(callees["ignored_external_callees"]),
            "recommended_initial_route": "FIND_CALLERS" if fields["relevant_field_access_count"] == 0 else "INVESTIGATOR_DECIDES_FROM_SOURCE",
        }
        report = {
            "validation": validation,
            "summary": summary,
            "engine": engine,
            "source": source,
            "callers": callers,
            "callees": callees,
            "fields": fields,
        }
        output = resolve_path(root, args.output)
        output.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print("\n========== TRAVERSAL TOOL VALIDATION V3 ==========")
        for key, value in validation.items():
            print(f"{key}: {value}")
        for key, value in summary.items():
            print(f"{key}: {value}")
        print(f"Report: {output}")
        print("==================================================")
        return 0 if all(validation.values()) else 1
    except Exception as error:
        print(f"TRAVERSAL TOOL TEST FAILED: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
