#!/usr/bin/env python3
"""Validate extractor artifact contracts and flag dangling graph references.

This validator is read-only with respect to source and extraction artifacts except
for its JSON report. It does not load Neo4j or read ground truth.
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

from extract_repository_graph_rca_v2 import EXTRA_CSV_FIELDS, JSONL_EXTRA_ARTIFACTS

LEGACY_CSV_HEADERS = {
    "calls.csv": ["caller_function_id", "callee_function_id", "callee_name", "qualified_callee_name", "callee_signature", "call_line", "call_column", "call_type", "resolution_status", "call_expression"],
    "data_fields.csv": ["field_id", "repository_id", "field_name", "qualified_name", "field_type", "declaration_relative_path", "declaration_line", "declaration_kind"],
    "data_access.csv": ["function_id", "field_id", "chain", "suffix2", "field_name", "field_type", "access_type", "line", "column"],
    "translation_units.csv": ["relative_path", "status", "duration_seconds", "functions", "calls", "data_fields", "data_accesses", "project_errors", "external_errors", "worker_exit_code", "error"],
}
LEGACY_JSONL_FIELDS = {"functions.jsonl": "function_id"}
IDENTITY_FIELDS = {
    "variables.csv": "variable_id",
    "function_parameters.csv": "parameter_id",
    "variable_accesses.csv": "access_id",
    "call_arguments.csv": "argument_id",
    "data_flows.csv": "flow_id",
    "lifetime_events.csv": "event_id",
    "thread_spawns.csv": "spawn_id",
    "lock_usage.csv": "lock_event_id",
    "pointer_events.csv": "event_id",
    "synchronization_events.csv": "event_id",
    "control_flow_events.csv": "event_id",
    "macro_events.csv": "macro_event_id",
    "types.csv": "type_id",
    "type_relationships.csv": "relationship_id",
    "callable_calls.csv": "call_site_id",
    "call_sites.csv": "call_site_id",
    "function_return_types.csv": "function_id",
    "return_flows.csv": "return_id",
}


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        return list(reader.fieldnames or []), list(reader)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at {path.name}:{line_number}: {exc}") from exc
            if not isinstance(item, dict):
                raise ValueError(f"JSONL item at {path.name}:{line_number} is not an object")
            rows.append(item)
    return rows


def _split_ids(value: Any) -> list[str]:
    return [part for part in str(value or "").split("|") if part]


def validate(output_dir: Path, require_complete: bool = False, strict_references: bool = False) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    counts: dict[str, int] = {}
    csv_rows: dict[str, list[dict[str, str]]] = {}
    jsonl_rows: dict[str, list[dict[str, Any]]] = {}

    expected_csv = {**LEGACY_CSV_HEADERS, **EXTRA_CSV_FIELDS}
    for filename, expected_header in expected_csv.items():
        path = output_dir / filename
        if not path.is_file():
            errors.append(f"missing required artifact: {filename}")
            continue
        try:
            header, rows = read_csv(path)
        except Exception as exc:
            errors.append(f"could not read {filename}: {exc}")
            continue
        if header != expected_header:
            errors.append(f"header mismatch in {filename}: expected {expected_header!r}, got {header!r}")
        csv_rows[filename] = rows
        counts[filename] = len(rows)

    expected_jsonl = {**LEGACY_JSONL_FIELDS, **{name: ("callable_id" if key == "callable_targets" else "symbol_id") for name, key in JSONL_EXTRA_ARTIFACTS.items()}}
    for filename, _ in expected_jsonl.items():
        path = output_dir / filename
        if not path.is_file():
            errors.append(f"missing required artifact: {filename}")
            continue
        try:
            rows = read_jsonl(path)
        except Exception as exc:
            errors.append(f"could not read {filename}: {exc}")
            continue
        jsonl_rows[filename] = rows
        counts[filename] = len(rows)

    # Identity uniqueness: repeated IDs can make CREATE-based import ambiguous.
    for filename, field in IDENTITY_FIELDS.items():
        rows = csv_rows.get(filename, [])
        values = [row.get(field, "") for row in rows]
        values = [value for value in values if value]
        repeated = [value for value, number in Counter(values).items() if number > 1]
        if repeated:
            warnings.append(f"{filename} contains {len(repeated)} repeated {field} values; first examples: {repeated[:5]}")
    for filename, field in expected_jsonl.items():
        values = [str(row.get(field, "") or "") for row in jsonl_rows.get(filename, [])]
        values = [value for value in values if value]
        repeated = [value for value, number in Counter(values).items() if number > 1]
        if repeated:
            warnings.append(f"{filename} contains {len(repeated)} repeated {field} values; first examples: {repeated[:5]}")

    def check_refs(artifact: str, column: str, known: set[str], allow_list: bool = False) -> None:
        for index, row in enumerate(csv_rows.get(artifact, []), start=2):
            raw = row.get(column, "")
            refs = _split_ids(raw) if allow_list else ([raw] if raw else [])
            for ref in refs:
                if ref not in known:
                    warnings.append(f"dangling reference {artifact}:{index} {column}={ref!r}")

    function_ids = {str(row.get("function_id", "")) for row in jsonl_rows.get("functions.jsonl", []) if row.get("function_id")}
    callable_ids = {str(row.get("callable_id", "")) for row in jsonl_rows.get("callable_targets.jsonl", []) if row.get("callable_id")}
    external_ids = {str(row.get("symbol_id", "")) for row in jsonl_rows.get("external_symbols.jsonl", []) if row.get("symbol_id")}
    variable_ids = {str(row.get("variable_id", "")) for row in csv_rows.get("variables.csv", []) if row.get("variable_id")}
    type_ids = {str(row.get("type_id", "")) for row in csv_rows.get("types.csv", []) if row.get("type_id")}

    for artifact in ("variable_accesses.csv", "lifetime_events.csv", "pointer_events.csv", "lock_usage.csv", "synchronization_events.csv", "control_flow_events.csv", "return_flows.csv"):
        check_refs(artifact, "function_id", function_ids)
        check_refs(artifact, "callable_id", callable_ids)
    check_refs("variables.csv", "owner_function_id", function_ids)
    check_refs("variables.csv", "owner_callable_id", callable_ids)
    check_refs("function_parameters.csv", "function_id", function_ids)
    check_refs("function_parameters.csv", "callable_id", callable_ids)
    check_refs("function_parameters.csv", "parameter_id", variable_ids)
    check_refs("variable_accesses.csv", "variable_id", variable_ids)
    check_refs("call_arguments.csv", "caller_function_id", function_ids)
    check_refs("call_arguments.csv", "caller_callable_id", callable_ids)
    check_refs("call_arguments.csv", "callee_function_id", function_ids)
    check_refs("call_arguments.csv", "target_parameter_id", variable_ids)
    check_refs("call_arguments.csv", "argument_type_id", type_ids)
    check_refs("call_arguments.csv", "referenced_variable_ids", variable_ids, allow_list=True)
    check_refs("data_flows.csv", "source_variable_id", variable_ids)
    check_refs("data_flows.csv", "target_variable_id", variable_ids)
    check_refs("data_flows.csv", "target_callable_id", callable_ids)
    check_refs("thread_spawns.csv", "spawner_function_id", function_ids)
    check_refs("thread_spawns.csv", "spawner_callable_id", callable_ids)
    check_refs("thread_spawns.csv", "target_function_id", function_ids)
    check_refs("thread_spawns.csv", "target_callable_id", callable_ids)
    check_refs("thread_spawns.csv", "target_external_symbol_id", external_ids)
    check_refs("thread_spawns.csv", "target_variable_id", variable_ids)
    check_refs("type_relationships.csv", "source_type_id", type_ids)
    check_refs("type_relationships.csv", "target_type_id", type_ids)
    check_refs("types.csv", "pointee_type_id", type_ids)
    check_refs("function_return_types.csv", "function_id", function_ids)
    check_refs("function_return_types.csv", "return_type_id", type_ids)
    check_refs("return_flows.csv", "referenced_variable_ids", variable_ids, allow_list=True)
    check_refs("call_sites.csv", "caller_function_id", function_ids)
    check_refs("call_sites.csv", "caller_callable_id", callable_ids)
    check_refs("call_sites.csv", "callee_function_id", function_ids)
    check_refs("call_sites.csv", "callee_external_symbol_id", external_ids)
    check_refs("call_sites.csv", "callee_variable_id", variable_ids)

    manifest_path = output_dir / "manifest.json"
    report_path = output_dir / "extraction_report.json"
    manifest: dict[str, Any] = {}
    report: dict[str, Any] = {}
    for path, target in ((manifest_path, manifest), (report_path, report)):
        if not path.is_file():
            errors.append(f"missing required metadata: {path.name}")
            continue
        try:
            target.update(json.loads(path.read_text(encoding="utf-8")))
        except Exception as exc:
            errors.append(f"could not read {path.name}: {exc}")
    if manifest and manifest.get("legacy_artifacts_compatible") is not True:
        errors.append("manifest does not confirm legacy artifact compatibility")
    if manifest and manifest.get("static_evidence_only") is not True:
        errors.append("manifest does not declare static-evidence-only output")
    if require_complete:
        if manifest.get("is_partial_run"):
            errors.append("manifest marks this as a partial run")
        if int(report.get("translation_units_failed", 0) or 0) != 0:
            errors.append("extraction report has failed translation units")
        if int(report.get("available_translation_units", report.get("translation_units_recorded", 0)) or 0) != int(report.get("selected_translation_units", report.get("translation_units_recorded", 0)) or 0):
            errors.append("selected translation-unit count does not equal available count")
    if strict_references and warnings:
        errors.append(f"strict reference validation failed because {len(warnings)} warning(s) remain")

    result = {
        "validator_version": "1.0",
        "output_directory": str(output_dir),
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
        "warnings": warnings,
        "artifact_record_counts": counts,
        "manifest_partial": manifest.get("is_partial_run"),
        "translation_units_recorded": report.get("translation_units_recorded"),
        "translation_units_successful": report.get("translation_units_successful"),
        "translation_units_failed": report.get("translation_units_failed"),
        "ground_truth_was_read": False,
        "neo4j_was_written": False,
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate static RCA extraction artifacts before downstream import.")
    parser.add_argument("--output-directory", required=True)
    parser.add_argument("--report", default="", help="Optional report path (default: artifact_validation.json inside output directory)")
    parser.add_argument("--require-complete", action="store_true", help="Fail if the run is partial or has failed translation units")
    parser.add_argument("--strict-references", action="store_true", help="Treat any dangling cross-artifact ID reference as an error")
    args = parser.parse_args()
    out = Path(args.output_directory).resolve()
    result = validate(out, require_complete=args.require_complete, strict_references=args.strict_references)
    report_path = Path(args.report).resolve() if args.report else out / "artifact_validation.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "errors": len(result["errors"]), "warnings": len(result["warnings"]), "report": str(report_path)}, indent=2))
    for item in result["errors"]:
        print(f"ERROR: {item}")
    for item in result["warnings"][:20]:
        print(f"WARNING: {item}")
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
