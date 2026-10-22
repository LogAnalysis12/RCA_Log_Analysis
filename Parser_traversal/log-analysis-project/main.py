from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from fingerprint import cluster_errors
from neptune_anchor import create_client, resolve_cluster
from parse_logs import filter_errors, parse_log_file, write_jsonl


def write_json(path: Path, value) -> None:
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run deterministic DWrapper log-analysis Tasks 29-31: "
            "ERROR parsing, fingerprint clustering, and Neo4j anchor resolution."
        )
    )
    parser.add_argument(
        "--config",
        default="config.json",
        help="Path to the project configuration JSON file.",
    )
    parser.add_argument(
        "--skip-graph", "--skip-neptune", dest="skip_graph",
        action="store_true",
        help="Run Tasks 29 and 30 only without connecting to Neo4j.",
    )
    return parser.parse_args()


def load_config(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(
            f"Configuration file not found: {path}\n"
            "Copy config.example.json to config.json and update local paths and endpoint."
        )
    return json.loads(path.read_text(encoding="utf-8-sig"))


def validate_config(config: dict, config_path: Path, skip_graph: bool) -> dict:
    project_root = config_path.parent.resolve()

    def resolve_local_path(value: str) -> Path:
        candidate = Path(value)
        return candidate.resolve() if candidate.is_absolute() else (project_root / candidate).resolve()

    log_path = resolve_local_path(config["input_log"])
    output_root = resolve_local_path(config.get("output_directory", "./output"))

    if not log_path.exists():
        raise FileNotFoundError(f"Input log not found: {log_path}")

    graph = config.get("graph", {})
    uri = str(graph.get("uri", "")).strip()
    if not skip_graph and not uri.startswith(("bolt://", "neo4j://", "bolt+s://", "neo4j+s://")):
        raise ValueError("Set graph.uri to a valid Neo4j URI before running Task 31.")

    return {
        "log_path": log_path,
        "output_root": output_root,
        "graph": graph,
    }


def run_task29(log_path: Path, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    metadata, all_records, report = parse_log_file(log_path)
    error_records = filter_errors(all_records)
    report["error_record_count"] = len(error_records)

    write_jsonl(output / "parsed_records.jsonl", all_records)
    write_jsonl(output / "errors.jsonl", error_records)
    write_json(output / "run_metadata.json", metadata)
    write_json(output / "parse_report.json", report)

    return {
        "report": report,
        "error_records": error_records,
    }


def run_task30(error_records, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    records = [record.__dict__ for record in error_records]
    clusters = cluster_errors(records)
    report = {
        "input_error_count": len(records),
        "distinct_cluster_count": len(clusters),
        "clustered_occurrence_count": sum(
            cluster["occurrence_count"] for cluster in clusters
        ),
        "all_inputs_are_error": all(
            record.get("level") == "ERROR" for record in records
        ),
    }

    write_json(output / "error_clusters.json", clusters)
    write_json(output / "fingerprint_report.json", report)
    return {"report": report, "clusters": clusters}


def run_task31(clusters, output: Path, settings: dict) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    client = create_client(settings["graph"])
    try:
        engine_status = client.get_engine_status()
        resolved = [resolve_cluster(client, cluster) for cluster in clusters]
    finally:
        client.close()
    status_counts: dict[str, int] = {}
    for item in resolved:
        status = item["anchor_resolution"]["status"]
        status_counts[status] = status_counts.get(status, 0) + 1

    report = {
        "graph_engine_status": engine_status.get("status", "UNKNOWN"),
        "cluster_count": len(clusters),
        "resolution_status_counts": status_counts,
        "all_clusters_accounted_for": len(resolved) == len(clusters),
    }
    write_json(output / "resolved_issues.json", resolved)
    write_json(output / "anchor_report.json", report)
    return {"report": report, "resolved": resolved}


def print_summary(pipeline_report: dict) -> None:
    print("\n========== LOG ANALYSIS PIPELINE ==========")
    task29 = pipeline_report["task29"]
    task30 = pipeline_report["task30"]
    print(f"Parsed records: {task29['parsed_record_count']}")
    print(f"ERROR records: {task29['error_record_count']}")
    print(f"Malformed lines: {task29['malformed_line_count']}")
    print(f"Distinct issue clusters: {task30['distinct_cluster_count']}")

    task31 = pipeline_report.get("task31")
    if task31:
        print(f"Graph status: {task31['graph_engine_status']}")
        print(f"Anchor results: {task31['resolution_status_counts']}")
    else:
        print("Task 31: skipped")
    print(f"Output directory: {pipeline_report['output_directory']}")
    print("===========================================")


def main() -> int:
    args = parse_arguments()
    config_path = Path(args.config).resolve()

    try:
        config = load_config(config_path)
        settings = validate_config(config, config_path, args.skip_graph)
        output_root = settings["output_root"]
        output_root.mkdir(parents=True, exist_ok=True)

        print("[1/3] Task 29: Parsing real log and selecting exact ERROR records...")
        task29 = run_task29(
            settings["log_path"],
            output_root / "task29_parsing",
        )

        print("[2/3] Task 30: Normalizing, fingerprinting, and clustering errors...")
        task30 = run_task30(
            task29["error_records"],
            output_root / "task30_fingerprints",
        )

        pipeline_report = {
            "task29": task29["report"],
            "task30": task30["report"],
            "task31": None,
            "output_directory": str(output_root),
        }

        if args.skip_graph:
            print("[3/3] Task 31: Skipped by command-line option.")
        else:
            print("[3/3] Task 31: Resolving issue clusters to Neo4j anchors...")
            task31 = run_task31(
                task30["clusters"],
                output_root / "task31_anchors",
                settings,
            )
            pipeline_report["task31"] = task31["report"]

        write_json(output_root / "pipeline_report.json", pipeline_report)
        print_summary(pipeline_report)
        return 0

    except Exception as error:
        print(f"\nPIPELINE FAILED: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
