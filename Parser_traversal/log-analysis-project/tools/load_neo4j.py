"""Load neutral-graph schema 2.0 CSV/JSONL exports into Neo4j."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ACCESS_TYPES = {"READ": "READS", "WRITE": "WRITES", "READ_WRITE": "READS_WRITES", "POSSIBLE_WRITE": "POSSIBLE_WRITE"}
LABELS = ("Function", "DataField", "ExternalSymbol")
REL_TYPES = ("CALLS", "CALLS_EXTERNAL", "READS", "WRITES", "READS_WRITES", "POSSIBLE_WRITE")


def _jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8-sig") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def _csv(path: Path) -> list[dict[str, str]]:
    csv.field_size_limit(sys.maxsize)
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def _integer(row: dict[str, Any], key: str) -> int:
    try:
        return int(row[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be an integer; got {row.get(key)!r}.") from exc


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes"}



def build_graph_data(root: Path) -> dict[str, Any]:
    """Convert raw extraction artifacts into the Neo4j graph schema."""
    import hashlib

    root = Path(root)
    functions_by_id = {}
    fields_by_id = {}
    symbols_by_key = {}
    mismatches = 0

    # 1. Load extracted function definitions.
    for source in _jsonl(root / "functions.jsonl"):
        function_id = str(source.get("function_id") or "").strip()
        if not function_id:
            raise ValueError("Function record has no function_id.")
        if function_id in functions_by_id:
            raise ValueError(f"Duplicate function_id: {function_id}")

        code = source.get("source_code") or ""
        digest = hashlib.sha256(
            code.replace("\r\n", "\n").encode("utf-8")
        ).hexdigest()

        if code and digest != source.get("content_hash"):
            mismatches += 1

        node = {
            key: source.get(key)
            for key in (
                "function_id", "repository_id", "class_name",
                "function_name", "qualified_name", "signature",
                "relative_path", "content_hash", "cursor_kind",
            )
        }
        node.update(
            node_id="Function:" + function_id,
            start_line=_integer(source, "start_line"),
            end_line=_integer(source, "end_line"),
            source_available=bool(code.strip()),
            definition_available=True,
            extraction_status="EXTRACTED",
        )
        functions_by_id[function_id] = node

    # 2. Load data-field definitions.
    for source in _csv(root / "data_fields.csv"):
        field_id = str(source.get("field_id") or "").strip()
        if not field_id:
            raise ValueError("Data-field record has no field_id.")
        if field_id in fields_by_id:
            raise ValueError(f"Duplicate field_id: {field_id}")

        node = {
            key: source.get(key)
            for key in (
                "field_id", "repository_id", "field_name",
                "qualified_name", "field_type",
                "declaration_relative_path", "declaration_kind",
            )
        }
        node.update(
            node_id="DataField:" + field_id,
            declaration_line=_integer(source, "declaration_line"),
        )
        fields_by_id[field_id] = node

    # 3. Read calls and create external symbols or missing-definition
    # placeholders where the original records justify them.
    calls_by_type = {"CALLS": [], "CALLS_EXTERNAL": []}

    for index, source in enumerate(_csv(root / "calls.csv"), start=1):
        caller_id = str(source.get("caller_function_id") or "").strip()
        if caller_id not in functions_by_id:
            raise ValueError(f"Call {index}: unknown caller {caller_id!r}")

        status = str(source.get("resolution_status") or "").strip()
        callee_id = str(source.get("callee_function_id") or "").strip()
        callee_name = str(source.get("callee_name") or "").strip()
        qualified_name = str(
            source.get("qualified_callee_name") or callee_name
        ).strip()
        signature = str(source.get("callee_signature") or "").strip()

        if status == "RESOLVED_PROJECT":
            if not callee_id:
                raise ValueError(
                    f"Call {index}: RESOLVED_PROJECT has no callee_function_id."
                )

            if callee_id not in functions_by_id:
                caller = functions_by_id[caller_id]
                class_name = (
                    qualified_name.split("::", 1)[0]
                    if "::" in qualified_name else None
                )
                functions_by_id[callee_id] = {
                    "node_id": "Function:" + callee_id,
                    "function_id": callee_id,
                    "repository_id": caller.get("repository_id"),
                    "class_name": class_name,
                    "function_name": callee_name,
                    "qualified_name": qualified_name,
                    "signature": signature,
                    "relative_path": None,
                    "content_hash": None,
                    "cursor_kind": "MISSING_DEFINITION",
                    "start_line": 0,
                    "end_line": 0,
                    "source_code": "",
                    "source_available": False,
                    "definition_available": False,
                    "extraction_status": "MISSING_DEFINITION",
                }

            target_node_id = "Function:" + callee_id
            relationship_type = "CALLS"

        elif status == "EXTERNAL":
            # Use name + signature to keep distinct overloads separate.
            identity = (qualified_name or callee_name, signature)
            if not identity[0]:
                raise ValueError(
                    f"Call {index}: external symbol has no usable name."
                )

            if identity not in symbols_by_key:
                symbol_id = "ext_" + hashlib.sha256(
                    (identity[0] + "\0" + identity[1]).encode("utf-8")
                ).hexdigest()[:24]

                symbols_by_key[identity] = {
                    "node_id": "ExternalSymbol:" + symbol_id,
                    "symbol_id": symbol_id,
                    "repository_id": functions_by_id[caller_id].get(
                        "repository_id"
                    ),
                    "symbol_name": callee_name or identity[0],
                    "qualified_name": identity[0],
                    "signature": signature,
                    "symbol_kind": str(
                        source.get("call_type") or "UNKNOWN"
                    ),
                    "resolution_reason": "RAW_BASELINE_EXTERNAL",
                    "original_resolution_status": status,
                    "implementation_available": False,
                    "source_available": False,
                }

            target_node_id = symbols_by_key[identity]["node_id"]
            relationship_type = "CALLS_EXTERNAL"

        else:
            raise ValueError(
                f"Call {index}: unsupported resolution_status {status!r}"
            )

        relationship_identity = "\0".join((
            caller_id, target_node_id,
            str(source.get("call_line") or ""),
            str(source.get("call_column") or ""),
            str(source.get("call_expression") or ""),
            str(index),
        ))
        relationship_id = "call_" + hashlib.sha256(
            relationship_identity.encode("utf-8")
        ).hexdigest()[:24]

        calls_by_type[relationship_type].append({
            "relationship_id": relationship_id,
            "start": "Function:" + caller_id,
            "end": target_node_id,
            "call_line": _integer(source, "call_line"),
            "call_column": _integer(source, "call_column"),
            "call_type": source.get("call_type", ""),
            "call_expression": source.get("call_expression", ""),
            "resolution_status": status,
            "original_resolution_status": status,
            "reconciliation_reason": (
                "MISSING_PROJECT_DEFINITION"
                if status == "RESOLVED_PROJECT"
                and not any(
                    fn.get("function_id") == callee_id
                    and fn.get("definition_available") is True
                    for fn in _jsonl(root / "functions.jsonl")
                )
                else ""
            ),
        })

    # 4. Convert data-access records into graph relationships.
    accesses = {
        "READS": [],
        "WRITES": [],
        "READS_WRITES": [],
        "POSSIBLE_WRITE": [],
    }

    for index, source in enumerate(_csv(root / "data_access.csv"), start=1):
        function_id = str(source.get("function_id") or "").strip()
        field_id = str(source.get("field_id") or "").strip()

        if function_id not in functions_by_id:
            raise ValueError(
                f"Data access {index}: unknown function {function_id!r}"
            )
        if field_id not in fields_by_id:
            raise ValueError(
                f"Data access {index}: unknown field {field_id!r}"
            )

        access_type = str(source.get("access_type") or "").strip()
        if access_type not in ACCESS_TYPES:
            raise ValueError(
                f"Data access {index}: unknown access_type {access_type!r}"
            )

        rel_type = ACCESS_TYPES[access_type]
        accesses[rel_type].append({
            "start": "Function:" + function_id,
            "end": "DataField:" + field_id,
            "line": _integer(source, "line"),
            "column": _integer(source, "column"),
            "chain": source.get("chain", ""),
            "suffix2": source.get("suffix2", ""),
        })

    # 5. Validate every relationship endpoint before returning data.
    nodes = {
        "Function": list(functions_by_id.values()),
        "DataField": list(fields_by_id.values()),
        "ExternalSymbol": list(symbols_by_key.values()),
    }
    relationships = {**calls_by_type, **accesses}
    node_ids = {
        node["node_id"]
        for label_nodes in nodes.values()
        for node in label_nodes
    }

    if sum(len(rows) for rows in calls_by_type.values()) != sum(
        1 for _ in _csv(root / "calls.csv")
    ):
        raise RuntimeError("Not all input call records were preserved.")

    for rel_type, rows in relationships.items():
        for row in rows:
            if row["start"] not in node_ids:
                raise ValueError(
                    f"{rel_type} has missing start node: {row['start']}"
                )
            if row["end"] not in node_ids:
                raise ValueError(
                    f"{rel_type} has missing end node: {row['end']}"
                )

    report_path = root / "normalization_report.json"
    report = (
        json.loads(report_path.read_text(encoding="utf-8-sig"))
        if report_path.exists() else {}
    )

    return {
        "nodes": nodes,
        "relationships": relationships,
        "counts": {
            "nodes": {key: len(value) for key, value in nodes.items()},
            "relationships": {
                key: len(value) for key, value in relationships.items()
            },
        },
        "hash_mismatch_count": mismatches,
        "normalization_report": report,
    }


def load_graph_data(driver, database: str, data: dict[str, Any], reset: bool = False) -> dict[str, int]:
    with driver.session(database=database) as session:
        if session.run("MATCH (n) RETURN count(n) AS count").single()["count"] and not reset:
            raise RuntimeError("Neo4j database is not empty; pass --reset to replace its contents.")
        if reset:
            session.run("MATCH (n) DETACH DELETE n").consume()
        statements = [
            "CREATE CONSTRAINT fn_node IF NOT EXISTS FOR (n:Function) REQUIRE n.node_id IS UNIQUE",
            "CREATE CONSTRAINT field_node IF NOT EXISTS FOR (n:DataField) REQUIRE n.node_id IS UNIQUE",
            "CREATE CONSTRAINT sym_node IF NOT EXISTS FOR (n:ExternalSymbol) REQUIRE n.node_id IS UNIQUE",
            "CREATE INDEX fn_qname IF NOT EXISTS FOR (n:Function) ON (n.qualified_name)",
            "CREATE INDEX fn_name IF NOT EXISTS FOR (n:Function) ON (n.function_name)",
        ]
        for statement in statements: session.run(statement).consume()
        for label, rows in data["nodes"].items():
            for offset in range(0, len(rows), 1000):
                session.run(f"UNWIND $rows AS row CREATE (n:{label}) SET n = row", rows=rows[offset:offset + 1000]).consume()
        for rel_type, rows in data["relationships"].items():
            for offset in range(0, len(rows), 1000):
                batch = rows[offset:offset + 1000]
                session.run(f"UNWIND $rows AS row MATCH (a {{node_id: row.start}}), (b {{node_id: row.end}}) CREATE (a)-[r:{rel_type}]->(b) SET r = row", rows=batch).consume()
        live = {}
        for label in LABELS:
            live[label] = session.run(f"MATCH (n:{label}) RETURN count(n) AS count").single()["count"]
        for rel in REL_TYPES:
            live[rel] = session.run(f"MATCH ()-[r:{rel}]->() RETURN count(r) AS count").single()["count"]
        expected = {**data["counts"]["nodes"], **data["counts"]["relationships"]}
        if live != expected:
            raise RuntimeError(f"Neo4j counts differ from input: expected={expected}, live={live}")
        report = data["normalization_report"]
        expected_report = {
            "Function": report.get("input_function_count"),
            "ExternalSymbol": report.get("external_symbol_count"),
            "normalized calls": report.get("normalized_call_count"),
        }
        for key, count in expected_report.items():
            actual = live["CALLS"] + live["CALLS_EXTERNAL"] if key == "normalized calls" else live[key]
            if count is not None and actual != count:
                raise RuntimeError(f"{key} count {actual} differs from normalization_report.json ({count}).")
        return live


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("neutral_graph_dir", type=Path)
    parser.add_argument("--uri", default="bolt://localhost:7687")
    parser.add_argument("--user", default="neo4j")
    parser.add_argument("--database", default="neo4j")
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()
    password = os.environ.get("NEO4J_PASSWORD")
    if not password:
        parser.error("Set the NEO4J_PASSWORD environment variable.")
    data = build_graph_data(args.neutral_graph_dir)
    if data["hash_mismatch_count"]:
        print(f"Source hash mismatches: {data['hash_mismatch_count']}", file=sys.stderr)
        return 1
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(args.uri, auth=(args.user, password), connection_timeout=20)
    try:
        counts = load_graph_data(driver, args.database, data, args.reset)
    finally:
        driver.close()
    print("Type | count\n-----|------")
    for key, count in counts.items(): print(f"{key} | {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
