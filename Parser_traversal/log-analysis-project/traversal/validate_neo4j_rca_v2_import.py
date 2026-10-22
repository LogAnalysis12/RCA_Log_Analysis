#!/usr/bin/env python3
"""Read-only audit of an additive RCA v2 Neo4j import, with an S7-focused check.

The script performs no CREATE, MERGE, SET, DELETE, or other write operation.
It checks global label/relationship counts, per-scenario graph coverage, S7's
8 thread-entry edges, the T6 -> forward -> releaseCurrent call path, and the
seven observer paths. It can optionally include skipped-edge diagnostics from
an additive importer JSON report.

Environment (read from exported variables or a nearby .env):
  NEO4J_URI       default bolt://localhost:7687
  NEO4J_USERNAME  or NEO4J_USER (default neo4j)
  NEO4J_PASSWORD  required
  NEO4J_DATABASE  default neo4j
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

try:
    from neo4j import GraphDatabase
except ImportError:
    print("ERROR: Neo4j Python driver is missing. Activate the project's venv and run: pip install neo4j", file=sys.stderr)
    raise SystemExit(2)

SCENARIOS = [f"scenario_s{i}.cpp" for i in range(7, 14)]
IMPORTANT_LABELS = [
    "Function", "Variable", "DataField", "Type", "CallSite", "ThreadSpawn",
    "LifetimeEvent", "PointerEvent", "SynchronizationEvent", "LockUsageEvent",
    "MacroEvent", "ControlFlowEvent", "DataFlow", "Argument", "ReturnFlow",
]
IMPORTANT_RELS = [
    "CALLS", "CALLS_AT", "HAS_CALL_SITE", "SPAWNS_THREAD", "DISPATCHES_ASYNC",
    "HAS_LIFETIME_EVENT", "AFFECTS", "HAS_POINTER_EVENT", "POINTER_SOURCE",
    "POINTER_TARGET", "HAS_LOCK_EVENT", "LOCK_OPERATION", "HAS_SYNC_EVENT",
    "SYNCHRONIZATION_OPERATION", "HAS_CONTROL_FLOW_EVENT", "POSSIBLY_EXPANDED_FROM",
    "HAS_MACRO_EVENT", "DATA_FLOW_SOURCE", "DATA_FLOW_TARGET", "PASSES_ARGUMENT",
    "ARGUMENT_TO_PARAMETER", "DECLARES_VARIABLE", "REFERENCES_VARIABLE",
]
PATH_KEYS = ["relative_path", "file", "source_file", "path"]


def load_nearby_env() -> list[str]:
    """Load .env files found alongside this script or in its ancestor folders."""
    loaded: list[str] = []
    script_dir = Path(__file__).resolve().parent
    candidates = [script_dir, *list(script_dir.parents)]
    seen: set[Path] = set()
    for folder in candidates:
        env_file = folder / ".env"
        if env_file in seen or not env_file.is_file():
            continue
        seen.add(env_file)
        try:
            from dotenv import load_dotenv  # type: ignore
            load_dotenv(env_file, override=False)
        except ImportError:
            for raw in env_file.read_text(encoding="utf-8-sig").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:].strip()
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key, value = key.strip(), value.strip()
                if not key or key in os.environ:
                    continue
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                elif " #" in value:
                    value = value.split(" #", 1)[0].rstrip()
                os.environ[key] = value
        loaded.append(str(env_file))
    return loaded


def run_query(session, query: str, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    return [record.data() for record in session.run(query, parameters or {})]


def one_count(session, query: str, parameters: dict[str, Any] | None = None) -> int:
    rows = run_query(session, query, parameters)
    return int(rows[0]["count"]) if rows else 0


def node_name_expr(alias: str) -> str:
    return f"coalesce({alias}.qualified_name, {alias}.function_name, {alias}.name, {alias}.symbol_name, {alias}.target_name, {alias}.variable_name, {alias}.field_name, {alias}.macro_name, {alias}.event_kind, {alias}.kind, {alias}.node_id, '')"


def node_name_values_expr(alias: str) -> str:
    """All likely names, so a short qualified/name property cannot hide a match."""
    return f"[{alias}.qualified_name, {alias}.function_name, {alias}.name, {alias}.symbol_name, {alias}.target_name, {alias}.variable_name, {alias}.field_name, {alias}.macro_name, {alias}.event_kind, {alias}.kind, {alias}.node_id]"


def file_suffix_predicate(alias: str, parameter: str = "suffix") -> str:
    return "any(k IN $path_keys WHERE " + " OR ".join(
        f"(coalesce(toString({alias}[k]), '') ENDS WITH ${parameter})" for _ in [0]
    ) + ")"


def file_suffix_predicate_any(alias: str, parameter: str = "suffix") -> str:
    # Use dynamic property access to accommodate small schema variations.
    return f"any(k IN $path_keys WHERE coalesce(toString({alias}[k]), '') ENDS WITH ${parameter})"


def scenario_node_counts(session, suffix: str) -> list[dict[str, Any]]:
    query = """
    MATCH (n)
    WHERE any(k IN $path_keys WHERE coalesce(toString(n[k]), '') ENDS WITH $suffix)
    UNWIND labels(n) AS label
    RETURN label, count(*) AS count
    ORDER BY label
    """
    return run_query(session, query, {"suffix": suffix, "path_keys": PATH_KEYS})


def scenario_relationship_counts(session, suffix: str) -> list[dict[str, Any]]:
    query = """
    MATCH (a)-[r]->(b)
    WHERE any(k IN $path_keys WHERE coalesce(toString(r[k]), '') ENDS WITH $suffix)
       OR any(k IN $path_keys WHERE coalesce(toString(a[k]), '') ENDS WITH $suffix)
       OR any(k IN $path_keys WHERE coalesce(toString(b[k]), '') ENDS WITH $suffix)
    RETURN type(r) AS relationship, count(*) AS count
    ORDER BY relationship
    """
    return run_query(session, query, {"suffix": suffix, "path_keys": PATH_KEYS})


def find_s7_workers(session) -> dict[str, Any]:
    query = f"""
    MATCH (s:Function)-[:SPAWNS_THREAD]->(spawn:ThreadSpawn)-[:SPAWNS_THREAD]->(target:Function)
    WHERE any(v IN {node_name_values_expr('s')} WHERE v IS NOT NULL AND v CONTAINS 'runS7')
      AND any(k IN $path_keys WHERE coalesce(toString(s[k]), '') ENDS WITH 'scenario_s7.cpp')
    WITH DISTINCT target
    UNWIND $workers AS worker_short_name
    WITH target, worker_short_name
    WHERE any(v IN {node_name_values_expr('target')} WHERE v IS NOT NULL AND v CONTAINS worker_short_name)
    RETURN worker_short_name AS function, target.relative_path AS relative_path
    ORDER BY function
    """
    rows = run_query(session, query, {
        "path_keys": PATH_KEYS,
        "workers": [f"runT{i}" for i in range(1, 9)],
    })
    found = {f"CS7Workers::runT{i}": False for i in range(1, 9)}
    actual: list[str] = []
    for row in rows:
        name = str(row.get("function") or "")
        canonical = f"CS7Workers::{name}" if name.startswith("runT") else name
        actual.append(canonical)
        for worker in found:
            if worker.endswith(name) or name in worker:
                found[worker] = True
    return {"found_workers": actual, "workers": found, "found_count": sum(found.values()), "expected_count": 8}


def find_s7_t6_path(session) -> list[dict[str, Any]]:
    query = f"""
    MATCH (start:Function), (root:Function)
    WHERE any(v IN {node_name_values_expr('start')} WHERE v IS NOT NULL AND v CONTAINS 'runT6')
      AND any(k IN $path_keys WHERE coalesce(toString(start[k]), '') ENDS WITH 'scenario_s7.cpp')
      AND any(v IN {node_name_values_expr('root')} WHERE v IS NOT NULL AND v CONTAINS 'releaseCurrent')
      AND any(k IN $path_keys WHERE coalesce(toString(root[k]), '') ENDS WITH 'scenario_s7.cpp')
    MATCH p = shortestPath((start)-[:CALLS*1..6]->(root))
    RETURN [n IN nodes(p) | coalesce(n.qualified_name, n.function_name, n.name, n.node_id, '')] AS path,
           length(p) AS edge_count
    LIMIT 1
    """
    return run_query(session, query, {"path_keys": PATH_KEYS})


def find_s7_observers(session) -> list[str]:
    query = f"""
    MATCH p = (worker:Function)-[:CALLS*1..3]->(observer:Function)
    WHERE any(v IN {node_name_values_expr('worker')} WHERE v IS NOT NULL AND v CONTAINS 'runT')
      AND NOT any(v IN {node_name_values_expr('worker')} WHERE v IS NOT NULL AND v CONTAINS 'runT6')
      AND any(k IN $path_keys WHERE coalesce(toString(worker[k]), '') ENDS WITH 'scenario_s7.cpp')
      AND any(v IN {node_name_values_expr('observer')} WHERE v IS NOT NULL AND v CONTAINS 'observe')
      AND any(k IN $path_keys WHERE coalesce(toString(observer[k]), '') ENDS WITH 'scenario_s7.cpp')
    RETURN DISTINCT coalesce(worker.qualified_name, worker.function_name, worker.name, worker.node_id, '') AS worker
    ORDER BY worker
    """
    return [str(row["worker"]) for row in run_query(session, query, {"path_keys": PATH_KEYS})]


def check_s7_reset_event(session) -> dict[str, Any]:
    query = f"""
    MATCH (root:Function)-[:HAS_LIFETIME_EVENT]->(e:LifetimeEvent)
    WHERE any(v IN {node_name_values_expr('root')} WHERE v IS NOT NULL AND v CONTAINS 'releaseCurrent')
      AND any(k IN $path_keys WHERE coalesce(toString(root[k]), '') ENDS WITH 'scenario_s7.cpp')
      AND any(k IN $path_keys WHERE coalesce(toString(e[k]), '') ENDS WITH 'scenario_s7.cpp')
    RETURN e.line AS line, e.operation AS operation, e.operation_kind AS operation_kind,
           e.expression AS expression, e.event_kind AS event_kind,
           e.source_snippet AS source_snippet
    ORDER BY e.line
    """
    rows = run_query(session, query, {"path_keys": PATH_KEYS})
    line_27 = [r for r in rows if str(r.get("line", "")) == "27"]
    return {"events_found": len(rows), "line_27_events": line_27, "sample_events": rows[:8]}


def optional_import_report(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    report_path = Path(path).expanduser().resolve()
    if not report_path.is_file():
        return {"path": str(report_path), "error": "file not found"}
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception as exc:  # report diagnostics only
        return {"path": str(report_path), "error": f"could not parse JSON: {exc}"}
    summary = data.get("summary", data) if isinstance(data, dict) else {}
    skipped = summary.get("skipped", data.get("skipped", {})) if isinstance(summary, dict) else {}
    return {"path": str(report_path), "skipped_edges": skipped}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", help="Optional additive-import JSON report to include skipped-edge counts")
    parser.add_argument("--output", default="neo4j_relationship_validation.json", help="JSON output path (default: ./neo4j_relationship_validation.json)")
    args = parser.parse_args()

    env_files = load_nearby_env()
    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    username = os.getenv("NEO4J_USERNAME") or os.getenv("NEO4J_USER") or "neo4j"
    password = os.getenv("NEO4J_PASSWORD")
    database = os.getenv("NEO4J_DATABASE", "neo4j")
    if not password:
        print("ERROR: NEO4J_PASSWORD is not set. Export it or place it in a nearby .env. The script never prints the password.", file=sys.stderr)
        return 2

    result: dict[str, Any] = {
        "mode": "read_only",
        "database": database,
        "uri": uri,
        "env_files_found": env_files,
        "checks": [],
        "global_node_counts": {},
        "global_relationship_counts": {},
        "scenarios": {},
    }
    critical_failures = 0
    driver = None
    try:
        driver = GraphDatabase.driver(uri, auth=(username, password))
        driver.verify_connectivity()
        with driver.session(database=database) as session:
            result["server"] = run_query(session, "CALL dbms.components() YIELD name, versions RETURN name, versions")[0]

            labels = run_query(session, "MATCH (n) UNWIND labels(n) AS label RETURN label, count(*) AS count ORDER BY label")
            rels = run_query(session, "MATCH ()-[r]->() RETURN type(r) AS relationship, count(*) AS count ORDER BY relationship")
            label_map = {str(row["label"]): int(row["count"]) for row in labels}
            rel_map = {str(row["relationship"]): int(row["count"]) for row in rels}
            result["global_node_counts"] = {label: label_map.get(label, 0) for label in IMPORTANT_LABELS}
            result["global_relationship_counts"] = {rel: rel_map.get(rel, 0) for rel in IMPORTANT_RELS}
            result["all_global_node_counts"] = label_map
            result["all_global_relationship_counts"] = rel_map

            for suffix in SCENARIOS:
                node_rows = scenario_node_counts(session, suffix)
                rel_rows = scenario_relationship_counts(session, suffix)
                result["scenarios"][suffix] = {
                    "node_counts": {str(row["label"]): int(row["count"]) for row in node_rows},
                    "relationship_counts": {str(row["relationship"]): int(row["count"]) for row in rel_rows},
                    "node_count_total": sum(int(row["count"]) for row in node_rows),
                    "relationship_count_total": sum(int(row["count"]) for row in rel_rows),
                }

            s7 = {
                "worker_spawn_links": find_s7_workers(session),
                "t6_to_release_call_path": find_s7_t6_path(session),
                "observer_worker_paths": find_s7_observers(session),
                "release_current_lifetime_events": check_s7_reset_event(session),
            }
            s7["checks"] = [
                {"name": "all_8_worker_spawn_targets_linked", "status": "PASS" if s7["worker_spawn_links"]["found_count"] == 8 else "FAIL"},
                {"name": "T6_calls_reach_releaseCurrent", "status": "PASS" if bool(s7["t6_to_release_call_path"]) else "FAIL"},
                {"name": "7_non_culprit_observer_paths_to_observe", "status": "PASS" if len(s7["observer_worker_paths"]) == 7 else "REVIEW"},
                {"name": "reset_lifetime_event_at_line_27", "status": "PASS" if s7["release_current_lifetime_events"]["line_27_events"] else "REVIEW"},
            ]
            result["s7_audit"] = s7
            for check in s7["checks"]:
                if check["status"] == "FAIL":
                    critical_failures += 1

        result["import_report_diagnostics"] = optional_import_report(args.report)
        result["summary"] = {
            "critical_s7_failures": critical_failures,
            "interpretation": (
                "S7 requires 8 linked thread entry points and a call path from T6 to releaseCurrent. "
                "The culprit path is T6 -> forward -> releaseCurrent/reset at line 27; the other seven "
                "workers are observers that report the missing resource after release. This is a synchronized "
                "lifetime/ordering defect, not evidence of an unsynchronized pointer data race."
            ),
        }

    except Exception as exc:
        print(f"ERROR: Neo4j validation failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    finally:
        if driver is not None:
            driver.close()

    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")

    print("\n=== NEO4J RCA GRAPH VALIDATION (READ-ONLY) ===")
    print(f"Database: {database} | URI: {uri}")
    print("\nGlobal node counts:")
    for label, count in result["global_node_counts"].items():
        print(f"  {label:28} {count:>6}")
    print("\nGlobal relationship counts:")
    for rel, count in result["global_relationship_counts"].items():
        print(f"  {rel:28} {count:>6}")
    print("\nPer-scenario graph coverage:")
    for suffix, data in result["scenarios"].items():
        print(f"  {suffix:20} nodes={data['node_count_total']:>5} relationships={data['relationship_count_total']:>5}")
    s7 = result["s7_audit"]
    print("\nS7 thread/call-chain audit:")
    print(f"  Worker spawn targets found: {s7['worker_spawn_links']['found_count']}/8")
    for name, found in s7["worker_spawn_links"]["workers"].items():
        print(f"    {'PASS' if found else 'FAIL'} {name}")
    if s7["t6_to_release_call_path"]:
        path = s7["t6_to_release_call_path"][0].get("path", [])
        print("  T6 call path: " + " -> ".join(path))
    else:
        print("  FAIL T6 -> releaseCurrent call path not found")
    print(f"  Observer entry points reaching observe(): {len(s7['observer_worker_paths'])}/7")
    for check in s7["checks"]:
        print(f"  {check['status']:6} {check['name']}")
    diagnostics = result.get("import_report_diagnostics")
    if diagnostics:
        print("\nImport report skipped-edge diagnostics:")
        print(json.dumps(diagnostics, indent=2, ensure_ascii=False))
    print(f"\nJSON report saved: {output_path}")
    print("No graph nodes or relationships were changed.")
    return 2 if critical_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
