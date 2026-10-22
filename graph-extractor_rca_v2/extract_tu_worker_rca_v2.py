from __future__ import annotations

import argparse
import importlib.util
import json
import time
import traceback
from pathlib import Path

from clang import cindex


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location("validated_graph_extractor", str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load extractor module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_checkpoint(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Isolated worker for one C/C++ translation unit.")
    parser.add_argument("--repository-root", required=True)
    parser.add_argument("--build-directory", required=True)
    parser.add_argument("--source-file", required=True)
    parser.add_argument("--checkpoint-file", required=True)
    parser.add_argument("--libclang", required=True)
    parser.add_argument("--extractor-module", required=True)
    parser.add_argument("--thread-api-config", default="")
    args = parser.parse_args()

    started_at = time.perf_counter()
    repository_root = Path(args.repository_root).resolve()
    build_directory = Path(args.build_directory).resolve()
    source_file = Path(args.source_file).resolve()
    checkpoint_file = Path(args.checkpoint_file).resolve()
    extractor_module_path = Path(args.extractor_module).resolve()
    checkpoint_file.parent.mkdir(parents=True, exist_ok=True)

    result = {
        "extractor_version": "rca-v2.0",
        "relative_path": source_file.relative_to(repository_root).as_posix(),
        "source_file": str(source_file),
        "status": "FAILED",
        "worker_exit_code": 0,
        "duration_seconds": 0,
        "functions": [], "calls": [], "data_fields": [], "data_accesses": [],
        "variables": [], "function_parameters": [], "variable_accesses": [], "call_arguments": [],
        "data_flows": [], "lifetime_events": [], "thread_spawns": [], "lock_usage": [],
        "pointer_events": [], "synchronization_events": [], "control_flow_events": [],
        "macro_events": [], "types": [], "type_relationships": [], "callable_targets": [],
        "callable_calls": [], "return_flows": [], "call_sites": [], "external_symbols": [], "function_return_types": [],
        "diagnostics": [], "diagnostic_summary": {},
        "error": None, "traceback": None,
    }

    try:
        cindex.Config.set_library_file(args.libclang)
        extractor = load_module(extractor_module_path)
        compilation_database = cindex.CompilationDatabase.fromDirectory(str(build_directory))
        commands = compilation_database.getCompileCommands(str(source_file))
        command = next(iter(commands), None) if commands is not None else None
        if command is None:
            raise RuntimeError("No compilation command was found for this translation unit.")

        clang_arguments = extractor.sanitize_compile_arguments(list(command.arguments), source_file)
        index = cindex.Index.create()
        parse_options = cindex.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD
        translation_unit = index.parse(path=str(source_file), args=clang_arguments, options=parse_options)

        diagnostics = [{
            "severity": diagnostic.severity,
            "spelling": diagnostic.spelling,
            "location": str(diagnostic.location) if diagnostic.location else None,
        } for diagnostic in translation_unit.diagnostics]
        diagnostic_summary = extractor.classify_diagnostics(diagnostics, repository_root)
        repository_id = repository_root.name
        functions = extractor.extract_functions(translation_unit.cursor, repository_root, repository_id)
        calls = extractor.extract_calls(translation_unit.cursor, repository_root, repository_id)
        fields, accesses = extractor.extract_field_accesses(translation_unit.cursor, repository_root, repository_id)
        config_path = Path(args.thread_api_config).resolve() if args.thread_api_config else extractor_module_path.with_name("thread_api_config.json")
        evidence = extractor.extract_rca_evidence(translation_unit.cursor, repository_root, repository_id, config_path)

        result.update({
            "status": "SUCCESS",
            "functions": functions,
            "calls": calls,
            "data_fields": fields,
            "data_accesses": accesses,
            "diagnostics": diagnostics,
            "diagnostic_summary": diagnostic_summary,
            **evidence,
        })
    except Exception as error:
        result["error"] = str(error)
        result["traceback"] = traceback.format_exc()

    result["duration_seconds"] = round(time.perf_counter() - started_at, 3)
    write_checkpoint(checkpoint_file, result)
    print(json.dumps({
        "relative_path": result["relative_path"],
        "status": result["status"],
        "duration_seconds": result["duration_seconds"],
        "functions": len(result.get("functions", [])),
        "lifetime_events": len(result.get("lifetime_events", [])),
        "thread_spawns": len(result.get("thread_spawns", [])),
        "lock_usage": len(result.get("lock_usage", [])),
    }))


if __name__ == "__main__":
    main()
