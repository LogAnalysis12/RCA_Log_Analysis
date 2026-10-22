from __future__ import annotations

import argparse
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from traversal.backend_factory import make_backend_factory
from traversal.budget import BudgetManager
from traversal.investigator import run_investigation
from traversal.neptune_tools import NeptuneTraversalTools
from traversal.graph_client import create_graph_client
from traversal.llm_trace import LLMTraceRecorder
from traversal.rca_report import generate_human_report


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


def select_issue(
    issues: list[dict[str, Any]],
    fingerprint_id: str | None,
) -> dict[str, Any]:
    if fingerprint_id:
        matching_issues = [
            issue
            for issue in issues
            if issue.get("fingerprint_id")
            == fingerprint_id
        ]

        if not matching_issues:
            available_ids = [
                issue.get("fingerprint_id")
                for issue in issues
            ]

            raise ValueError(
                "Fingerprint ID was not found: "
                f"{fingerprint_id}. "
                "Available fingerprint IDs: "
                f"{available_ids}"
            )

        issue = matching_issues[0]

        resolution = issue.get(
            "anchor_resolution",
            {},
        )

        if resolution.get("status") != "RESOLVED":
            raise ValueError(
                "The selected issue cannot be traversed "
                "because its anchor status is "
                f"{resolution.get('status', 'UNKNOWN')}. "
                f"Fingerprint ID: {fingerprint_id}"
            )

        if not resolution.get("selected_function"):
            raise ValueError(
                "The selected resolved issue does not "
                "contain selected_function metadata."
            )

        return issue

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

    raise ValueError(
        "No issue with a RESOLVED anchor was found."
    )


def exit_code_for_status(
    status: str,
) -> int:
    if status == "ACCEPTED":
        return 0

    if status in {
        "NOT_ACCEPTED",
        "STALLED",
        "MAX_STEPS_REACHED",
        "BUDGET_EXCEEDED",
    }:
        return 2

    return 1


def _generate_reports_after_result(
    result: dict[str, Any],
    result_path: Path,
    trace_directory: Path | None,
) -> Path | None:
    trace_summary = None
    if trace_directory is not None:
        summary_path = trace_directory / "llm_trace_summary.json"
        try:
            trace_summary = load_json(summary_path)
        except (OSError, json.JSONDecodeError):
            trace_summary = None

    report_path = result_path.with_name(f"{result_path.stem}_report.md")
    report_written = False
    try:
        generate_human_report(
            result,
            report_path,
            result_path=result_path,
            trace_directory=trace_directory,
            trace_summary=trace_summary,
        )
        print(f"Human-readable report: {report_path}")
        report_written = True
    except Exception as error:
        print(f"WARNING: RCA JSON was saved, but could not generate RCA Markdown report at {report_path}: {error}")

    if trace_directory is not None:
        trace_report_path = trace_directory / report_path.name
        try:
            generate_human_report(
                result,
                trace_report_path,
                result_path=trace_directory / "rca_result.json",
                trace_directory=trace_directory,
                trace_summary=trace_summary,
            )
            print(f"Run-local RCA report: {trace_report_path}")
        except Exception as error:
            print(f"WARNING: RCA JSON was saved, but could not generate run-local RCA Markdown report at {trace_report_path}: {error}")
    return report_path if report_written else None


def generate_reports_safely(
    result_path: str | Path,
    trace_directory: str | Path | None = None,
) -> Path | None:
    """Generate adjacent and run-local reports without affecting the saved RCA JSON."""
    result_file = Path(result_path)
    try:
        result = load_json(result_file)
        if not isinstance(result, dict):
            raise ValueError("RCA result JSON must contain an object.")
        return _generate_reports_after_result(
            result,
            result_file,
            Path(trace_directory) if trace_directory is not None else None,
        )
    except Exception as error:
        print(f"WARNING: RCA JSON was saved, but Markdown report generation failed for {result_file}: {error}")
        return None


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run root-cause traversal for one "
            "resolved error cluster."
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
            "./output/investigation_result.json"
        ),
    )

    parser.add_argument(
        "--fingerprint-id",
        default=None,
        help=(
            "Fingerprint ID of the resolved cluster "
            "to investigate. If omitted, the first "
            "resolved cluster is selected."
        ),
    )

    return parser.parse_args()


def main() -> int:
    args = parse_arguments()

    started_at = datetime.now(
        timezone.utc
    )

    started_clock = time.perf_counter()

    config_path = Path(
        args.config
    ).resolve()

    project_root = config_path.parent

    output_path = resolve_path(
        project_root,
        args.output,
    )

    budget_manager: BudgetManager | None = None
    trace_recorder: LLMTraceRecorder | None = None
    trace_directory: Path | None = None

    selected_fingerprint_id = (
        args.fingerprint_id
    )

    try:
        config = load_json(
            config_path
        )

        graph_config = config["graph"]

        llm_config = config[
            "llm"
        ]

        traversal_config = config.get(
            "traversal",
            {},
        )

        if config.get("observability", {}).get("llm_trace", False):
            run_id = uuid.uuid4().hex
            run_name = (
                f"{output_path.stem}_"
                f"{started_at.strftime('%Y%m%dT%H%M%SZ')}_"
                f"{run_id[:8]}"
            )
            trace_directory = output_path.parent / run_name
            trace_recorder = LLMTraceRecorder(
                trace_directory,
                run_id=run_id,
                provider=str(llm_config.get("provider", "unknown")),
                model=str(llm_config.get("model", "unknown")),
                base_url=str(llm_config.get("base_url", "")),
                pricing=traversal_config.get("pricing", {}),
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

        issue = select_issue(
            issues=issues,
            fingerprint_id=(
                selected_fingerprint_id
            ),
        )

        selected_fingerprint_id = (
            issue.get("fingerprint_id")
        )

        resolution = issue[
            "anchor_resolution"
        ]

        selected_function = resolution[
            "selected_function"
        ]

        source_root = resolve_path(
            project_root,
            config["source_root"],
        )

        if not source_root.exists():
            raise FileNotFoundError(
                "Configured source_root does "
                f"not exist: {source_root}"
            )

        graph_client = create_graph_client(graph_config)
        tools = NeptuneTraversalTools(
            graph_client=graph_client,
            source_root=source_root,
            ignored_fields=traversal_config.get("ignored_fields", []),
            ignored_project_callees=traversal_config.get("ignored_project_callees", []),
            ignored_external_prefixes=traversal_config.get("ignored_external_prefixes", []),
        )

        budget_manager = BudgetManager(
            traversal_config
        )

        traversal_config.setdefault("repository_id", str(config.get("repository_id") or source_root.name))
        backend_llm_config = dict(llm_config)
        backend_llm_config["history_config"] = {
            "enabled": traversal_config.get("conversation_history_compaction_enabled", True),
            "max_chars": int(traversal_config.get("conversation_history_max_chars", 24000)),
            "keep_recent_cycles": int(traversal_config.get("conversation_history_keep_recent_cycles", 4)),
        }
        backend_factory = (
            make_backend_factory(
                backend_llm_config,
                budget_manager,
                trace_recorder,
            )
        )

        print("")
        print(
            "========== SELECTED ISSUE =========="
        )
        print(
            "Fingerprint ID:",
            selected_fingerprint_id,
        )
        print(
            "Occurrences:",
            issue.get(
                "occurrence_count",
                0,
            ),
        )
        print(
            "Logged function:",
            issue.get(
                "qualified_function",
                "",
            ),
        )
        print(
            "Error message:",
            issue.get(
                "normalized_message",
                "",
            ),
        )
        print(
            "Resolved function:",
            selected_function.get(
                "qualified_name",
                "",
            ),
        )
        print(
            "Source:",
            selected_function.get(
                "relative_path",
                "",
            ),
        )
        print(
            "===================================="
        )
        print("")

        result = run_investigation(
            issue=issue,
            tools=tools,
            backend_factory=backend_factory,
            max_steps=int(
                traversal_config.get(
                    "max_steps",
                    16,
                )
            ),
            max_critic_rounds=int(
                traversal_config.get(
                    "max_critic_rounds",
                    3,
                )
            ),
            stall_retries=int(
                traversal_config.get(
                    "stall_retries",
                    1,
                )
            ),
            repeated_tool_limit=int(
                traversal_config.get(
                    "repeated_tool_limit",
                    1,
                )
            ),
            runtime_config=(
                traversal_config
            ),
        )

        result["selected_issue"] = {
            "fingerprint_id": (
                selected_fingerprint_id
            ),
            "occurrence_count": (
                issue.get(
                    "occurrence_count",
                    0,
                )
            ),
            "qualified_function": (
                issue.get(
                    "qualified_function",
                    ""
                )
            ),
            "normalized_message": (
                issue.get(
                    "normalized_message",
                    "",
                )
            ),
            "resolved_function": (
                selected_function.get(
                    "qualified_name",
                    "",
                )
            ),
            "relative_path": (
                selected_function.get(
                    "relative_path",
                    "",
                )
            ),
        }

    except Exception as error:
        result = {
            "status": "RUNTIME_FAILED",
            "error_type": (
                type(error).__name__
            ),
            "error_message": str(error),
            "selected_issue": {
                "fingerprint_id": (
                    selected_fingerprint_id
                ),
            },
        }

    if "graph_client" in locals():
        graph_client.close()

    completed_at = datetime.now(
        timezone.utc
    )

    execution_metrics = {
        "started_at_utc": (
            started_at.isoformat()
       ),
        "completed_at_utc": (
            completed_at.isoformat()
        ),
        "elapsed_seconds": round(
            time.perf_counter()
            - started_clock,
            3,
        ),
    }

    if budget_manager is not None:
        execution_metrics.update(
            budget_manager.to_dict()
        )

    result["execution_metrics"] = (
        execution_metrics
    )

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    output_path.write_text(
        json.dumps(
            result,
            indent=2,
            ensure_ascii=False,
            default=str,
        ),
        encoding="utf-8",
    )

    if trace_recorder is not None and trace_directory is not None:
        trace_recorder.write_result_copy(result)
        trace_recorder.write_summary()
        trace_recorder.close()

    generate_reports_safely(
        output_path,
        trace_directory,
    )

    console_result = {
        "status": result.get(
            "status"
        ),
        "fingerprint_id": (
            selected_fingerprint_id
        ),
        **execution_metrics,
        "output": str(
            output_path
        ),
    }
    if trace_directory is not None:
        console_result["llm_trace_directory"] = str(trace_directory)

    print(
        json.dumps(
            console_result,
            indent=2,
            ensure_ascii=False,
        )
    )

    return exit_code_for_status(
        result.get(
            "status",
            "UNKNOWN",
        )
    )


if __name__ == "__main__":
    raise SystemExit(
        main()
    )