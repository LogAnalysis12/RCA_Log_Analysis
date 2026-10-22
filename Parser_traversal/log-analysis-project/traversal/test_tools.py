from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from traversal.neptune_tools import NeptuneTraversalTools
from traversal.graph_client import create_graph_client


def load_json(path: Path) -> Any:
    return json.loads(
        path.read_text(
            encoding="utf-8-sig",
        )
    )


def resolve_path(
    root: Path,
    value: str,
) -> Path:
    path = Path(value).expanduser()

    if path.is_absolute():
        return path.resolve()

    return (root / path).resolve()


def selected_issue(
    issues: list[dict[str, Any]],
) -> dict[str, Any]:
    for issue in issues:
        resolution = issue.get(
            "anchor_resolution",
            {},
        )

        if (
            resolution.get("status") == "RESOLVED"
            and resolution.get("selected_function")
        ):
            return issue

    raise RuntimeError(
        "No issue with a RESOLVED graph anchor was found."
    )


def item_name(
    item: dict[str, Any],
) -> str:
    return str(
        item.get("qualified_name")
        or item.get("field_name")
        or item.get("function_name")
        or item.get("symbol_name")
        or ""
    )


def validate_aggregated_results(
    items: list[dict[str, Any]],
    expected_unique_count: int,
    expected_occurrence_count: int,
) -> dict[str, bool]:
    """
    Validate a deduplicated collection.

    Each unique item must appear once, while all original call sites remain
    nested inside the item's call_sites collection.
    """
    unique_count_matches = (
        expected_unique_count == len(items)
    )

    occurrence_count_matches = (
        expected_occurrence_count
        == sum(
            int(
                item.get(
                    "call_occurrence_count",
                    0,
                )
            )
            for item in items
        )
    )

    call_sites_preserved = all(
        int(
            item.get(
                "call_occurrence_count",
                0,
            )
        )
        == len(
            item.get(
                "call_sites",
                [],
            )
        )
        for item in items
    )

    return {
        "unique_count_matches": (
            unique_count_matches
        ),
        "occurrence_count_matches": (
            occurrence_count_matches
        ),
        "call_sites_preserved": (
            call_sites_preserved
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run generic deterministic validation for "
            "Neo4j traversal and local C++ source tools."
        )
    )

    parser.add_argument(
        "--config",
        default="./config.json",
    )

    parser.add_argument(
        "--issues",
        default=(
            "./output/task31_anchors/"
            "resolved_issues.json"
        ),
    )

    parser.add_argument(
        "--output",
        default=(
            "./output/"
            "traversal_tool_validation_v3.json"
        ),
    )

    args = parser.parse_args()

    try:
        config_path = Path(
            args.config
        ).resolve()

        project_root = config_path.parent

        config = load_json(
            config_path
        )

        graph_config = config["graph"]

        traversal_config = config.get(
            "traversal",
            {},
        )

        graph_client = create_graph_client(graph_config)
        tools = NeptuneTraversalTools(
            graph_client=graph_client,
            source_root=resolve_path(project_root, config["source_root"]),
            ignored_fields=traversal_config.get("ignored_fields", []),
            ignored_project_callees=traversal_config.get("ignored_project_callees", []),
            ignored_external_prefixes=traversal_config.get("ignored_external_prefixes", []),
        )

        issues_path = resolve_path(
            project_root,
            args.issues,
        )

        issues = load_json(
            issues_path
        )

        if not isinstance(issues, list):
            raise ValueError(
                "The resolved issues file must "
                "contain a JSON array."
            )

        issue = selected_issue(
            issues
        )

        anchor = issue[
            "anchor_resolution"
        ]["selected_function"]

        function_id = anchor[
            "function_id"
        ]

        qualified_name = anchor.get(
            "qualified_name",
            "",
        )

        engine = tools.get_engine_status()

        source = tools.read_function_source(
            function_id
        )

        callers = tools.find_callers(
            function_id,
            limit=int(
                traversal_config.get(
                    "max_callers",
                    30,
                )
            ),
        )

        callees = tools.find_callees(
            function_id,
            limit=int(
                traversal_config.get(
                    "max_callees",
                    40,
                )
            ),
        )

        fields = tools.find_fields_read(
            function_id,
            include_ignored=False,
        )

        caller_items = callers.get(
            "callers",
            [],
        )

        all_project_callees = callees.get(
            "all_project_callees_for_audit",
            [],
        )

        all_external_callees = callees.get(
            "all_external_callees_for_audit",
            [],
        )

        visible_project_callees = callees.get(
            "project_callees",
            [],
        )

        visible_external_callees = callees.get(
            "external_callees",
            [],
        )

        ignored_project_callees = callees.get(
            "ignored_project_callees",
            [],
        )

        ignored_external_callees = callees.get(
            "ignored_external_callees",
            [],
        )

        visible_fields = fields.get(
            "fields",
            [],
        )

        ignored_fields = fields.get(
            "ignored_fields",
            [],
        )

        configured_ignored_fields = {
            str(value)
            for value in traversal_config.get(
                "ignored_fields",
                [],
            )
        }

        configured_ignored_project = {
            str(value)
            for value in traversal_config.get(
                "ignored_project_callees",
                [],
            )
        }

        configured_ignored_external = tuple(
            str(value)
            for value in traversal_config.get(
                "ignored_external_prefixes",
                [],
            )
        )

        caller_validation = (
            validate_aggregated_results(
                items=caller_items,
                expected_unique_count=int(
                    callers.get(
                        "unique_caller_count",
                        0,
                    )
                ),
                expected_occurrence_count=int(
                    callers.get(
                        "call_occurrence_count",
                        0,
                    )
                ),
            )
        )

        project_validation = (
            validate_aggregated_results(
                items=all_project_callees,
                expected_unique_count=int(
                    callees.get(
                        "unique_project_callee_count",
                        0,
                    )
                ),
                expected_occurrence_count=int(
                    callees.get(
                        "project_call_occurrence_count",
                        0,
                    )
                ),
            )
        )

        external_validation = (
            validate_aggregated_results(
                items=all_external_callees,
                expected_unique_count=int(
                    callees.get(
                        "unique_external_symbol_count",
                        0,
                    )
                ),
                expected_occurrence_count=int(
                    callees.get(
                        "external_call_occurrence_count",
                        0,
                    )
                ),
            )
        )

        visible_project_names = {
            item_name(item)
            for item in visible_project_callees
        }

        visible_external_names = {
            item_name(item)
            for item in visible_external_callees
        }

        visible_field_names = {
            item_name(item)
            for item in visible_fields
        }

        project_noise_remaining = sorted(
            name
            for name in visible_project_names
            if name in configured_ignored_project
        )

        external_noise_remaining = sorted(
            name
            for name in visible_external_names
            if configured_ignored_external
            and name.startswith(
                configured_ignored_external
            )
        )

        logging_fields_remaining = sorted(
            name
            for name in visible_field_names
            if name in configured_ignored_fields
        )

        project_partition_valid = (
            len(
                visible_project_callees
            )
            + len(
                ignored_project_callees
            )
            == len(
                all_project_callees
            )
        )

        external_partition_valid = (
            len(
                visible_external_callees
            )
            + len(
                ignored_external_callees
            )
            == len(
                all_external_callees
            )
        )

        relevant_field_access_count = int(
            fields.get(
                "relevant_field_access_count",
                fields.get(
                    "field_access_count",
                    len(visible_fields),
                ),
            )
        )

        ignored_field_access_count = int(
            fields.get(
                "ignored_field_access_count",
                len(ignored_fields),
            )
        )

        all_call_sites_preserved = all(
            [
                caller_validation[
                    "call_sites_preserved"
                ],
                project_validation[
                    "call_sites_preserved"
                ],
                external_validation[
                    "call_sites_preserved"
                ],
            ]
        )

        validation = {
            "graph_healthy": (
                engine.get("status")
                == "healthy"
            ),
            "source_verified": (
                source.get("status")
                == "SOURCE_VERIFIED"
            ),
            "callers_query_passed": (
                callers.get("status")
                == "OK"
            ),
            "callees_query_passed": (
                callees.get("status")
                == "OK"
            ),
            "fields_query_passed": (
                fields.get("status")
                == "OK"
            ),
            "callers_deduplicated": all(
                [
                    caller_validation[
                        "unique_count_matches"
                    ],
                    caller_validation[
                        "occurrence_count_matches"
                    ],
                ]
            ),
            "project_callees_deduplicated": all(
                [
                    project_validation[
                        "unique_count_matches"
                    ],
                    project_validation[
                        "occurrence_count_matches"
                    ],
                ]
            ),
            "external_callees_deduplicated": all(
                [
                    external_validation[
                        "unique_count_matches"
                    ],
                    external_validation[
                        "occurrence_count_matches"
                    ],
                ]
            ),
            "all_call_sites_preserved": (
                all_call_sites_preserved
            ),
            "project_filter_partition_valid": (
                project_partition_valid
            ),
            "external_filter_partition_valid": (
                external_partition_valid
            ),
            "project_noise_filtered": (
                len(
                    project_noise_remaining
                )
                == 0
            ),
            "external_noise_filtered": (
                len(
                    external_noise_remaining
                )
                == 0
            ),
            "logging_fields_filtered": (
                len(
                    logging_fields_remaining
                )
                == 0
            ),
        }

        external_names = [
            item_name(item)
            for item in visible_external_callees
        ]

        mcd_boundary_present = any(
            name.startswith(
                "asam::mcd::MCDLogicalLink"
            )
            for name in external_names
        )

        anchor_is_golden_case_01 = (
            qualified_name
            == (
                "CDServerCommunication::"
                "CreateDiagComPrimitiveByName"
            )
        )

        if relevant_field_access_count > 0:
            recommended_route = (
                "INVESTIGATOR_DECIDES_FROM_SOURCE"
            )
        elif int(
            callers.get(
                "unique_caller_count",
                0,
            )
        ) > 0:
            recommended_route = (
                "FIND_CALLERS"
            )
        else:
            recommended_route = (
                "INVESTIGATOR_DECIDES_FROM_SOURCE"
            )

        scenario_observations = {
            "anchor_function_id": (
                function_id
            ),
            "anchor_qualified_name": (
                qualified_name
            ),
            "anchor_relative_path": (
                anchor.get(
                    "relative_path",
                    "",
                )
            ),
            "anchor_start_line":(
                anchor.get("start_line")
            ),
            "anchor_end_line": (
                anchor.get("end_line")
            ),
            "external_boundary_present": (
                bool(
                    callees.get(
                        "external_boundary_present",
                        False,
                    )
                )
            ),
            "relevant_external_boundary_count": (
                len(
                    visible_external_callees
                )
            ),
            "mcd_logical_link_boundary_present": (
                mcd_boundary_present
            ),
            "mcd_boundary_check_applicable": (
                anchor_is_golden_case_01
            ),
            "relevant_field_access_count": (
                relevant_field_access_count
            ),
            "ignored_field_access_count": (
                ignored_field_access_count
            ),
            "configured_logging_fields_present": (
                sorted(
                    {
                        item_name(item)
                        for item in ignored_fields
                        if item_name(item)
                        in configured_ignored_fields
                    }
                )
            ),
            "recommended_initial_route": (
                recommended_route
            ),
        }

        summary = {
            "caller_occurrences": int(
                callers.get(
                    "call_occurrence_count",
                    0,
                )
            ),
            "unique_callers": int(
                callers.get(
                    "unique_caller_count",
                    0,
                )
            ),
            "project_call_occurrences": int(
                callees.get(
                    "project_call_occurrence_count",
                    0,
                )
            ),
            "unique_project_callees": int(
                callees.get(
                    "unique_project_callee_count",
                    0,
                )
            ),
            "relevant_project_callees": int(
                callees.get(
                    "relevant_project_callee_count",
                    len(
                        visible_project_callees
                    ),
                )
            ),
            "ignored_project_callees": (
                len(
                    ignored_project_callees
                )
            ),
            "external_call_occurrences": int(
                callees.get(
                    "external_call_occurrence_count",
                    0,
                )
            ),
            "unique_external_symbols": int(
                callees.get(
                    "unique_external_symbol_count",
                    0,
                )
            ),
            "relevant_external_callees": int(
                callees.get(
                    "relevant_external_callee_count",
                    len(
                        visible_external_callees
                    ),
                )
            ),
            "ignored_external_callees": (
                len(
                    ignored_external_callees
                )
            ),
            "relevant_field_accesses": (
                relevant_field_access_count
            ),
            "ignored_field_accesses": (
                ignored_field_access_count
            ),
            "recommended_initial_route": (
                recommended_route
            ),
        }

        report = {
            "validation": validation,
            "scenario_observations": (
                scenario_observations
            ),
            "summary": summary,
            "anchor": anchor,
            "engine": engine,
            "source_result": source,
            "callers": callers,
            "callees": callees,
            "investigator_fields": fields,
            "filter_diagnostics": {
                "project_noise_remaining": (
                    project_noise_remaining
                ),
                "external_noise_remaining": (
                    external_noise_remaining
                ),
                "logging_fields_remaining": (
                    logging_fields_remaining
                ),
            },
        }

        output_path = resolve_path(
            project_root,
            args.output,
        )

        output_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        output_path.write_text(
            json.dumps(
                report,
                indent=2,
                ensure_ascii=False,
                default=str,
            ),
            encoding="utf-8",
        )

        print("")
        print(
            "========== GENERIC TRAVERSAL TOOL VALIDATION =========="
        )

        for key, value in validation.items():
            print(
                f"{key}: {value}"
            )

        print("")
        print(
            "---------- SCENARIO OBSERVATIONS ----------"
        )

        for key, value in (
            scenario_observations.items()
        ):
            print(
                f"{key}: {value}"
            )

        print("")
        print(
            "---------- SUMMARY ----------"
        )

        for key, value in summary.items():
            print(
                f"{key}: {value}"
            )

        print(
            f"Report: {output_path}"
        )

        print(
            "======================================================="
        )

        graph_client.close()
        return (
            0
            if all(validation.values())
            else 1
        )

    except Exception as error:
        if "graph_client" in locals():
            graph_client.close()
        print(
            f"TRAVERSAL TOOL TEST FAILED: {error}",
            file=sys.stderr,
        )

        return 1


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
