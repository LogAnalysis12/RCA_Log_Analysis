#!/usr/bin/env python3
"""Additively import RCA Graph Evidence Model v2 artifacts into Neo4j.

Safe defaults:
- dry-run unless --apply is supplied
- never deletes nodes or relationships
- uses MERGE for node and relationship identities
- deliberately does not persist source_code/source excerpts
- skips and reports edges whose evidence endpoints cannot be resolved

When using --apply, NEO4J_PASSWORD is read from the process environment or a nearby .env file.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def load_project_env() -> Path | None:
    """Load environment settings from a nearby .env without overriding exported values.

    Searches the repository root, this tool's project directory, then the current
    working directory. Uses python-dotenv when available and a conservative
    fallback parser otherwise. Secret values are never printed.
    """
    script_path = Path(__file__).resolve()
    candidates = [
        script_path.parents[2] / ".env" if len(script_path.parents) > 2 else None,
        script_path.parents[1] / ".env" if len(script_path.parents) > 1 else None,
        Path.cwd() / ".env",
    ]
    env_path = next((p for p in candidates if p is not None and p.is_file()), None)
    if env_path is None:
        return None

    try:
        from dotenv import load_dotenv  # type: ignore
    except ImportError:
        # Minimal fallback for KEY=value dotenv files; existing shell variables win.
        for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key or key in os.environ:
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '\"'}:
                value = value[1:-1]
            elif " #" in value:
                value = value.split(" #", 1)[0].rstrip()
            os.environ[key] = value
    else:
        load_dotenv(dotenv_path=env_path, override=False)
    return env_path

# Dynamic Cypher labels and relationship names are restricted to this allowlist.
NODE_LABELS = {
    "Function", "DataField", "ExternalSymbol", "Callable", "CallSite",
    "Variable", "Parameter", "Type", "TranslationUnit", "Argument",
    "DataFlow", "LifetimeEvent", "PointerEvent", "ThreadSpawn",
    "LockUsageEvent", "SynchronizationEvent", "ControlFlowEvent",
    "MacroEvent", "ReturnFlow",
}
REL_TYPES = {
    "CALLS", "CALLS_EXTERNAL", "CALLS_AT", "HAS_CALL_SITE", "CALLS_VARIABLE",
    "READS", "WRITES", "READS_WRITES", "POSSIBLE_WRITE", "HAS_PARAMETER",
    "HAS_TYPE", "RETURNS_TYPE", "INHERITS", "TYPE_RELATIONSHIP",
    "POINTS_TO_TYPE", "REFERENCES_TYPE", "SMART_POINTER_TO_TYPE",
    "WEAKLY_REFERENCES_TYPE", "TYPE_ALIAS_OF", "ELEMENT_TYPE",
    "HAS_ARGUMENT", "PASSES_ARGUMENT", "ARGUMENT_TO_PARAMETER",
    "REFERENCES_VARIABLE", "DATA_FLOW_SOURCE", "DATA_FLOW_TARGET",
    "HAS_LIFETIME_EVENT", "AFFECTS", "HAS_POINTER_EVENT", "POINTER_SOURCE",
    "POINTER_TARGET", "SPAWNS_THREAD", "DISPATCHES_ASYNC",
    "PASSES_TO_THREAD_ENTRY", "HAS_LOCK_EVENT", "LOCK_OPERATION",
    "HAS_SYNC_EVENT", "SYNCHRONIZATION_OPERATION", "HAS_CONTROL_FLOW_EVENT",
    "POSSIBLY_EXPANDED_FROM", "HAS_MACRO_EVENT", "RETURN_REFERENCES",
    "RETURNS_FLOW", "HAS_TRANSLATION_UNIT", "DECLARES_VARIABLE",
}


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    csv.field_size_limit(sys.maxsize)
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    # Some exports contain a trailing all-empty CSV record. Ignore it rather than
    # creating a fake node or reporting a missing identifier for a blank line.
    return [
        row for row in rows
        if any(str(value or "").strip() for value in row.values())
    ]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8-sig") as f:
        for n, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at {path.name}:{n}: {exc}") from exc
            if isinstance(value, dict):
                rows.append(value)
    return rows


INT_PROPERTY_KEYS = {
    "start_line", "end_line", "declaration_line", "line", "column",
    "call_line", "call_column", "parameter_index", "argument_index",
    "pointer_depth", "scope_start_line", "scope_end_line", "target_line",
}


def safe_props(row: dict[str, Any], *, drop_source: bool = True) -> dict[str, Any]:
    """Keep Neo4j-compatible properties, normalize source coordinates, omit source excerpts."""
    result: dict[str, Any] = {}
    for key, value in row.items():
        if key in {"node_id", "start", "end", "source_code", "source_excerpt", "body_text"}:
            continue
        if drop_source and key.lower() in {"source_code", "source_excerpt", "function_body", "body_text"}:
            continue
        if value is None:
            # Do not erase existing Neo4j properties during an additive MERGE.
            continue
        if key in INT_PROPERTY_KEYS and isinstance(value, str) and value.strip():
            try:
                value = int(value)
            except ValueError:
                pass
        if isinstance(value, (str, int, float, bool)):
            result[key] = value
        elif isinstance(value, list) and all(x is None or isinstance(x, (str, int, float, bool)) for x in value):
            result[key] = [x for x in value if x is not None]
        # Nested objects are intentionally omitted instead of failing a Neo4j write.
    return result


def make_node_id(label: str, raw_id: Any) -> str | None:
    value = str(raw_id or "").strip()
    return f"{label}:{value}" if value else None


def ext_identity(row: dict[str, Any]) -> tuple[str, str]:
    name = str(row.get("qualified_name") or row.get("symbol_name") or row.get("callee_name") or "").strip()
    sig = str(row.get("signature") or row.get("callee_signature") or "").strip()
    return name, sig


def normalized_external_node_id(name: str, signature: str) -> str:
    # Match the identity convention in the existing load_neo4j.py.
    symbol_id = "ext_" + hashlib.sha256((name + "\0" + signature).encode("utf-8")).hexdigest()[:24]
    return "ExternalSymbol:" + symbol_id


def split_ids(value: Any) -> list[str]:
    if value is None:
        return []
    text = str(value).strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [str(x).strip() for x in parsed if str(x).strip()]
    except (json.JSONDecodeError, TypeError):
        pass
    # IDs may be serialized as a delimited string by CSV export.
    return [x.strip().strip("\"'[]") for x in re.split(r"[;,|]", text) if x.strip().strip("\"'[]")]


def build_plan(root: Path) -> tuple[dict[str, dict[str, dict[str, Any]]], dict[str, list[dict[str, Any]]], dict[str, Any]]:
    if not root.is_dir():
        raise SystemExit(f"Artifact directory does not exist: {root}")

    nodes: dict[str, dict[str, dict[str, Any]]] = {label: {} for label in NODE_LABELS}
    edges: dict[str, list[dict[str, Any]]] = defaultdict(list)
    maps: dict[str, dict[str, str]] = defaultdict(dict)
    stats: Counter[str] = Counter()
    skipped: Counter[str] = Counter()
    edge_sequence = 0

    def add_node(label: str, raw_id: Any, row: dict[str, Any], prefix: str | None = None) -> str | None:
        if label not in NODE_LABELS:
            raise ValueError(f"Unsupported node label: {label}")
        nid = f"{prefix or label}:{str(raw_id).strip()}" if str(raw_id or "").strip() else None
        if not nid:
            skipped[f"node_missing_id:{label}"] += 1
            return None
        props = safe_props(row)
        props["node_id"] = nid
        # Identity is canonical; never persist raw C/C++ source bodies.
        nodes[label][nid] = props
        stats[f"nodes:{label}"] += 1
        return nid

    def add_edge(rel: str, start: str | None, end: str | None, props: dict[str, Any] | None = None, identity: str | None = None, merge_fields: tuple[str, ...] = ("relationship_id",)) -> None:
        nonlocal edge_sequence
        if rel not in REL_TYPES:
            raise ValueError(f"Unsupported relationship type: {rel}")
        if not start or not end:
            skipped[f"edge_unresolved:{rel}"] += 1
            return
        edge_sequence += 1
        properties = safe_props(props or {})
        if not identity:
            identity = f"{rel}|{start}|{end}|{properties.get('relative_path','')}|{properties.get('line','')}|{edge_sequence}"
        rid = rel.lower() + "_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:28]
        properties.update(relationship_id=rid, start=start, end=end)
        # Import-control metadata is removed before it is sent to Neo4j.
        properties["__merge_fields"] = list(merge_fields)
        edges[rel].append(properties)

    # 1. Functions: preserve the identity convention of the current importer.
    function_rows = read_jsonl(root / "functions.jsonl")
    defined_fn_ids: set[str] = set()
    for row in function_rows:
        fid = str(row.get("function_id") or "").strip()
        nid = add_node("Function", fid, row)
        if fid and nid:
            maps["function"][fid] = nid
            defined_fn_ids.add(fid)
            nodes["Function"][nid].update(
                start_line=row.get("start_line"), end_line=row.get("end_line"),
                source_available=bool(str(row.get("source_code") or "").strip()),
                definition_available=True, extraction_status="EXTRACTED",
            )
    if len(defined_fn_ids) != len(function_rows):
        stats["function_rows_with_duplicate_or_missing_id"] += len(function_rows) - len(defined_fn_ids)

    # 2. Data fields.
    for row in read_csv(root / "data_fields.csv"):
        fid = str(row.get("field_id") or "").strip()
        nid = add_node("DataField", fid, row)
        if fid and nid:
            maps["field"][fid] = nid

    # 3. Extractor external symbols, using old loader's normalized key where possible.
    ext_rows = read_jsonl(root / "external_symbols.jsonl")
    for row in ext_rows:
        raw_sid = str(row.get("symbol_id") or "").strip()
        name, sig = ext_identity(row)
        nid = normalized_external_node_id(name, sig) if name else make_node_id("ExternalSymbol", raw_sid)
        if not nid:
            skipped["node_missing_id:ExternalSymbol"] += 1
            continue
        props = safe_props(row)
        props["node_id"] = nid
        props.setdefault("symbol_id", raw_sid)
        nodes["ExternalSymbol"][nid] = props
        if raw_sid:
            maps["external"][raw_sid] = nid
        if name:
            maps["external_key"][name + "\0" + sig] = nid

    # 4. Callables (usually lambdas).
    for row in read_jsonl(root / "callable_targets.jsonl"):
        cid = str(row.get("callable_id") or row.get("target_callable_id") or row.get("id") or "").strip()
        nid = add_node("Callable", cid, row)
        if cid and nid:
            maps["callable"][cid] = nid
            owner = str(row.get("owner_function_id") or row.get("function_id") or "").strip()
            add_edge("DECLARES_VARIABLE", maps["function"].get(owner), nid,
                     {"relative_path": row.get("relative_path", ""), "line": row.get("line", "")},
                     f"callable-owner|{owner}|{cid}")

    # 5. Ensure missing project definitions become explicit placeholders, as in old loader.
    call_rows = read_csv(root / "calls.csv")
    for row in call_rows:
        status = str(row.get("resolution_status") or "").strip()
        cid = str(row.get("callee_function_id") or "").strip()
        if status == "RESOLVED_PROJECT" and cid and cid not in maps["function"]:
            qname = str(row.get("qualified_callee_name") or row.get("callee_name") or "").strip()
            cname = str(row.get("callee_name") or "").strip()
            class_name = qname.split("::", 1)[0] if "::" in qname else ""
            nid = add_node("Function", cid, {
                "function_id": cid, "repository_id": "New_thread", "class_name": class_name,
                "function_name": cname, "qualified_name": qname,
                "signature": row.get("callee_signature", ""), "relative_path": "",
                "cursor_kind": "MISSING_DEFINITION", "start_line": "0", "end_line": "0",
                "source_available": False, "definition_available": False,
                "extraction_status": "MISSING_DEFINITION",
            })
            if nid:
                maps["function"][cid] = nid
                stats["placeholder_functions"] += 1

    # 6. Resolve/create external symbols for basic calls; normalize IDs to legacy format.
    for row in call_rows:
        if str(row.get("resolution_status") or "").strip() != "EXTERNAL":
            continue
        name = str(row.get("qualified_callee_name") or row.get("callee_name") or "").strip()
        sig = str(row.get("callee_signature") or "").strip()
        if not name:
            skipped["external_call_without_name"] += 1
            continue
        key = name + "\0" + sig
        nid = maps["external_key"].get(key) or normalized_external_node_id(name, sig)
        if nid not in nodes["ExternalSymbol"]:
            row_props = {
                "symbol_id": nid.split(":", 1)[-1], "repository_id": "New_thread",
                "symbol_name": row.get("callee_name") or name, "qualified_name": name,
                "signature": sig, "symbol_kind": row.get("call_type") or "UNKNOWN",
                "resolution_reason": "CALLS_CSV_EXTERNAL", "original_resolution_status": "EXTERNAL",
                "implementation_available": False, "source_available": False,
                "node_id": nid,
            }
            nodes["ExternalSymbol"][nid] = row_props
        maps["external_key"][key] = nid

    # 7. Variables/types/parameters/translation units.
    for row in read_csv(root / "variables.csv"):
        vid = str(row.get("variable_id") or "").strip()
        nid = add_node("Variable", vid, row)
        if vid and nid:
            maps["variable"][vid] = nid
            owner_fn = str(row.get("owner_function_id") or "").strip()
            owner_cb = str(row.get("owner_callable_id") or "").strip()
            add_edge("DECLARES_VARIABLE", maps["function"].get(owner_fn) or maps["callable"].get(owner_cb), nid,
                     {"relative_path": row.get("relative_path", ""), "line": row.get("line", "")},
                     f"declares-variable|{owner_fn}|{owner_cb}|{vid}")
    for row in read_csv(root / "types.csv"):
        tid = str(row.get("type_id") or "").strip()
        nid = add_node("Type", tid, row)
        if tid and nid:
            maps["type"][tid] = nid
    # Type edges are added after Type nodes exist.
    for row in read_csv(root / "variables.csv"):
        vid = str(row.get("variable_id") or "").strip()
        tid = str(row.get("type_id") or "").strip()
        if vid and tid and tid in maps["type"]:
            add_edge("HAS_TYPE", maps["variable"].get(vid), maps["type"].get(tid),
                     {"type_spelling": row.get("type_spelling", ""), "evidence_grade": row.get("evidence_grade", "")},
                     f"var-type|{vid}|{tid}")
    for row in read_csv(root / "function_parameters.csv"):
        pid = str(row.get("parameter_id") or "").strip()
        if not pid:
            skipped["parameter_missing_id"] += 1
            continue
        nid = add_node("Parameter", pid, row)
        if not nid:
            continue
        maps["parameter"][pid] = nid
        owner_fn = str(row.get("function_id") or "").strip()
        owner_cb = str(row.get("callable_id") or "").strip()
        add_edge("HAS_PARAMETER", maps["function"].get(owner_fn) or maps["callable"].get(owner_cb), nid,
                 {"parameter_index": row.get("parameter_index", ""), "relative_path": row.get("relative_path", ""), "line": row.get("line", "")},
                 f"has-param|{owner_fn}|{owner_cb}|{pid}")
        tid = str(row.get("type_id") or "").strip()
        if tid:
            add_edge("HAS_TYPE", nid, maps["type"].get(tid) or make_node_id("Type", tid),
                     {"type_spelling": row.get("type_spelling", "")}, f"param-type|{pid}|{tid}")
    for row in read_csv(root / "translation_units.csv"):
        raw_id = str(row.get("translation_unit_id") or row.get("unit_id") or row.get("relative_path") or row.get("path") or "").strip()
        nid = add_node("TranslationUnit", raw_id, row)
        if raw_id and nid:
            maps["translation_unit"][raw_id] = nid

    # 8. Existing coarse field-access model remains available for traversal compatibility.
    for i, row in enumerate(read_csv(root / "data_access.csv"), 1):
        fid, field_id = str(row.get("function_id") or "").strip(), str(row.get("field_id") or "").strip()
        access = str(row.get("access_type") or "").strip().upper()
        rel = {"READ": "READS", "WRITE": "WRITES", "READ_WRITE": "READS_WRITES", "POSSIBLE_WRITE": "POSSIBLE_WRITE"}.get(access)
        if not rel:
            skipped["data_access_unknown_type"] += 1
            continue
        add_edge(rel, maps["function"].get(fid), maps["field"].get(field_id), row,
                 f"data-access|{fid}|{field_id}|{row.get('line','')}|{row.get('column','')}|{i}",
                 merge_fields=("line", "column", "chain", "suffix2"))

    # 9. Detailed variable accesses.
    for i, row in enumerate(read_csv(root / "variable_accesses.csv"), 1):
        fid = str(row.get("function_id") or "").strip()
        cid = str(row.get("callable_id") or "").strip()
        vid = str(row.get("variable_id") or "").strip()
        access = str(row.get("access_type") or "").strip().upper()
        rel = {"READ": "READS", "WRITE": "WRITES", "READ_WRITE": "READS_WRITES", "POSSIBLE_WRITE": "POSSIBLE_WRITE"}.get(access)
        if not rel:
            skipped["variable_access_unknown_type"] += 1
            continue
        add_edge(rel, maps["function"].get(fid) or maps["callable"].get(cid), maps["variable"].get(vid), row,
                 f"variable-access|{fid}|{cid}|{vid}|{row.get('line','')}|{row.get('column','')}|{i}")

    # 10. Per-call-site nodes and resolvable target links.
    for row in read_csv(root / "call_sites.csv"):
        csid = str(row.get("call_site_id") or "").strip()
        nid = add_node("CallSite", csid, row)
        if not csid or not nid:
            continue
        maps["call_site"][csid] = nid
        caller_fn = str(row.get("caller_function_id") or "").strip()
        caller_cb = str(row.get("caller_callable_id") or "").strip()
        add_edge("HAS_CALL_SITE", maps["function"].get(caller_fn) or maps["callable"].get(caller_cb), nid, row,
                 f"callsite-owner|{csid}|{caller_fn}|{caller_cb}")
        callee_fn = str(row.get("callee_function_id") or "").strip()
        callee_ext = str(row.get("callee_external_symbol_id") or "").strip()
        callee_var = str(row.get("callee_variable_id") or "").strip()
        if callee_fn:
            add_edge("CALLS_AT", nid, maps["function"].get(callee_fn), row, f"callsite-target-fn|{csid}|{callee_fn}")
        elif callee_ext:
            add_edge("CALLS_EXTERNAL", nid, maps["external"].get(callee_ext), row, f"callsite-target-ext|{csid}|{callee_ext}")
        elif callee_var:
            add_edge("CALLS_VARIABLE", nid, maps["variable"].get(callee_var), row, f"callsite-target-var|{csid}|{callee_var}")
        elif str(row.get("resolution_status") or "").upper() == "EXTERNAL":
            name = str(row.get("callee_name") or "").strip()
            sig = str(row.get("callee_signature") or "").strip()
            ext_nid = maps["external_key"].get(name + "\0" + sig)
            if ext_nid:
                add_edge("CALLS_EXTERNAL", nid, ext_nid, row, f"callsite-target-ext-name|{csid}|{name}|{sig}")

    # 11. Preserve old direct function-level calls, idempotently.
    for i, row in enumerate(call_rows, 1):
        caller = str(row.get("caller_function_id") or "").strip()
        status = str(row.get("resolution_status") or "").strip()
        callee = str(row.get("callee_function_id") or "").strip()
        if status == "RESOLVED_PROJECT":
            target = maps["function"].get(callee)
            rel = "CALLS"
        elif status == "EXTERNAL":
            name = str(row.get("qualified_callee_name") or row.get("callee_name") or "").strip()
            sig = str(row.get("callee_signature") or "").strip()
            target = maps["external_key"].get(name + "\0" + sig)
            rel = "CALLS_EXTERNAL"
        else:
            skipped["calls_unknown_resolution"] += 1
            continue
        add_edge(rel, maps["function"].get(caller), target, row,
                 f"legacy-call|{caller}|{target}|{row.get('call_line','')}|{row.get('call_column','')}|{row.get('call_expression','')}|{i}")
        # Reuse the exact relationship ID convention of load_neo4j.py so overlapping
        # legacy CALLS/CALLS_EXTERNAL relationships MERGE rather than duplicate.
        relationship_identity = "\0".join((
            caller, target or "", str(row.get("call_line") or ""),
            str(row.get("call_column") or ""), str(row.get("call_expression") or ""), str(i),
        ))
        edges[rel][-1]["relationship_id"] = "call_" + hashlib.sha256(relationship_identity.encode("utf-8")).hexdigest()[:24]

    # 12. Call argument nodes and their evidence edges.
    for row in read_csv(root / "call_arguments.csv"):
        aid = str(row.get("argument_id") or "").strip()
        nid = add_node("Argument", aid, row)
        if not aid or not nid:
            continue
        maps["argument"][aid] = nid
        csid = str(row.get("call_site_id") or "").strip()
        add_edge("HAS_ARGUMENT", maps["call_site"].get(csid), nid, row, f"has-arg|{aid}|{csid}")
        pid = str(row.get("target_parameter_id") or "").strip()
        if pid:
            # The extractor uses both function_parameters.parameter_id and the
            # corresponding variables.variable_id for parameter declarations.
            # Prefer a dedicated Parameter node when present; otherwise link to
            # the Variable record that represents the same parameter declaration.
            parameter_target = maps["parameter"].get(pid)
            if parameter_target:
                stats["argument_target_resolved_to_parameter"] += 1
            else:
                parameter_target = maps["variable"].get(pid)
                if parameter_target:
                    stats["argument_target_resolved_to_variable_backed_parameter"] += 1
                else:
                    skipped["argument_target_unresolved"] += 1
            add_edge("ARGUMENT_TO_PARAMETER", nid, parameter_target, row, f"arg-param|{aid}|{pid}")
        for vid in split_ids(row.get("referenced_variable_ids")):
            add_edge("REFERENCES_VARIABLE", nid, maps["variable"].get(vid),
                     {"argument_index": row.get("argument_index", ""), "evidence_grade": row.get("evidence_grade", "")},
                     f"arg-var|{aid}|{vid}")
        caller_fn = str(row.get("caller_function_id") or "").strip()
        caller_cb = str(row.get("caller_callable_id") or "").strip()
        if csid and (caller_fn or caller_cb):
            add_edge("PASSES_ARGUMENT", maps["function"].get(caller_fn) or maps["callable"].get(caller_cb), nid, row, f"passes-arg|{aid}")

    # 13. Flows, including thread-entry argument flows.
    for row in read_csv(root / "data_flows.csv"):
        flow_id = str(row.get("flow_id") or "").strip()
        nid = add_node("DataFlow", flow_id, row)
        if not flow_id or not nid:
            continue
        maps["data_flow"][flow_id] = nid
        src = str(row.get("source_variable_id") or "").strip()
        dst = str(row.get("target_variable_id") or "").strip()
        dst_cb = str(row.get("target_callable_id") or "").strip()
        add_edge("DATA_FLOW_SOURCE", maps["variable"].get(src), nid, row, f"flow-src|{flow_id}|{src}")
        add_edge("DATA_FLOW_TARGET", nid, maps["variable"].get(dst) or maps["callable"].get(dst_cb), row, f"flow-dst|{flow_id}|{dst}|{dst_cb}")

    # 14. Evidence event nodes: preserve row properties and precise source locations.
    event_specs = [
        ("lifetime_events.csv", "LifetimeEvent", "event_id", "lifetime"),
        ("pointer_events.csv", "PointerEvent", "event_id", "pointer"),
        ("thread_spawns.csv", "ThreadSpawn", "spawn_id", "spawn"),
        ("lock_usage.csv", "LockUsageEvent", "lock_event_id", "lock"),
        ("synchronization_events.csv", "SynchronizationEvent", "event_id", "sync"),
        ("control_flow_events.csv", "ControlFlowEvent", "event_id", "control"),
        ("macro_events.csv", "MacroEvent", "macro_event_id", "macro"),
        ("return_flows.csv", "ReturnFlow", "return_id", "return"),
    ]
    event_rows: dict[str, list[dict[str, str]]] = {}
    for filename, label, id_key, map_key in event_specs:
        rows = read_csv(root / filename)
        event_rows[map_key] = rows
        for row in rows:
            raw_id = str(row.get(id_key) or "").strip()
            nid = add_node(label, raw_id, row)
            if raw_id and nid:
                maps[map_key][raw_id] = nid

    # Link lifetime and pointer evidence back to the source function/callable and only resolved targets.
    for row in event_rows["lifetime"]:
        eid = str(row.get("event_id") or "").strip()
        fid = str(row.get("function_id") or "").strip()
        cid = str(row.get("callable_id") or "").strip()
        enid = maps["lifetime"].get(eid)
        add_edge("HAS_LIFETIME_EVENT", maps["function"].get(fid) or maps["callable"].get(cid), enid, row, f"has-lifetime|{eid}")
        target_id = str(row.get("target_id") or "").strip()
        target_node = (maps["variable"].get(target_id) or maps["field"].get(target_id) or maps["type"].get(target_id) or maps["function"].get(target_id))
        add_edge("AFFECTS", enid, target_node, row, f"lifetime-target|{eid}|{target_id}")
        for mid in split_ids(row.get("macro_event_ids")):
            add_edge("POSSIBLY_EXPANDED_FROM", enid, maps["macro"].get(mid),
                     {"macro_event_names": row.get("macro_event_names", ""), "evidence_grade": row.get("evidence_grade", "")},
                     f"lifetime-macro|{eid}|{mid}")

    for row in event_rows["pointer"]:
        eid = str(row.get("event_id") or "").strip()
        fid = str(row.get("function_id") or "").strip()
        cid = str(row.get("callable_id") or "").strip()
        enid = maps["pointer"].get(eid)
        add_edge("HAS_POINTER_EVENT", maps["function"].get(fid) or maps["callable"].get(cid), enid, row, f"has-pointer|{eid}")
        for col, rel in (("source_id", "POINTER_SOURCE"), ("target_id", "POINTER_TARGET")):
            raw = str(row.get(col) or "").strip()
            endpoint = maps["variable"].get(raw) or maps["field"].get(raw) or maps["type"].get(raw) or maps["function"].get(raw)
            add_edge(rel, enid, endpoint, {"endpoint_kind": col, "expression": row.get("expression", ""), "evidence_grade": row.get("evidence_grade", "")}, f"pointer-{col}|{eid}|{raw}")
        for mid in split_ids(row.get("macro_event_ids")):
            add_edge("POSSIBLY_EXPANDED_FROM", enid, maps["macro"].get(mid),
                     {"macro_event_names": row.get("macro_event_names", ""), "evidence_grade": row.get("evidence_grade", "")},
                     f"pointer-macro|{eid}|{mid}")

    for row in event_rows["spawn"]:
        sid = str(row.get("spawn_id") or "").strip()
        snid = maps["spawn"].get(sid)
        fid = str(row.get("spawner_function_id") or "").strip()
        cid = str(row.get("spawner_callable_id") or "").strip()
        target_fn = str(row.get("target_function_id") or "").strip()
        target_cb = str(row.get("target_callable_id") or "").strip()
        target_ext = str(row.get("target_external_symbol_id") or "").strip()
        target_var = str(row.get("target_variable_id") or "").strip()
        spawner = maps["function"].get(fid) or maps["callable"].get(cid)
        target = maps["function"].get(target_fn) or maps["callable"].get(target_cb) or maps["external"].get(target_ext) or maps["variable"].get(target_var)
        kind = str(row.get("spawn_kind") or "").upper()
        spawn_rel = "DISPATCHES_ASYNC" if "ASYNC" in kind else "SPAWNS_THREAD"
        add_edge(spawn_rel, spawner, snid, row, f"spawner-event|{sid}|{fid}|{cid}")
        add_edge(spawn_rel, snid, target, row, f"spawn-target|{sid}|{target_fn}|{target_cb}|{target_ext}|{target_var}")

    for row in event_rows["lock"]:
        eid = str(row.get("lock_event_id") or "").strip()
        fid = str(row.get("function_id") or "").strip()
        cid = str(row.get("callable_id") or "").strip()
        enid = maps["lock"].get(eid)
        add_edge("HAS_LOCK_EVENT", maps["function"].get(fid) or maps["callable"].get(cid), enid, row, f"has-lock|{eid}")
        raw = str(row.get("lock_variable_id") or row.get("mutex_id") or "").strip()
        endpoint = maps["variable"].get(raw) or maps["field"].get(raw)
        add_edge("LOCK_OPERATION", enid, endpoint, row, f"lock-target|{eid}|{raw}")

    for row in event_rows["sync"]:
        eid = str(row.get("event_id") or "").strip()
        fid = str(row.get("function_id") or "").strip()
        cid = str(row.get("callable_id") or "").strip()
        enid = maps["sync"].get(eid)
        add_edge("HAS_SYNC_EVENT", maps["function"].get(fid) or maps["callable"].get(cid), enid, row, f"has-sync|{eid}")
        raw = str(row.get("target_id") or "").strip()
        endpoint = maps["variable"].get(raw) or maps["field"].get(raw) or maps["type"].get(raw)
        add_edge("SYNCHRONIZATION_OPERATION", enid, endpoint, row, f"sync-target|{eid}|{raw}")

    for row in event_rows["control"]:
        eid = str(row.get("event_id") or "").strip()
        fid = str(row.get("function_id") or "").strip()
        cid = str(row.get("callable_id") or "").strip()
        add_edge("HAS_CONTROL_FLOW_EVENT", maps["function"].get(fid) or maps["callable"].get(cid), maps["control"].get(eid), row, f"has-control|{eid}")

    # Return-flow facts connect source function/callable to evidence node and referenced variables.
    for row in event_rows["return"]:
        rid = str(row.get("return_id") or "").strip()
        fid = str(row.get("function_id") or "").strip()
        cid = str(row.get("callable_id") or "").strip()
        rnode = maps["return"].get(rid)
        add_edge("RETURNS_FLOW", maps["function"].get(fid) or maps["callable"].get(cid), rnode, row, f"returns-flow|{rid}")
        for vid in split_ids(row.get("referenced_variable_ids")):
            add_edge("RETURN_REFERENCES", rnode, maps["variable"].get(vid), row, f"return-var|{rid}|{vid}")

    # 15. Macro evidence: attach macro events to functions/call sites by exact file+line only.
    macro_index: dict[tuple[str, str], list[str]] = defaultdict(list)
    for row in event_rows["macro"]:
        macro_index[(str(row.get("relative_path") or ""), str(row.get("line") or ""))].append(str(row.get("macro_event_id") or ""))
    for label, key in (("lifetime", "function_id"), ("pointer", "function_id"), ("sync", "function_id"), ("control", "function_id")):
        for row in event_rows[label]:
            loc = (str(row.get("relative_path") or ""), str(row.get("line") or ""))
            eid = str(row.get("event_id") or "")
            if label == "lifetime": enid = maps["lifetime"].get(eid)
            elif label == "pointer": enid = maps["pointer"].get(eid)
            elif label == "sync": enid = maps["sync"].get(eid)
            else: enid = maps["control"].get(eid)
            for mid in macro_index.get(loc, []):
                add_edge("POSSIBLY_EXPANDED_FROM", enid, maps["macro"].get(mid),
                         {"evidence_grade": "HEURISTIC_SAME_FILE_LINE", "relative_path": loc[0], "line": loc[1]},
                         f"same-line-macro|{label}|{eid}|{mid}")

    # 16. Type relationships and function return types.
    type_rel_map = {
        "INHERITS": "INHERITS", "POINTS_TO_TYPE": "POINTS_TO_TYPE",
        "REFERENCES_TYPE": "REFERENCES_TYPE", "SMART_POINTER_TO_TYPE": "SMART_POINTER_TO_TYPE",
        "WEAKLY_REFERENCES_TYPE": "WEAKLY_REFERENCES_TYPE", "TYPE_ALIAS_OF": "TYPE_ALIAS_OF",
        "ELEMENT_TYPE": "ELEMENT_TYPE",
    }
    for row in read_csv(root / "type_relationships.csv"):
        raw_kind = str(row.get("relationship_type") or "").strip().upper()
        rel = type_rel_map.get(raw_kind, "TYPE_RELATIONSHIP")
        src = str(row.get("source_type_id") or row.get("child_type_id") or "").strip()
        dst = str(row.get("target_type_id") or row.get("base_type_id") or "").strip()
        if src and dst:
            props = dict(row)
            props["semantic_relationship_type"] = raw_kind
            add_edge(rel, maps["type"].get(src), maps["type"].get(dst), props, f"type-rel|{row.get('relationship_id','')}|{src}|{dst}|{raw_kind}")
    for row in read_csv(root / "types.csv"):
        src = str(row.get("type_id") or "").strip()
        dst = str(row.get("pointee_type_id") or "").strip()
        if src and dst:
            kind = str(row.get("smart_pointer_kind") or "").lower()
            rel = "SMART_POINTER_TO_TYPE" if kind else "POINTS_TO_TYPE"
            add_edge(rel, maps["type"].get(src), maps["type"].get(dst),
                     {"pointee_type": row.get("pointee_type", ""), "ownership_model_hint": row.get("ownership_model_hint", ""), "evidence_grade": row.get("evidence_grade", "")},
                     f"pointee-type|{src}|{dst}|{rel}")
    for row in read_csv(root / "function_return_types.csv"):
        fid = str(row.get("function_id") or "").strip()
        tid = str(row.get("return_type_id") or "").strip()
        if fid and tid:
            add_edge("RETURNS_TYPE", maps["function"].get(fid), maps["type"].get(tid), row,
                     f"function-return-type|{fid}|{tid}|{row.get('relative_path','')}|{row.get('line','')}")

    # 17. Add a semantic bridge from new Function nodes to corresponding DataField through its name only
    # is intentionally omitted: name-based field aliasing is not reliable enough to invent edges.

    planned_nodes = {label: list(by_id.values()) for label, by_id in nodes.items() if by_id}
    planned_edges = {rel: rows for rel, rows in edges.items() if rows}
    # Resolve edges only to nodes planned in this import. This makes omissions visible pre-write.
    node_ids = {nid for rows in nodes.values() for nid in rows}
    resolved_edges: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rel, rows in planned_edges.items():
        for edge in rows:
            if edge["start"] not in node_ids or edge["end"] not in node_ids:
                skipped[f"edge_endpoint_not_in_plan:{rel}"] += 1
                continue
            resolved_edges[rel].append(edge)

    summary = {
        "artifact_directory": str(root),
        "nodes": {label: len(rows) for label, rows in planned_nodes.items()},
        "relationships": {rel: len(rows) for rel, rows in resolved_edges.items()},
        "skipped": dict(sorted(skipped.items())),
        "stats": dict(sorted(stats.items())),
        "source_code_persisted": False,
        "database_deletions": 0,
    }
    return planned_nodes, dict(resolved_edges), summary


def _chunks(rows: list[dict[str, Any]], size: int = 500):
    for offset in range(0, len(rows), size):
        yield rows[offset:offset + size]


def apply_plan(uri: str, user: str, database: str, password: str,
               nodes: dict[str, list[dict[str, Any]]], edges: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(uri, auth=(user, password), connection_timeout=20)
    counts: dict[str, Any] = {"nodes_merged": {}, "relationships_merged": {}}
    try:
        with driver.session(database=database) as session:
            # Add per-label uniqueness constraints; no drops and no deletes.
            existing_constraint_names = {
                # These exact constraint names are already used by the current importer.
                "Function": "fn_node",
                "DataField": "field_node",
                "ExternalSymbol": "sym_node",
            }
            for label in nodes:
                if label not in NODE_LABELS:
                    raise ValueError(f"Unapproved label: {label}")
                constraint_name = existing_constraint_names.get(
                    label, "uniq_" + re.sub(r"[^A-Za-z0-9_]", "_", label.lower()) + "_node_id"
                )
                session.run(
                    f"CREATE CONSTRAINT {constraint_name} IF NOT EXISTS "
                    f"FOR (n:{label}) REQUIRE n.node_id IS UNIQUE"
                ).consume()

            # Merge nodes without downgrading an existing Function definition.
            for label, rows in nodes.items():
                if not rows:
                    continue

                total = 0

                if label == "Function":
                    defined_rows = [
                        row for row in rows
                        if row.get("extraction_status") != "MISSING_DEFINITION"
                    ]
                    placeholder_rows = [
                        row for row in rows
                        if row.get("extraction_status") == "MISSING_DEFINITION"
                    ]

                    # Real definitions may update existing Function nodes.
                    for batch in _chunks(defined_rows):
                        result = session.run(
                            "UNWIND $rows AS row "
                            "MERGE (n:Function {node_id: row.node_id}) "
                            "SET n += row RETURN count(n) AS touched",
                            rows=batch,
                        ).single()
                        total += int(result["touched"] or 0)

                    # Placeholders create nodes only when no matching node exists.
                    for batch in _chunks(placeholder_rows):
                        result = session.run(
                            "UNWIND $rows AS row "
                            "MERGE (n:Function {node_id: row.node_id}) "
                            "ON CREATE SET n += row "
                            "RETURN count(n) AS touched",
                            rows=batch,
                        ).single()
                        total += int(result["touched"] or 0)

                else:
                    for batch in _chunks(rows):
                        result = session.run(
                            f"UNWIND $rows AS row "
                            f"MERGE (n:{label} {{node_id: row.node_id}}) "
                            "SET n += row RETURN count(n) AS touched",
                            rows=batch,
                        ).single()
                        total += int(result["touched"] or 0)

                counts["nodes_merged"][label] = total

            # Merge by stable identity. Legacy coarse data-access edges predate
            # relationship_id, so they use the same location/chain properties as the old importer.
            for rel, rows in edges.items():
                if rel not in REL_TYPES:
                    raise ValueError(f"Unapproved relationship type: {rel}")
                grouped: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
                for edge in rows:
                    grouped[tuple(edge.get("__merge_fields") or ["relationship_id"])].append(edge)
                total = 0
                for merge_fields, group_rows in grouped.items():
                    if not merge_fields or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k) for k in merge_fields):
                        raise ValueError(f"Unsafe relationship merge keys: {merge_fields!r}")
                    merge_pattern = ", ".join(f"{key}: row.{key}" for key in merge_fields)
                    cleaned_rows = []
                    for edge in group_rows:
                        cleaned = dict(edge)
                        cleaned.pop("__merge_fields", None)
                        cleaned_rows.append(cleaned)
                    for batch in _chunks(cleaned_rows):
                        result = session.run(
                            f"UNWIND $rows AS row MATCH (a {{node_id: row.start}}) "
                            f"MATCH (b {{node_id: row.end}}) "
                            f"MERGE (a)-[r:{rel} {{{merge_pattern}}}]->(b) "
                            "SET r += row RETURN count(r) AS touched",
                            rows=batch,
                        ).single()
                        total += int(result["touched"] or 0)
                counts["relationships_merged"][rel] = total

            counts["live_node_counts"] = {}
            for label in nodes:
                result = session.run(f"MATCH (n:{label}) RETURN count(n) AS c").single()
                counts["live_node_counts"][label] = int(result["c"])
            counts["live_relationship_counts"] = {}
            for rel in edges:
                result = session.run(f"MATCH ()-[r:{rel}]->() RETURN count(r) AS c").single()
                counts["live_relationship_counts"][rel] = int(result["c"])
    finally:
        driver.close()
    return counts


def main() -> int:
    loaded_env_path = load_project_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact_directory", type=Path)
    parser.add_argument("--uri", default="bolt://localhost:7687")
    parser.add_argument("--user", default="neo4j")
    parser.add_argument("--database", default="neo4j")
    parser.add_argument("--apply", action="store_true", help="Actually write to Neo4j. Without this flag only a local dry-run plan is produced.")
    parser.add_argument("--report", type=Path, help="Write plan/import summary JSON to this path.")
    args = parser.parse_args()

    nodes, edges, summary = build_plan(args.artifact_directory)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    if not args.apply:
        print("\nDRY RUN ONLY: database was not contacted or modified. Re-run with --apply to import additively.")
        result: dict[str, Any] = {"mode": "dry_run", **summary}
    else:
        password = os.environ.get("NEO4J_PASSWORD")
        if not password:
            searched = [
                str(Path(__file__).resolve().parents[2] / ".env"),
                str(Path(__file__).resolve().parents[1] / ".env"),
                str(Path.cwd() / ".env"),
            ]
            parser.error(
                "NEO4J_PASSWORD is not set. The importer searched nearby .env files "
                f"({'; '.join(searched)}) and the process environment. "
                "Add NEO4J_PASSWORD to the project's .env file or export it locally."
            )
        counts = apply_plan(args.uri, args.user, args.database, password, nodes, edges)
        result = {"mode": "apply", **summary, "neo4j": counts}
        print("\n=== NEO4J IMPORT COUNTS ===")
        print(json.dumps(counts, indent=2, ensure_ascii=False))
        print("\nNo nodes or relationships were deleted by this importer.")

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"\nReport written: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
