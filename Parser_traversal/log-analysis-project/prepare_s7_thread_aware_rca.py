#!/usr/bin/env python3
"""Prepare an isolated S7-only RCA input using the live Neo4j graph.

This script does not modify Neo4j and does not run the LLM. It verifies the
S7 anchor's source hash against the checkpoint source tree, writes a one-issue
JSON file plus an isolated traversal config, and prints the exact run command.
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path.home() / "Desktop/LogAnalysis_thread/Parser_traversal/log-analysis-project"
CHECKPOINT = Path.home() / "Desktop/LogAnalysis_thread/ThreadDemo_S7_S13_checkpoint/ThreadDemo"
BASE_CONFIG = PROJECT / "config.threaddemo.json"
LOG_PATH = CHECKPOINT / "logs/sample_run_s7_s13.log"
ANCHOR_NAME = "CS7Workers::runT2"
SCENARIO_FILE = "scenario_s7.cpp"
ERROR_MESSAGE = "Shared resource unavailable after phase transition"
FINGERPRINT = "fp_s7_premature_resource_release_thread_aware"


def fail(message: str, code: int = 1) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(code)


def extract_logged_function(raw_line: str) -> str | None:
    """Extract the worker function actually named in an S7 error log line."""
    match = re.search(
        r"\b([A-Za-z_]\w*(?:::[A-Za-z_]\w*)+)\s+" + re.escape(ERROR_MESSAGE),
        raw_line,
    )
    return match.group(1) if match else None


def select_representative_line(
    matching_lines: list[tuple[int, str]], preferred_function: str
) -> tuple[int, str]:
    """Choose a raw event for the requested anchor, never the first message-only match."""
    for line_number, raw_line in matching_lines:
        if extract_logged_function(raw_line) == preferred_function:
            return line_number, raw_line
    observed = sorted({
        function for _, raw_line in matching_lines
        if (function := extract_logged_function(raw_line)) is not None
    })
    raise ValueError(
        f"No error log record names preferred representative function {preferred_function!r}. "
        f"Functions present in matching records: {observed}. Refusing to create inconsistent issue metadata."
    )


def main() -> int:
    if not PROJECT.is_dir():
        fail(f"Project directory not found: {PROJECT}")
    if not CHECKPOINT.is_dir():
        fail(f"S7-S13 checkpoint not found: {CHECKPOINT}")
    if not BASE_CONFIG.is_file():
        fail(f"Base configuration not found: {BASE_CONFIG}")
    if not LOG_PATH.is_file():
        fail(f"S7-S13 log not found: {LOG_PATH}")

    os.chdir(PROJECT)
    try:
        from dotenv import load_dotenv
        for env_file in (
            PROJECT / ".env",
            Path.home() / "Desktop/LogAnalysis_thread/.env",
            Path.home() / "Desktop/LogAnalysis_thread/Parser_traversal/.env",
        ):
            if env_file.is_file():
                load_dotenv(env_file, override=False)
    except ImportError:
        pass

    try:
        from traversal.graph_client import create_graph_client
        from traversal.neptune_tools import NeptuneTraversalTools
    except Exception as exc:
        fail(f"Could not import active traversal code: {type(exc).__name__}: {exc}")

    base_config = json.loads(BASE_CONFIG.read_text(encoding="utf-8"))
    graph_config = base_config.get("graph", {})
    if not isinstance(graph_config, dict):
        fail("The graph section in config.threaddemo.json is not an object.")

    # Ensure the configured password variable is present without printing it.
    password_env = str(graph_config.get("password_env", "NEO4J_PASSWORD"))
    if not os.environ.get(password_env):
        fail(
            f"{password_env} is not set. Load the same .env used by the existing project; "
            "do not print the variable's value."
        )

    client = None
    try:
        client = create_graph_client(graph_config)
        client.get_engine_status()
        rows = client.query(
            """
            MATCH (f:Function)
            WHERE f.qualified_name = $qualified_name
              AND coalesce(f.relative_path, '') ENDS WITH $source_file
            RETURN f.node_id AS node_id,
                   f.qualified_name AS qualified_name,
                   f.function_name AS function_name,
                   f.class_name AS class_name,
                   f.signature AS signature,
                   f.relative_path AS relative_path,
                   f.start_line AS start_line,
                   f.end_line AS end_line,
                   f.content_hash AS content_hash
            ORDER BY f.node_id
            LIMIT 10
            """,
            {"qualified_name": ANCHOR_NAME, "source_file": SCENARIO_FILE},
        )
        if not rows:
            fail(
                f"No graph Function matched {ANCHOR_NAME} in *{SCENARIO_FILE}. "
                "Do not create a fabricated anchor; inspect the graph extraction instead."
            )
        if len(rows) > 1:
            fail(
                f"Found {len(rows)} matching anchor functions. Resolve the ambiguity before running RCA."
            )

        row = rows[0]
        function_id = str(row.get("node_id") or "").strip()
        relative_path = str(row.get("relative_path") or "").strip()
        if not function_id or not relative_path:
            fail("Matched graph function is missing node_id or relative_path metadata.")

        tools = NeptuneTraversalTools(
            graph_client=client,
            source_root=CHECKPOINT,
        )
        source_result = tools.read_function_source(function_id)
        if source_result.get("status") != "SOURCE_VERIFIED":
            fail(
                "The S7 anchor source did not pass hash verification. "
                f"Status={source_result.get('status')}; "
                f"details={source_result.get('error', source_result.get('message', 'not provided'))}. "
                "No issue/config files were written."
            )

        selected_function = {
            "function_id": function_id,
            "qualified_name": str(row.get("qualified_name") or ANCHOR_NAME),
            "function_name": str(row.get("function_name") or "runT2"),
            "class_name": str(row.get("class_name") or "CS7Workers"),
            "signature": str(row.get("signature") or ""),
            "relative_path": relative_path,
            "start_line": row.get("start_line"),
            "end_line": row.get("end_line"),
            "content_hash": str(row.get("content_hash") or ""),
        }
    finally:
        if client is not None:
            client.close()

    matching_lines = []
    with LOG_PATH.open(encoding="utf-8", errors="replace") as log_file:
        for line_number, line in enumerate(log_file, 1):
            if ERROR_MESSAGE in line:
                matching_lines.append((line_number, line.rstrip("\r\n")))
    if not matching_lines:
        fail(f"Expected S7 error message was not found in {LOG_PATH}; no files written.")

    # Every occurrence must retain its actual worker identity. Select the event
    # matching the analysis anchor instead of taking the first message match.
    unparsed_lines = [
        (line_number, raw_line)
        for line_number, raw_line in matching_lines
        if extract_logged_function(raw_line) is None
    ]
    if unparsed_lines:
        fail(
            "Could not extract the logged function from matching error records; "
            f"examples={unparsed_lines[:3]}. No issue/config files written."
        )
    try:
        log_line_number, raw_log_line = select_representative_line(
            matching_lines, ANCHOR_NAME
        )
    except ValueError as exc:
        fail(f"{exc} No issue/config files written.")

    affected_function_counts = Counter(
        extract_logged_function(raw_line) for _, raw_line in matching_lines
    )

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = PROJECT / "output" / f"s7_thread_aware_test_{timestamp}"
    run_dir.mkdir(parents=True, exist_ok=False)
    config_path = PROJECT / f"config.s7_thread_aware_{timestamp}.json"
    issues_path = run_dir / "resolved_issues_s7.json"

    representative_function = extract_logged_function(raw_log_line)
    if representative_function != ANCHOR_NAME:
        fail(
            "Internal consistency check failed: representative raw log line names "
            f"{representative_function!r}, expected {ANCHOR_NAME!r}. No files written."
        )
    representative_error = {
        "level": "ERROR",
        "message": ERROR_MESSAGE,
        "qualified_function": representative_function,
        "relative_path": relative_path,
        "log_file": str(LOG_PATH),
        "log_line_number": log_line_number,
        "raw_line": raw_log_line,
    }
    issue = {
        "fingerprint_id": FINGERPRINT,
        "occurrence_count": len(matching_lines),
        "qualified_function": ANCHOR_NAME,
        "normalized_message": ERROR_MESSAGE,
        "representative_error": representative_error,
        "affected_functions": [
            {"qualified_function": function, "occurrence_count": count}
            for function, count in sorted(affected_function_counts.items())
        ],
        "anchor_resolution": {
            "status": "RESOLVED",
            "match_method": "exact_qualified_name_and_source_file_verified_by_hash",
            "candidate_count": 1,
            "selected_function": selected_function,
            "candidates": [selected_function],
        },
    }

    # Keep all existing LLM, graph, traversal, and budget configuration intact.
    isolated_config = dict(base_config)
    isolated_config["source_root"] = str(CHECKPOINT.resolve())
    isolated_config["output_directory"] = str(run_dir.resolve())

    issues_path.write_text(json.dumps([issue], indent=2) + "\n", encoding="utf-8")
    config_path.write_text(json.dumps(isolated_config, indent=2) + "\n", encoding="utf-8")

    print("S7-only RCA input prepared. No LLM call has been made and Neo4j was not modified.")
    print(f"Anchor source verification: {source_result.get('status')}")
    print(f"Anchor: {selected_function['qualified_name']}")
    print(f"Source: {selected_function['relative_path']}:{selected_function['start_line']}-{selected_function['end_line']}")
    print(f"Verified source hash present: {bool(selected_function['content_hash'])}")
    print(f"Matching S7 error log records: {len(matching_lines)}")
    print(f"Representative log line: {log_line_number}")
    print(f"Config: {config_path}")
    print(f"Issues: {issues_path}")
    print(f"Output directory: {run_dir}")
    print("\nNEXT COMMAND (run only when ready to spend an LLM call):")
    print(
        "python -m traversal.run_traversal "
        f"--config '{config_path}' "
        f"--issues '{issues_path}' "
        f"--output '{run_dir / 'rca_result.json'}' "
        f"--fingerprint-id '{FINGERPRINT}'"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
