from __future__ import annotations

from pathlib import Path
from typing import Any


from traversal.source_reader import SourceReaderError, read_source_range


class NeptuneToolError(RuntimeError):
    pass


def strip_id_space(node_id: str) -> str:
    return node_id.split(":", 1)[1] if node_id and ":" in node_id else node_id or ""


def ensure_id(node_id: str, space: str, prefix: str) -> str:
    if not node_id:
        raise NeptuneToolError(f"{space} ID is required.")
    if node_id.startswith(f"{space}:"):
        return node_id
    return f"{space}:{node_id}" if node_id.startswith(prefix) else node_id


def ensure_function_id(value: str) -> str:
    return ensure_id(value, "Function", "fn_")


def ensure_field_id(value: str) -> str:
    return ensure_id(value, "DataField", "field_")


def _group_by_id(items: list[dict[str, Any]], id_key: str) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in items:
        node_id = item[id_key]
        if node_id not in grouped:
            copy = {k: v for k, v in item.items() if k != "call_site"}
            copy["call_sites"] = []
            copy["call_occurrence_count"] = 0
            grouped[node_id] = copy
            order.append(node_id)
        grouped[node_id]["call_sites"].append(item["call_site"])
        grouped[node_id]["call_occurrence_count"] += 1
    return [grouped[node_id] for node_id in order]


class NeptuneTraversalTools:
    def __init__(self, graph_client, source_root: str | Path, ignored_fields: list[str] | None = None, ignored_project_callees: list[str] | None = None, ignored_external_prefixes: list[str] | None = None) -> None:
        self.graph = graph_client
        self.source_root = Path(source_root).expanduser().resolve()
        self.ignored_fields = {x.strip() for x in (ignored_fields or []) if x.strip()}
        self.ignored_project_callees = {x.strip() for x in (ignored_project_callees or []) if x.strip()}
        self.ignored_external_prefixes = tuple(x.strip() for x in (ignored_external_prefixes or []) if x.strip())

    def _execute(self, query: str, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return self.graph.query(query, parameters or {})

    def get_engine_status(self) -> dict[str, Any]:
        return self.graph.get_engine_status()

    def get_function(self, function_id: str) -> dict[str, Any] | None:
        node_id = ensure_function_id(function_id)
        rows = self._execute(
            """
            MATCH (f:Function) WHERE f.node_id = $id
            RETURN f.node_id AS neptune_node_id,
                   f.repository_id AS repository_id,
                   f.class_name AS class_name,
                   f.function_name AS function_name,
                   f.qualified_name AS qualified_name,
                   f.signature AS signature,
                   f.relative_path AS relative_path,
                   f.start_line AS start_line,
                   f.end_line AS end_line,
                   f.content_hash AS content_hash,
                   f.cursor_kind AS cursor_kind,
                   f.source_available AS source_available
            LIMIT 2
            """,
            {"id": node_id},
        )
        if not rows:
            return None
        if len(rows) != 1:
            raise NeptuneToolError(f"Multiple Function nodes returned for {node_id}.")
        rows[0]["function_id"] = strip_id_space(rows[0]["neptune_node_id"])
        return rows[0]

    def read_function_source(self, function_id: str) -> dict[str, Any]:
        function = self.get_function(function_id)
        if not function:
            return {"status": "FUNCTION_NOT_FOUND", "function_id": function_id, "function": None, "source": None, "warnings": []}
        if not function.get("relative_path"):
            return {"status": "SOURCE_UNAVAILABLE", "function_id": function["function_id"], "function": function, "source": None, "warnings": ["No relative source path."]}
        try:
            source = read_source_range(
                self.source_root,
                function["relative_path"],
                int(function["start_line"]),
                int(function["end_line"]),
                function.get("content_hash") or "",
            )
        except (SourceReaderError, TypeError, ValueError) as error:
            return {"status": "SOURCE_READ_FAILED", "function_id": function["function_id"], "function": function, "source": None, "warnings": [str(error)]}
        status = "SOURCE_VERIFIED" if source.content_hash_matches is True else "SOURCE_HASH_MISMATCH" if source.content_hash_matches is False else "SOURCE_READ_UNVERIFIED"

        # Verify the original source hash first, then remove evaluation
        # annotations from the evidence exposed to the investigator and critic.
        from traversal.source_redaction import redact_source_payload

        source_payload, redaction = redact_source_payload(source.to_dict())
        warnings = list(source.warnings)
        if redaction["applied"]:
            warnings.append(
                "Evaluation annotations were redacted after source hash "
                "verification. Original source line numbers are preserved."
            )

        return {
            "status": status,
            "function_id": function["function_id"],
            "neptune_node_id": function["neptune_node_id"],
            "function": function,
            "source": source_payload,
            "warnings": warnings,
            "redaction": redaction,
        }

    @staticmethod
    def _function_item(row: dict[str, Any], prefix: str) -> dict[str, Any]:
        node_id = row.get(f"{prefix}_neptune_node_id", "")
        return {
            "function_id": strip_id_space(node_id),
            "neptune_node_id": node_id,
            "qualified_name": row.get(f"{prefix}_qualified_name", ""),
            "function_name": row.get(f"{prefix}_function_name", ""),
            "class_name": row.get(f"{prefix}_class_name", ""),
            "signature": row.get(f"{prefix}_signature", ""),
            "relative_path": row.get(f"{prefix}_relative_path", ""),
            "start_line": row.get(f"{prefix}_start_line"),
            "end_line": row.get(f"{prefix}_end_line"),
            "content_hash": row.get(f"{prefix}_content_hash", ""),
        }

    @staticmethod
    def _call_site(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "line": row.get("call_line"),
            "column": row.get("call_column"),
            "call_type": row.get("call_type", ""),
            "expression": row.get("call_expression", ""),
            "resolution_status": row.get("resolution_status", ""),
            "relationship_type": row.get("relationship_type", ""),
        }

    def find_callers(self, function_id: str, limit: int = 100) -> dict[str, Any]:
        target_id = ensure_function_id(function_id)
        rows = self._execute(
            """
            MATCH (caller:Function)-[c:CALLS|SPAWNS]->(target:Function)
            WHERE target.node_id = $id
            RETURN target.qualified_name AS target_qualified_name,
                   type(c) AS relationship_type,
                   caller.node_id AS caller_neptune_node_id,
                   caller.qualified_name AS caller_qualified_name,
                   caller.function_name AS caller_function_name,
                   caller.class_name AS caller_class_name,
                   caller.signature AS caller_signature,
                   caller.relative_path AS caller_relative_path,
                   caller.start_line AS caller_start_line,
                   caller.end_line AS caller_end_line,
                   caller.content_hash AS caller_content_hash,
                   c.call_line AS call_line, c.call_column AS call_column,
                   c.call_type AS call_type, c.call_expression AS call_expression,
                   c.resolution_status AS resolution_status
            ORDER BY caller.relative_path, c.call_line, c.call_column, caller.node_id
            LIMIT $limit
            """,
            {"id": target_id, "limit": max(1, min(int(limit), 300))},
        )
        occurrences = []
        for row in rows:
            item = self._function_item(row, "caller")
            item["call_site"] = self._call_site(row)
            occurrences.append(item)
        unique = _group_by_id(occurrences, "neptune_node_id")
        return {
            "status": "OK",
            "target_function_id": strip_id_space(target_id),
            "target_neptune_node_id": target_id,
            "target_qualified_name": rows[0].get("target_qualified_name", "") if rows else "",
            "call_occurrence_count": len(occurrences),
            "unique_caller_count": len(unique),
            "caller_count": len(unique),
            "callers": unique,
            "all_call_occurrences_for_audit": occurrences,
        }

    def find_callees(self, function_id: str, limit: int = 200) -> dict[str, Any]:
        caller_id = ensure_function_id(function_id)
        safe_limit = max(1, min(int(limit), 500))
        project_rows = self._execute(
            """
            MATCH (caller:Function)-[c:CALLS|SPAWNS]->(callee:Function)
            WHERE caller.node_id = $id
            RETURN caller.qualified_name AS caller_qualified_name,
                   type(c) AS relationship_type,
                   callee.node_id AS callee_neptune_node_id,
                   callee.qualified_name AS callee_qualified_name,
                   callee.function_name AS callee_function_name,
                   callee.class_name AS callee_class_name,
                   callee.signature AS callee_signature,
                   callee.relative_path AS callee_relative_path,
                   callee.start_line AS callee_start_line,
                   callee.end_line AS callee_end_line,
                   callee.content_hash AS callee_content_hash,
                   c.call_line AS call_line, c.call_column AS call_column,
                   c.call_type AS call_type, c.call_expression AS call_expression,
                   c.resolution_status AS resolution_status
            ORDER BY c.call_line, c.call_column, callee.node_id
            LIMIT $limit
            """,
            {"id": caller_id, "limit": safe_limit},
        )
        external_rows = self._execute(
            """
            MATCH (caller:Function)-[c:CALLS_EXTERNAL]->(callee:ExternalSymbol)
            WHERE caller.node_id = $id
            RETURN caller.qualified_name AS caller_qualified_name,
                   callee.node_id AS external_neptune_node_id,
                   callee.qualified_name AS external_qualified_name,
                   callee.symbol_name AS external_symbol_name,
                   callee.signature AS external_signature,
                   callee.symbol_kind AS external_symbol_kind,
                   callee.source_available AS external_source_available,
                   callee.implementation_available AS external_implementation_available,
                   callee.resolution_reason AS external_resolution_reason,
                   c.call_line AS call_line, c.call_column AS call_column,
                   c.call_type AS call_type, c.call_expression AS call_expression,
                   c.resolution_status AS resolution_status
            ORDER BY c.call_line, c.call_column, callee.node_id
            LIMIT $limit
            """,
            {"id": caller_id, "limit": safe_limit},
        )
        project_occurrences = []
        for row in project_rows:
            item = self._function_item(row, "callee")
            item["call_site"] = self._call_site(row)
            project_occurrences.append(item)
        external_occurrences = []
        for row in external_rows:
            node_id = row.get("external_neptune_node_id", "")
            external_occurrences.append({
                "symbol_id": strip_id_space(node_id),
                "neptune_node_id": node_id,
                "qualified_name": row.get("external_qualified_name", ""),
                "symbol_name": row.get("external_symbol_name", ""),
                "signature": row.get("external_signature", ""),
                "symbol_kind": row.get("external_symbol_kind", ""),
                "source_available": row.get("external_source_available"),
                "implementation_available": row.get("external_implementation_available"),
                "resolution_reason": row.get("external_resolution_reason", ""),
                "call_site": self._call_site(row),
            })
        unique_project = _group_by_id(project_occurrences, "neptune_node_id")
        unique_external = _group_by_id(external_occurrences, "neptune_node_id")
        relevant_project = [x for x in unique_project if x["qualified_name"] not in self.ignored_project_callees]
        ignored_project = [x for x in unique_project if x["qualified_name"] in self.ignored_project_callees]
        relevant_external = [x for x in unique_external if not x["qualified_name"].startswith(self.ignored_external_prefixes)]
        ignored_external = [x for x in unique_external if x["qualified_name"].startswith(self.ignored_external_prefixes)]
        caller_name = project_rows[0].get("caller_qualified_name", "") if project_rows else external_rows[0].get("caller_qualified_name", "") if external_rows else ""
        return {
            "status": "OK",
            "caller_function_id": strip_id_space(caller_id),
            "caller_neptune_node_id": caller_id,
            "caller_qualified_name": caller_name,
            "project_call_occurrence_count": len(project_occurrences),
            "external_call_occurrence_count": len(external_occurrences),
            "unique_project_callee_count": len(unique_project),
            "unique_external_symbol_count": len(unique_external),
            "relevant_project_callee_count": len(relevant_project),
            "relevant_external_callee_count": len(relevant_external),
            "project_callees": relevant_project,
            "external_callees": relevant_external,
            "ignored_project_callees": ignored_project,
            "ignored_external_callees": ignored_external,
            "all_project_callees_for_audit": unique_project,
            "all_external_callees_for_audit": unique_external,
            "all_project_call_occurrences_for_audit": project_occurrences,
            "all_external_call_occurrences_for_audit": external_occurrences,
            "external_boundary_present": bool(relevant_external),
        }

    def find_spawned_workers(self, function_id: str, limit: int = 100) -> dict[str, Any]:
        """Return worker functions launched through the two-edge ThreadSpawn pattern.

        This is deliberately separate from find_callees(): the repository graph
        represents thread creation as Function -> ThreadSpawn -> Function, not
        necessarily as a direct Function-to-Function CALLS/SPAWNS edge.
        """
        parent_id = ensure_function_id(function_id)
        safe_limit = max(1, min(int(limit), 300))
        rows = self._execute(
            """
            MATCH (parent:Function)-[r1:SPAWNS_THREAD]->(spawn:ThreadSpawn)
                  -[r2:SPAWNS_THREAD]->(worker:Function)
            WHERE parent.node_id = $id
            WITH parent, spawn, worker, properties(spawn) AS sp,
                 properties(r1) AS parent_spawn_props,
                 properties(r2) AS spawn_worker_props
            RETURN parent.node_id AS parent_neptune_node_id,
                   parent.qualified_name AS parent_qualified_name,
                   parent.relative_path AS parent_relative_path,
                   worker.node_id AS worker_neptune_node_id,
                   worker.qualified_name AS worker_qualified_name,
                   worker.function_name AS worker_function_name,
                   worker.class_name AS worker_class_name,
                   worker.signature AS worker_signature,
                   worker.relative_path AS worker_relative_path,
                   worker.start_line AS worker_start_line,
                   worker.end_line AS worker_end_line,
                   worker.content_hash AS worker_content_hash,
                   coalesce(sp["line"], sp["call_line"], sp["start_line"]) AS spawn_line,
                   coalesce(sp["relative_path"], sp["file"], sp["source_file"]) AS spawn_relative_path,
                   coalesce(sp["expression"], sp["call_expression"], sp["thread_expression"]) AS spawn_expression,
                   sp AS spawn_evidence,
                   parent_spawn_props AS parent_spawn_relationship_properties,
                   spawn_worker_props AS spawn_worker_relationship_properties
            ORDER BY worker_relative_path, spawn_line, worker_neptune_node_id
            LIMIT $limit
            """,
            {"id": parent_id, "limit": safe_limit},
        )
        workers = []
        for row in rows:
            worker_node_id = str(row.get("worker_neptune_node_id") or "")
            workers.append({
                "function_id": strip_id_space(worker_node_id),
                "neptune_node_id": worker_node_id,
                "qualified_name": row.get("worker_qualified_name", ""),
                "function_name": row.get("worker_function_name", ""),
                "class_name": row.get("worker_class_name", ""),
                "signature": row.get("worker_signature", ""),
                "relative_path": row.get("worker_relative_path", ""),
                "start_line": row.get("worker_start_line"),
                "end_line": row.get("worker_end_line"),
                "content_hash": row.get("worker_content_hash", ""),
                "spawn_site": {
                    "relative_path": row.get("spawn_relative_path") or row.get("parent_relative_path", ""),
                    "line": row.get("spawn_line"),
                    "expression": row.get("spawn_expression", ""),
                    "evidence": row.get("spawn_evidence") or {},
                    "function_to_spawn_relationship": row.get("parent_spawn_relationship_properties") or {},
                    "spawn_to_worker_relationship": row.get("spawn_worker_relationship_properties") or {},
                },
            })
        return {
            "status": "OK",
            "launcher_function_id": strip_id_space(parent_id),
            "launcher_neptune_node_id": parent_id,
            "launcher_qualified_name": rows[0].get("parent_qualified_name", "") if rows else "",
            "spawned_worker_count": len(workers),
            "spawned_workers": workers,
            "truncated": len(rows) >= safe_limit,
        }

    def find_fields_read(self, function_id: str, limit: int = 200, include_ignored: bool = False) -> dict[str, Any]:
        node_id = ensure_function_id(function_id)
        rows = self._execute(
            """
            MATCH (f:Function)-[a]->(d:DataField)
            WHERE f.node_id = $id AND type(a) IN ['READS','READS_WRITES','POSSIBLE_WRITE']
            RETURN f.qualified_name AS function_qualified_name,
                   d.node_id AS field_neptune_node_id,
                   d.qualified_name AS field_qualified_name,
                   d.field_name AS field_name,
                   d.field_type AS field_type,
                   d.declaration_relative_path AS declaration_relative_path,
                   d.declaration_line AS declaration_line,
                   type(a) AS access_type, a.line AS access_line,
                   a.column AS access_column, a.chain AS access_chain
            ORDER BY a.line, a.column, d.node_id
            LIMIT $limit
            """,
            {"id": node_id, "limit": max(1, min(int(limit), 500))},
        )
        all_fields = []
        for row in rows:
            field_node = row.get("field_neptune_node_id", "")
            item = {
                "field_id": strip_id_space(field_node),
                "neptune_node_id": field_node,
                "qualified_name": row.get("field_qualified_name", ""),
                "field_name": row.get("field_name", ""),
                "field_type": row.get("field_type", ""),
                "declaration_relative_path": row.get("declaration_relative_path", ""),
                "declaration_line": row.get("declaration_line"),
                "access": {
                    "type": row.get("access_type", ""),
                    "line": row.get("access_line"),
                    "column": row.get("access_column"),
                    "chain": row.get("access_chain", ""),
                },
            }
            item["ignored"] = item["field_name"] in self.ignored_fields or item["qualified_name"] in self.ignored_fields
            all_fields.append(item)
        relevant = [x for x in all_fields if not x["ignored"]]
        ignored = [x for x in all_fields if x["ignored"]]
        return {
            "status": "OK",
            "function_id": strip_id_space(node_id),
            "neptune_node_id": node_id,
            "qualified_name": rows[0].get("function_qualified_name", "") if rows else "",
            "field_access_count": len(all_fields if include_ignored else relevant),
            "fields": all_fields if include_ignored else relevant,
            "relevant_field_access_count": len(relevant),
            "ignored_field_access_count": len(ignored),
            "ignored_fields": ignored,
            "all_fields_for_audit": all_fields,
        }

    def find_field_writers(
        self,
        field_id: str,
        exclude_function_id: str | None = None,
        include_possible_writes: bool = True,
        limit: int = 200,
    ) -> dict[str, Any]:
        supplied_field_id = str(field_id or "").strip()
        if not supplied_field_id:
            return {
                "status": "FIELD_NOT_FOUND",
                "input_field_identifier": supplied_field_id,
                "error": "A field ID or qualified field name is required.",
                "writers": [],
            }

        field_resolution = "canonical_id"

        if supplied_field_id.startswith(("DataField:", "field_")):
            field_node = ensure_field_id(supplied_field_id)
        else:
            # First resolve an exact qualified name such as CSettings::m_mode.
            exact_rows = self._execute(
                """
                MATCH (d:DataField)
                WHERE d.qualified_name = $name
                RETURN d.node_id AS field_node_id,
                       d.qualified_name AS field_qualified_name
                LIMIT 2
                """,
                {"name": supplied_field_id},
            )

            if len(exact_rows) > 1:
                return {
                    "status": "AMBIGUOUS_FIELD_NAME",
                    "input_field_identifier": supplied_field_id,
                    "error": "Multiple fields matched the qualified name.",
                    "candidates": exact_rows,
                    "writers": [],
                }

            if exact_rows:
                field_node = ensure_field_id(
                    str(exact_rows[0].get("field_node_id") or "")
                )
                field_resolution = "qualified_name"
            else:
                # Compatibility fallback for schemas where the stored
                # qualified_name is absent or differs from the display name.
                short_name = supplied_field_id.rsplit("::", 1)[-1].strip()

                name_rows = self._execute(
                    """
                    MATCH (d:DataField)
                    WHERE d.field_name = $name
                    RETURN d.node_id AS field_node_id,
                           d.qualified_name AS field_qualified_name,
                           d.field_name AS field_name,
                           d.declaration_relative_path AS declaration_relative_path,
                           d.declaration_line AS declaration_line
                    LIMIT 2
                    """,
                    {"name": short_name},
                )

                if not name_rows:
                    return {
                        "status": "FIELD_NOT_FOUND",
                        "input_field_identifier": supplied_field_id,
                        "error": (
                            "Could not resolve the field identifier to a "
                            "DataField node. No writer search was performed."
                        ),
                        "writers": [],
                    }

                if len(name_rows) > 1:
                    return {
                        "status": "AMBIGUOUS_FIELD_NAME",
                        "input_field_identifier": supplied_field_id,
                        "error": (
                            "The short field name matched multiple DataField "
                            "nodes. Refusing to guess which field was intended."
                        ),
                        "candidates": name_rows,
                        "writers": [],
                    }

                field_node = ensure_field_id(
                    str(name_rows[0].get("field_node_id") or "")
                )
                field_resolution = "field_name"

        accepted = ["WRITES", "READS_WRITES"] + (["POSSIBLE_WRITE"] if include_possible_writes else [])
        exclude = ensure_function_id(exclude_function_id) if exclude_function_id else ""
        rows = self._execute(
            """
            MATCH (w:Function)-[a]->(d:DataField)
            WHERE d.node_id = $field AND type(a) IN $types
              AND ($exclude = '' OR w.node_id <> $exclude)
            RETURN d.node_id AS field_neptune_node_id,
                   d.qualified_name AS field_qualified_name,
                   d.field_name AS field_name, d.field_type AS field_type,
                   w.node_id AS writer_neptune_node_id,
                   w.qualified_name AS writer_qualified_name,
                   w.function_name AS writer_function_name,
                   w.class_name AS writer_class_name,
                   w.signature AS writer_signature,
                   w.relative_path AS writer_relative_path,
                   w.start_line AS writer_start_line,
                   w.end_line AS writer_end_line,
                   w.content_hash AS writer_content_hash,
                   type(a) AS access_type, a.line AS access_line,
                   a.column AS access_column, a.chain AS access_chain
            ORDER BY w.relative_path, a.line, a.column, w.node_id
            LIMIT $limit
            """,
            {"field": field_node, "types": accepted, "exclude": exclude, "limit": max(1, min(int(limit), 500))},
        )
        occurrences = []
        for row in rows:
            item = self._function_item(row, "writer")
            item["call_site"] = {
                "line": row.get("access_line"),
                "column": row.get("access_column"),
                "call_type": row.get("access_type", ""),
                "expression": row.get("access_chain", ""),
                "resolution_status": "FIELD_ACCESS",
            }
            occurrences.append(item)
        unique = _group_by_id(occurrences, "neptune_node_id")
        return {
            "status": "OK",
            "field_id": strip_id_space(field_node),
            "neptune_node_id": field_node,
            "field_name": rows[0].get("field_name", "") if rows else "",
            "qualified_name": rows[0].get("field_qualified_name", "") if rows else "",
            "field_type": rows[0].get("field_type", "") if rows else "",
            "write_occurrence_count": len(occurrences),
            "unique_writer_count": len(unique),
            "writer_count": len(unique),
            "writers": unique,
            "all_write_occurrences_for_audit": occurrences,
            "included_access_types": accepted,
        }
