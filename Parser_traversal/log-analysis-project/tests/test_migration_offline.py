from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path

from tools.load_neo4j import ACCESS_TYPES, build_graph_data


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _write_csv(path: Path, headers: list[str], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=headers, lineterminator="\r\n")
        writer.writeheader()
        writer.writerows(rows)


def mini_graph(tmp_path: Path) -> Path:
    root = tmp_path / "mini_graph"
    root.mkdir()
    code = "int f() { return 1; }\n"
    _write_jsonl(root / "functions.jsonl", [
        {"function_id": "fn_a", "repository_id": "r", "class_name": None, "function_name": "f", "qualified_name": "X::f", "signature": "int f()", "relative_path": "src/f.cpp", "start_line": "1", "end_line": "1", "content_hash": hashlib.sha256(code.encode()).hexdigest(), "source_code": code, "cursor_kind": "FUNCTION_DECL"},
    ])
    _write_jsonl(root / "external_symbols.jsonl", [])
    _write_csv(root / "data_fields.csv", ["field_id", "repository_id", "field_name", "qualified_name", "field_type", "declaration_relative_path", "declaration_line", "declaration_kind"], [
        {"field_id": "field_a", "repository_id": "r", "field_name": "value", "qualified_name": "X::value", "field_type": "int", "declaration_relative_path": "", "declaration_line": "2", "declaration_kind": "FIELD_DECL"},
    ])
    _write_csv(root / "calls.csv", ["relationship_id", "caller_function_id", "callee_node_id", "callee_node_type", "call_line", "call_column", "call_type", "call_expression", "resolution_status", "original_resolution_status", "reconciliation_reason"], [])
    _write_csv(root / "data_access.csv", ["function_id", "field_id", "chain", "suffix2", "field_name", "field_type", "access_type", "line", "column"], [
        {"function_id": "fn_a", "field_id": "field_a", "chain": 'obj["a,b"]', "suffix2": "b", "field_name": "value", "field_type": "int", "access_type": key, "line": "3", "column": str(i)}
        for i, key in enumerate(ACCESS_TYPES)
    ])
    return root


def test_loader_converts_ids_types_and_preserves_relationship_rows(tmp_path):
    data = build_graph_data(mini_graph(tmp_path))
    assert data["nodes"]["Function"][0]["node_id"] == "Function:fn_a"
    assert data["nodes"]["Function"][0]["class_name"] is None
    assert data["nodes"]["Function"][0]["start_line"] == 1
    assert data["nodes"]["Function"][0]["source_available"] is True
    assert "source_code" not in data["nodes"]["Function"][0]
    assert data["nodes"]["DataField"][0]["declaration_relative_path"] == ""
    assert len(data["relationships"]["READS_WRITES"]) == 1
    assert data["relationships"]["READS"][0]["chain"] == 'obj["a,b"]'
    assert data["counts"]["relationships"] == {"CALLS": 0, "CALLS_EXTERNAL": 0, "READS": 1, "WRITES": 1, "READS_WRITES": 1, "POSSIBLE_WRITE": 1}


def test_active_graph_code_uses_node_id_and_has_no_neptune_transport(project_root):
    active = [project_root / "main.py", project_root / "neptune_anchor.py", project_root / "traversal/neptune_tools.py", project_root / "traversal/graph_client.py"]
    text = "\n".join(path.read_text(encoding="utf-8") for path in active)
    assert "neptunedata" not in text
    assert "execute_open_cypher_query" not in text
    query_sources = "\n".join((project_root / name).read_text(encoding="utf-8") for name in ("neptune_anchor.py", "traversal/neptune_tools.py"))
    assert not re.search(r"\bid\(", query_sources)
    assert "node_id" in query_sources
