from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


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
    write_csv(output_directory / "translation_units.csv", [
        "relative_path", "status", "duration_seconds", "functions", "calls", "data_fields",
        "data_accesses", "project_errors", "external_errors", "worker_exit_code", "error",
    ], sorted(unit_reports, key=lambda r: r.get("relative_path") or ""))

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
        "duration_seconds": round(time.perf_counter() - started_at, 3),
    }
    write_json(output_directory / "extraction_report.json", report)
    write_json(output_directory / "manifest.json", {
        "schema_version": "1.1",
        "repository_id": repository_id,
        "paths_are_repository_relative": True,
        "resume_supported": True,
        "worker_isolation": True,
        "artifacts": ["functions.jsonl", "calls.csv", "data_fields.csv", "data_access.csv", "translation_units.csv", "extraction_report.json"],
    })
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Resumable repository-wide C/C++ graph extraction.")
    parser.add_argument("--repository-root", required=True)
    parser.add_argument("--build-directory", required=True)
    parser.add_argument("--output-directory", required=True)
    parser.add_argument("--libclang", required=True)
    parser.add_argument("--extractor-module", default=r".\graph-extractor\extract_graph_step3_fields_working.py")
    parser.add_argument("--worker-script", default=r".\graph-extractor\extract_tu_worker.py")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    args = parser.parse_args()

    started_at = time.perf_counter()
    repository_root = Path(args.repository_root).resolve()
    build_directory = Path(args.build_directory).resolve()
    output_directory = Path(args.output_directory).resolve()
    extractor_module = Path(args.extractor_module).resolve()
    worker_script = Path(args.worker_script).resolve()
    checkpoint_directory = output_directory / "checkpoints"
    log_directory = output_directory / "worker-logs"
    checkpoint_directory.mkdir(parents=True, exist_ok=True)
    log_directory.mkdir(parents=True, exist_ok=True)

    translation_units = discover_project_translation_units(build_directory, repository_root)
    if args.limit > 0:
        translation_units = translation_units[:args.limit]

    print(f"Project translation units selected: {len(translation_units)}", flush=True)

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

        if args.resume and existing:
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
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
        log_file.write_text((completed.stdout or "") + "\n--- STDERR ---\n" + (completed.stderr or ""), encoding="utf-8")

        if completed.returncode != 0 or not checkpoint_file.exists():
            failure = {
                "relative_path": relative_path,
                "source_file": str(source_file),
                "status": "CRASHED",
                "worker_exit_code": completed.returncode,
                "duration_seconds": 0,
                "functions": [], "calls": [], "data_fields": [], "data_accesses": [],
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
                f"Accesses: {len(checkpoint.get('data_accesses', []))} | Duration: {checkpoint.get('duration_seconds')}s",
                flush=True,
            )

        # Rebuild merged artifacts after every unit. This makes partial progress usable.
        merge_successful_checkpoints(checkpoint_files, output_directory, repository_root.name, started_at)

    report = merge_successful_checkpoints(checkpoint_files, output_directory, repository_root.name, started_at)
    print("\n========== RESUMABLE EXTRACTION SUMMARY ==========", flush=True)
    for key, value in report.items():
        print(f"{key}: {value}", flush=True)
    print(f"Output directory: {output_directory}", flush=True)
    print("=================================================", flush=True)


if __name__ == "__main__":
    main()
