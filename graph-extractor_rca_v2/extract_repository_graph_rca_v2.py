from __future__ import annotations

import os

import argparse
import csv
import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


EXTRA_CSV_FIELDS = {
    "variables.csv": ["variable_id", "repository_id", "name", "qualified_name", "declaration_kind", "clang_cursor_kind", "owner_function_id", "owner_callable_id", "owner_function_name", "relative_path", "line", "column", "usr", "type_id", "type_spelling", "canonical_type", "clang_type_kind", "pointer_depth", "reference_kind", "pointee_type", "is_pointer_like", "is_function_pointer", "smart_pointer_kind", "ownership_model_hint", "is_const", "is_volatile", "storage_class"],
    "function_parameters.csv": ["parameter_id", "function_id", "callable_id", "function_name", "parameter_index", "parameter_name", "type_id", "type_spelling", "canonical_type", "pointer_depth", "reference_kind", "smart_pointer_kind", "ownership_model_hint", "is_const", "relative_path", "line", "column"],
    "variable_accesses.csv": ["access_id", "function_id", "callable_id", "function_name", "variable_id", "variable_name", "declaration_kind", "access_type", "expression", "relative_path", "line", "column", "type_id", "pointer_like", "smart_pointer_kind", "evidence_grade"],
    "call_arguments.csv": ["argument_id", "call_site_id", "caller_function_id", "caller_callable_id", "callee_function_id", "callee_name", "call_line", "call_column", "argument_index", "target_parameter_id", "target_parameter_name", "flow_kind", "argument_text", "argument_type_id", "argument_type", "argument_canonical_type", "referenced_variable_ids", "referenced_variable_names", "address_of", "uses_std_ref", "uses_std_move", "uses_std_forward", "pointer_depth", "smart_pointer_kind", "evidence_grade"],
    "data_flows.csv": ["flow_id", "flow_kind", "source_variable_id", "target_variable_id", "target_callable_id", "source_text", "target_text", "relative_path", "line", "evidence_grade", "interpretation"],
    "lifetime_events.csv": ["event_id", "function_id", "callable_id", "function_name", "target_kind", "target_id", "target_name", "kind", "line", "column", "expression", "relative_path", "macro_event_ids", "macro_event_names", "event_semantics", "evidence_grade"],
    "thread_spawns.csv": ["spawn_id", "spawner_function_id", "spawner_callable_id", "spawner_function_name", "api", "spawn_kind", "target_function_id", "target_callable_id", "target_external_symbol_id", "target_variable_id", "target_kind", "target_text", "target_expression", "target_body_calls", "args_text", "argument_variable_names", "line", "column", "relative_path", "resolution_status", "evidence_grade"],
    "lock_usage.csv": ["lock_event_id", "function_id", "callable_id", "function_name", "mutex_id", "mutex_text", "lock_variable_id", "kind", "event", "line", "scope_start_line", "scope_end_line", "relative_path", "expression", "scope_semantics", "evidence_grade"],
    "pointer_events.csv": ["event_id", "function_id", "callable_id", "kind", "target_id", "target_name", "source_id", "source_name", "target_type_id", "source_type_id", "expression", "line", "column", "relative_path", "macro_event_ids", "macro_event_names", "evidence_grade"],
    "synchronization_events.csv": ["event_id", "function_id", "callable_id", "kind", "primitive", "target_id", "target_name", "line", "column", "relative_path", "expression", "memory_order_text", "evidence_grade"],
    "control_flow_events.csv": ["event_id", "function_id", "callable_id", "function_name", "kind", "target_id", "target_name", "line", "column", "relative_path", "condition_text", "evidence_grade"],
    "macro_events.csv": ["macro_event_id", "event_kind", "macro_name", "relative_path", "line", "column", "tokens", "evidence_grade", "note"],
    "types.csv": ["type_id", "repository_id", "name", "qualified_name", "type_kind", "relative_path", "line", "start_line", "end_line", "usr", "canonical_type", "clang_type_kind", "pointer_depth", "reference_kind", "pointee_type_id", "pointee_type", "smart_pointer_kind", "ownership_model_hint", "is_const", "is_volatile", "is_function_pointer", "evidence_grade"],
    "type_relationships.csv": ["relationship_id", "relationship_type", "source_type_id", "source_type_name", "target_type_id", "target_type_name", "child_type_id", "child_type_name", "base_type_id", "base_type_name", "relative_path", "line", "evidence_grade"],
    "callable_calls.csv": ["call_site_id", "caller_callable_id", "callee_function_id", "callee_name", "relative_path", "line", "column", "call_expression", "resolution_status"],
    "call_sites.csv": ["call_site_id", "caller_function_id", "caller_callable_id", "callee_node_id", "callee_function_id", "callee_external_symbol_id", "callee_variable_id", "callee_name", "callee_signature", "call_type", "resolution_status", "call_expression", "line", "column", "relative_path", "evidence_grade"],
    "function_return_types.csv": ["function_id", "function_name", "return_type_id", "return_type_spelling", "canonical_type", "pointer_depth", "reference_kind", "smart_pointer_kind", "ownership_model_hint", "relative_path", "line", "evidence_grade"],
    "return_flows.csv": ["return_id", "function_id", "callable_id", "function_name", "expression", "referenced_variable_ids", "referenced_variable_names", "return_line", "relative_path", "expression_pointer_like", "evidence_grade"],
}
JSONL_EXTRA_ARTIFACTS = {"callable_targets.jsonl": "callable_targets", "external_symbols.jsonl": "external_symbols"}


def discover_project_translation_units(build_directory: Path, repository_root: Path) -> list[Path]:
    compile_db_path = build_directory / "compile_commands.json"
    entries = json.loads(compile_db_path.read_text(encoding="utf-8-sig"))

    allowed_extensions = {".c", ".cc", ".cpp", ".cxx"}
    excluded_fragments = {"/out/", "/build/", "/cmakefiles/", "/.git/"}
    repository_root = repository_root.resolve()
    selected: dict[str, Path] = {}

    for entry in entries:
        raw_file = Path(entry["file"])
        directory = Path(entry.get("directory") or repository_root)
        source_file = raw_file if raw_file.is_absolute() else directory / raw_file
        source_file = source_file.resolve()

        if not source_file.exists() or source_file.suffix.lower() not in allowed_extensions:
            continue

        try:
            relative_path = source_file.relative_to(repository_root).as_posix()
        except ValueError:
            continue

        normalized = f"/{relative_path.lower()}"
        if any(fragment in normalized for fragment in excluded_fragments):
            continue

        selected[str(source_file).lower()] = source_file

    return sorted(selected.values(), key=lambda path: path.as_posix().lower())


def checkpoint_name(relative_path: str) -> str:
    safe = relative_path.replace("/", "__").replace("\\", "__").replace(":", "_")
    digest = hashlib.sha256(relative_path.encode("utf-8")).hexdigest()[:10]
    return f"{safe}.{digest}.json"


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def write_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as output:
        for record in records:
            output.write(json.dumps(record, ensure_ascii=False) + "\n")


def write_csv(path: Path, fieldnames: list[str], records: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)


def deduplicate(records: list[dict], key_fields: tuple[str, ...]) -> list[dict]:
    seen: set[tuple] = set()
    unique: list[dict] = []
    for record in records:
        key = tuple(record.get(field) for field in key_fields)
        if key in seen:
            continue
        seen.add(key)
        unique.append(record)
    return unique


def merge_successful_checkpoints(checkpoint_files: list[Path], output_directory: Path, repository_id: str, started_at: float) -> dict:
    functions_by_id: dict[str, dict] = {}
    fields_by_id: dict[str, dict] = {}
    all_calls: list[dict] = []
    all_accesses: list[dict] = []
    evidence_keys = ["variables", "function_parameters", "variable_accesses", "call_arguments", "data_flows", "lifetime_events", "thread_spawns", "lock_usage", "pointer_events", "synchronization_events", "control_flow_events", "macro_events", "types", "type_relationships", "callable_targets", "callable_calls", "return_flows", "call_sites", "external_symbols", "function_return_types"]
    evidence_records = {key: [] for key in evidence_keys}
    unit_reports: list[dict] = []

    for checkpoint_file in checkpoint_files:
        checkpoint = json.loads(checkpoint_file.read_text(encoding="utf-8"))
        unit_reports.append({
            "relative_path": checkpoint.get("relative_path"),
            "status": checkpoint.get("status"),
            "duration_seconds": checkpoint.get("duration_seconds", 0),
            "functions": len(checkpoint.get("functions", [])),
            "calls": len(checkpoint.get("calls", [])),
            "data_fields": len(checkpoint.get("data_fields", [])),
            "data_accesses": len(checkpoint.get("data_accesses", [])),
            "variables": len(checkpoint.get("variables", [])),
            "lifetime_events": len(checkpoint.get("lifetime_events", [])),
            "thread_spawns": len(checkpoint.get("thread_spawns", [])),
            "lock_usage": len(checkpoint.get("lock_usage", [])),
            "pointer_events": len(checkpoint.get("pointer_events", [])),
            "synchronization_events": len(checkpoint.get("synchronization_events", [])),
            "macro_events": len(checkpoint.get("macro_events", [])),
            "types": len(checkpoint.get("types", [])),
            "call_sites": len(checkpoint.get("call_sites", [])),
            "external_symbols": len(checkpoint.get("external_symbols", [])),
            "function_return_types": len(checkpoint.get("function_return_types", [])),
            "project_errors": checkpoint.get("diagnostic_summary", {}).get("project_error_count", 0),
            "external_errors": checkpoint.get("diagnostic_summary", {}).get("external_error_count", 0),
            "worker_exit_code": checkpoint.get("worker_exit_code"),
            "error": checkpoint.get("error"),
        })

        if checkpoint.get("status") != "SUCCESS":
            continue

        for record in checkpoint.get("functions", []):
            if record.get("function_id"):
                functions_by_id[record["function_id"]] = record
        for record in checkpoint.get("data_fields", []):
            if record.get("field_id"):
                fields_by_id[record["field_id"]] = record
        all_calls.extend(checkpoint.get("calls", []))
        all_accesses.extend(checkpoint.get("data_accesses", []))
        for evidence_key in evidence_keys:
            evidence_records[evidence_key].extend(checkpoint.get(evidence_key, []))

    functions = list(functions_by_id.values())
    fields = list(fields_by_id.values())
    calls = deduplicate(all_calls, (
        "caller_function_id", "callee_function_id", "qualified_callee_name",
        "call_line", "call_column", "call_type", "call_expression",
    ))
    accesses = deduplicate(all_accesses, (
        "function_id", "field_id", "access_type", "line", "column", "chain",
    ))

    functions.sort(key=lambda r: (r.get("relative_path") or "", int(r.get("start_line") or 0), r.get("qualified_name") or ""))
    fields.sort(key=lambda r: (r.get("declaration_relative_path") or "", int(r.get("declaration_line") or 0), r.get("qualified_name") or ""))
    calls.sort(key=lambda r: (r.get("caller_function_id") or "", int(r.get("call_line") or 0), int(r.get("call_column") or 0)))
    accesses.sort(key=lambda r: (r.get("function_id") or "", int(r.get("line") or 0), int(r.get("column") or 0)))

    function_ids = {r["function_id"] for r in functions if r.get("function_id")}
    field_ids = {r["field_id"] for r in fields if r.get("field_id")}
    missing_callers = [r for r in calls if r.get("caller_function_id") not in function_ids]
    missing_callees = [r for r in calls if r.get("resolution_status") == "RESOLVED_PROJECT" and r.get("callee_function_id") and r.get("callee_function_id") not in function_ids]
    missing_access_functions = [r for r in accesses if r.get("function_id") not in function_ids]
    missing_access_fields = [r for r in accesses if r.get("field_id") not in field_ids]

    write_jsonl(output_directory / "functions.jsonl", functions)
    write_csv(output_directory / "calls.csv", [
        "caller_function_id", "callee_function_id", "callee_name", "qualified_callee_name",
        "callee_signature", "call_line", "call_column", "call_type", "resolution_status", "call_expression",
    ], calls)
    write_csv(output_directory / "data_fields.csv", [
        "field_id", "repository_id", "field_name", "qualified_name", "field_type",
        "declaration_relative_path", "declaration_line", "declaration_kind",
    ], fields)
    write_csv(output_directory / "data_access.csv", [
        "function_id", "field_id", "chain", "suffix2", "field_name", "field_type", "access_type", "line", "column",
    ], accesses)
    # Keep the legacy translation_units.csv contract stable; detailed evidence counts
    # live in extraction_report.json and the RCA-specific artifacts.
    write_csv(output_directory / "translation_units.csv", [
        "relative_path", "status", "duration_seconds", "functions", "calls", "data_fields",
        "data_accesses", "project_errors", "external_errors", "worker_exit_code", "error",
    ], sorted(unit_reports, key=lambda r: r.get("relative_path") or ""))

    # Additive RCA evidence artifacts. Legacy core files above retain their names
    # and schemas; these files are new and can be imported incrementally.
    for filename, columns in EXTRA_CSV_FIELDS.items():
        key = filename[:-4]
        # plural file stem and checkpoint key are identical except artifact naming is explicit.
        records = evidence_records.get(key, [])
        id_fields = ["event_id", "spawn_id", "lock_event_id", "macro_event_id", "relationship_id", "argument_id", "access_id", "flow_id", "return_id", "variable_id", "parameter_id", "type_id", "callable_id", "call_site_id", "symbol_id", "function_id"]
        seen_evidence = set()
        unique_records = []
        for rec in records:
            record_key = next((rec.get(field) for field in id_fields if rec.get(field)), None)
            if record_key is None:
                record_key = json.dumps(rec, sort_keys=True, default=str)
            if str(record_key) in seen_evidence:
                continue
            seen_evidence.add(str(record_key))
            unique_records.append(rec)
        unique_records.sort(key=lambda r: (str(r.get("relative_path", "")), int(r.get("line", r.get("call_line", r.get("return_line", 0))) or 0), str(r.get("event_id", r.get("spawn_id", r.get("variable_id", ""))))))
        write_csv(output_directory / filename, columns, unique_records)

    for filename, evidence_key in JSONL_EXTRA_ARTIFACTS.items():
        records = evidence_records.get(evidence_key, [])
        identity_key = "callable_id" if evidence_key == "callable_targets" else "symbol_id"
        unique = {str(rec.get(identity_key)): rec for rec in records if rec.get(identity_key)}
        write_jsonl(output_directory / filename, sorted(unique.values(), key=lambda r: (r.get("relative_path", r.get("declaration_file", "")), int(r.get("line", 0) or 0), r.get(identity_key, ""))))

    resolution_counts: dict[str, int] = {}
    for record in calls:
        key = record.get("resolution_status") or "UNKNOWN"
        resolution_counts[key] = resolution_counts.get(key, 0) + 1
    access_counts: dict[str, int] = {}
    for record in accesses:
        key = record.get("access_type") or "UNKNOWN"
        access_counts[key] = access_counts.get(key, 0) + 1

    successful = sum(1 for r in unit_reports if r["status"] == "SUCCESS")
    failed = len(unit_reports) - successful
    report = {
        "repository_id": repository_id,
        "extraction_timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "translation_units_recorded": len(unit_reports),
        "translation_units_successful": successful,
        "translation_units_failed": failed,
        "unique_functions": len(functions),
        "unique_calls": len(calls),
        "unique_data_fields": len(fields),
        "unique_data_accesses": len(accesses),
        "missing_caller_ids": len(missing_callers),
        "missing_resolved_callee_ids": len(missing_callees),
        "missing_access_function_ids": len(missing_access_functions),
        "missing_access_field_ids": len(missing_access_fields),
        "call_resolution_status_counts": resolution_counts,
        "access_type_counts": access_counts,
        "rca_evidence_counts": {key: len(records) for key, records in evidence_records.items()},
        "duration_seconds": round(time.perf_counter() - started_at, 3),
    }
    write_json(output_directory / "extraction_report.json", report)
    write_json(output_directory / "manifest.json", {
        "schema_version": "1.1",
        "repository_id": repository_id,
        "paths_are_repository_relative": True,
        "resume_supported": True,
        "worker_isolation": True,
        "artifacts": ["functions.jsonl", "calls.csv", "data_fields.csv", "data_access.csv", "translation_units.csv", "extraction_report.json", *EXTRA_CSV_FIELDS.keys(), *JSONL_EXTRA_ARTIFACTS.keys()],
        "legacy_artifacts_compatible": True,
        "rca_schema_version": "2.0",
        "static_evidence_only": True,
    })
    return report



def preflight(repository_root: Path, build_directory: Path, libclang: str, config_path: Path, limit: int, dry_run: bool) -> list[Path]:
    """Validate all inexpensive inputs before creating checkpoints or workers."""
    if limit < 0:
        raise ValueError("--limit must be 0 (all translation units) or a positive integer")
    if not repository_root.is_dir():
        raise FileNotFoundError(f"Repository root is not a directory: {repository_root}")
    compile_db = build_directory / "compile_commands.json"
    if not compile_db.is_file():
        raise FileNotFoundError(f"Compilation database not found: {compile_db}")
    if not config_path.is_file():
        raise FileNotFoundError(f"Thread/API config not found: {config_path}")
    try:
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in thread/API config {config_path}: {exc}") from exc
    if not isinstance(config, dict):
        raise ValueError(f"Thread/API config must be a JSON object: {config_path}")
    for key in ("spawn_apis", "lock_apis", "synchronization_apis", "smart_pointer_type_patterns"):
        if key not in config or not isinstance(config[key], list):
            raise ValueError(f"Thread/API config must contain an array named {key!r}")
    try:
        compile_entries = json.loads(compile_db.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in compilation database {compile_db}: {exc}") from exc
    if not isinstance(compile_entries, list):
        raise ValueError(f"Compilation database must be a JSON array: {compile_db}")
    for index, entry in enumerate(compile_entries):
        if not isinstance(entry, dict) or not entry.get("file"):
            raise ValueError(f"Compilation database entry {index} must be an object with a non-empty 'file' field")
    translation_units = discover_project_translation_units(build_directory, repository_root)
    selected = translation_units[:limit] if limit else translation_units
    if not selected:
        raise RuntimeError(
            f"No project C/C++ translation units were selected from {compile_db}. "
            "Check source paths, source extensions, and --repository-root."
        )
    if not dry_run:
        if not Path(libclang).is_file():
            raise FileNotFoundError(f"LibClang library not found: {libclang}")
        probe = subprocess.run(
            [sys.executable, "-c", "from clang import cindex; print(cindex.__file__)"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if probe.returncode != 0:
            detail = (probe.stderr or probe.stdout).strip()
            raise RuntimeError(
                "The selected Python interpreter cannot import clang.cindex. "
                f"Interpreter: {sys.executable}. Activate the intended venv and install a "
                f"binding compatible with LibClang. Details: {detail}"
            )
    return selected

def main() -> None:
    parser = argparse.ArgumentParser(description="Resumable repository-wide C/C++ graph extraction.")
    parser.add_argument("--repository-root", required=True)
    parser.add_argument("--build-directory", required=True)
    parser.add_argument("--output-directory", required=True)
    parser.add_argument("--libclang", required=True)
    parser.add_argument("--extractor-module", default=str(Path(__file__).with_name("extract_graph_rca_v2.py")))
    parser.add_argument("--worker-script", default=str(Path(__file__).with_name("extract_tu_worker_rca_v2.py")))
    parser.add_argument("--thread-api-config", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Validate compile database, source discovery and config without invoking Clang or writing extraction output")
    args = parser.parse_args()

    started_at = time.perf_counter()
    repository_root = Path(args.repository_root).resolve()
    build_directory = Path(args.build_directory).resolve()
    output_directory = Path(args.output_directory).resolve()
    extractor_module = Path(args.extractor_module).resolve()
    worker_script = Path(args.worker_script).resolve()
    config_path = Path(args.thread_api_config).resolve() if args.thread_api_config else Path(__file__).with_name("thread_api_config.json").resolve()
    try:
        selected_translation_units = preflight(repository_root, build_directory, args.libclang, config_path, args.limit, args.dry_run)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    all_translation_units = discover_project_translation_units(build_directory, repository_root)
    available_translation_unit_count = len(all_translation_units)
    translation_units = selected_translation_units
    selected_translation_unit_count = len(translation_units)
    print(f"Preflight OK: {available_translation_unit_count} source translation units available; {selected_translation_unit_count} selected", flush=True)
    print(f"Compile database: {build_directory / 'compile_commands.json'}", flush=True)
    print(f"API config: {config_path}", flush=True)
    if args.dry_run:
        for source in translation_units:
            print(f"  {source.relative_to(repository_root).as_posix()}")
        print("Dry run completed: no AST was parsed and no extraction output was written.")
        return
    if not extractor_module.is_file():
        parser.error(f"Extractor module not found: {extractor_module}")
    if not worker_script.is_file():
        parser.error(f"Worker script not found: {worker_script}")
    checkpoint_directory = output_directory / "checkpoints"
    log_directory = output_directory / "worker-logs"
    checkpoint_directory.mkdir(parents=True, exist_ok=True)
    log_directory.mkdir(parents=True, exist_ok=True)

    print(f"Project translation units available: {available_translation_unit_count}; selected: {selected_translation_unit_count}", flush=True)

    checkpoint_files: list[Path] = []
    for position, source_file in enumerate(translation_units, start=1):
        relative_path = source_file.relative_to(repository_root).as_posix()
        checkpoint_file = checkpoint_directory / checkpoint_name(relative_path)
        log_file = log_directory / (checkpoint_file.stem + ".log")
        checkpoint_files.append(checkpoint_file)

        existing = None
        if checkpoint_file.exists():
            try:
                existing = json.loads(checkpoint_file.read_text(encoding="utf-8"))
            except Exception:
                existing = None

        if args.resume and existing and existing.get("extractor_version") == "rca-v2.0":
            if existing.get("status") == "SUCCESS":
                print(f"[{position}/{len(translation_units)}] SKIP completed {relative_path}", flush=True)
                continue
            if existing.get("status") != "SUCCESS" and not args.retry_failed:
                print(f"[{position}/{len(translation_units)}] SKIP failed {relative_path} (use --retry-failed)", flush=True)
                continue

        print(f"\n[{position}/{len(translation_units)}] Worker parsing {relative_path}", flush=True)
        command = [
            sys.executable, str(worker_script),
            "--repository-root", str(repository_root),
            "--build-directory", str(build_directory),
            "--source-file", str(source_file),
            "--checkpoint-file", str(checkpoint_file),
            "--libclang", args.libclang,
            "--extractor-module", str(extractor_module),
        ]
        command.extend(["--thread-api-config", str(config_path)])
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
        log_file.write_text((completed.stdout or "") + "\n--- STDERR ---\n" + (completed.stderr or ""), encoding="utf-8")

        if completed.returncode != 0 or not checkpoint_file.exists():
            failure = {
                "extractor_version": "rca-v2.0",
                "relative_path": relative_path,
                "source_file": str(source_file),
                "status": "CRASHED",
                "worker_exit_code": completed.returncode,
                "duration_seconds": 0,
                "functions": [], "calls": [], "data_fields": [], "data_accesses": [],
                **{key: [] for key in ["variables", "function_parameters", "variable_accesses", "call_arguments", "data_flows", "lifetime_events", "thread_spawns", "lock_usage", "pointer_events", "synchronization_events", "control_flow_events", "macro_events", "types", "type_relationships", "callable_targets", "callable_calls", "return_flows", "call_sites", "external_symbols", "function_return_types"]},
                "diagnostics": [], "diagnostic_summary": {},
                "error": f"Worker exited with code {completed.returncode}. See {log_file.name}",
            }
            write_json(checkpoint_file, failure)
            print(f"Status: CRASHED | Exit code: {completed.returncode} | Log: {log_file}", flush=True)
        else:
            checkpoint = json.loads(checkpoint_file.read_text(encoding="utf-8"))
            print(
                f"Status: {checkpoint['status']} | Functions: {len(checkpoint.get('functions', []))} | "
                f"Calls: {len(checkpoint.get('calls', []))} | Fields: {len(checkpoint.get('data_fields', []))} | "
                f"Accesses: {len(checkpoint.get('data_accesses', []))} | Lifetime: {len(checkpoint.get('lifetime_events', []))} | "
                f"Spawns: {len(checkpoint.get('thread_spawns', []))} | Locks: {len(checkpoint.get('lock_usage', []))} | Duration: {checkpoint.get('duration_seconds')}s",
                flush=True,
            )

        # Rebuild merged artifacts after every unit. This makes partial progress usable.
        merge_successful_checkpoints(checkpoint_files, output_directory, os.environ.get("GRAPH_REPOSITORY_ID", os.environ.get("GRAPH_REPOSITORY_ID", repository_root.name)), started_at)

    report = merge_successful_checkpoints(checkpoint_files, output_directory, os.environ.get("GRAPH_REPOSITORY_ID", os.environ.get("GRAPH_REPOSITORY_ID", repository_root.name)), started_at)
    report["available_translation_units"] = available_translation_unit_count
    report["selected_translation_units"] = selected_translation_unit_count
    report["limit_argument"] = args.limit
    report["is_partial_run"] = bool(args.limit > 0 or report.get("translation_units_failed", 0) > 0)
    write_json(output_directory / "extraction_report.json", report)
    manifest_path = output_directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update({
        "extractor_version": "rca-v2.0",
        "available_translation_units": available_translation_unit_count,
        "selected_translation_units": selected_translation_unit_count,
        "limit_argument": args.limit,
        "is_partial_run": report["is_partial_run"],
        "api_config": str(config_path),
    })
    write_json(manifest_path, manifest)
    print("\n========== RESUMABLE EXTRACTION SUMMARY ==========", flush=True)
    for key, value in report.items():
        print(f"{key}: {value}", flush=True)
    print(f"Output directory: {output_directory}", flush=True)
    print("=================================================", flush=True)


if __name__ == "__main__":
    main()
