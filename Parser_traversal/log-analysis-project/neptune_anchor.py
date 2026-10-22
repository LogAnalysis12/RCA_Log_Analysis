from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from traversal.graph_client import create_graph_client


def load_clusters(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def create_client(graph_config: dict[str, Any]):
    return create_graph_client(graph_config)


def exact_candidates(client, qualified_name: str) -> list[dict[str, Any]]:
    return client.query("""
            MATCH (f:Function)
            WHERE f.qualified_name = $qualifiedName
            RETURN f.node_id AS function_id,
                   f.qualified_name AS qualified_name,
                   f.function_name AS function_name,
                   f.class_name AS class_name,
                   f.signature AS signature,
                   f.relative_path AS relative_path,
                   f.start_line AS start_line,
                   f.end_line AS end_line,
                   f.content_hash AS content_hash
            ORDER BY f.relative_path, f.start_line, f.node_id
        """, {"qualifiedName": qualified_name})


def function_name_candidates(client, function_name: str) -> list[dict[str, Any]]:
    return client.query("""
            MATCH (f:Function)
            WHERE f.function_name = $functionName
            RETURN f.node_id AS function_id,
                   f.qualified_name AS qualified_name,
                   f.function_name AS function_name,
                   f.class_name AS class_name,
                   f.signature AS signature,
                   f.relative_path AS relative_path,
                   f.start_line AS start_line,
                   f.end_line AS end_line,
                   f.content_hash AS content_hash
            ORDER BY f.qualified_name, f.relative_path, f.start_line, f.node_id
        """, {"functionName": function_name})


def resolve_cluster(client, cluster: dict[str, Any]) -> dict[str, Any]:
    qualified_name = cluster.get("qualified_function") or ""
    function_name = cluster.get("function_name") or (qualified_name.split("::")[-1] if qualified_name else "")

    exact = exact_candidates(client, qualified_name) if qualified_name else []
    if len(exact) == 1:
        status, method, candidates = "RESOLVED", "EXACT_QUALIFIED_NAME", exact
    elif len(exact) > 1:
        status, method, candidates = "AMBIGUOUS", "EXACT_QUALIFIED_NAME", exact
    else:
        fallback = function_name_candidates(client, function_name) if function_name else []
        if len(fallback) == 1:
            status, method, candidates = "RESOLVED", "UNIQUE_FUNCTION_NAME_FALLBACK", fallback
        elif len(fallback) > 1:
            status, method, candidates = "AMBIGUOUS", "FUNCTION_NAME_FALLBACK", fallback
        else:
            status, method, candidates = "UNRESOLVED", "NO_MATCH", []

    return {
        "fingerprint_id": cluster["fingerprint_id"],
        "occurrence_count": cluster["occurrence_count"],
        "qualified_function": qualified_name,
        "normalized_message": cluster["normalized_message"],
        "representative_error": cluster["representative_error"],
        "anchor_resolution": {
            "status": status,
            "match_method": method,
            "candidate_count": len(candidates),
            "selected_function": candidates[0] if status == "RESOLVED" else None,
            "candidates": candidates,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Resolve clustered log errors to Neo4j Function nodes.")
    parser.add_argument("--clusters", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output-directory", required=True)
    args = parser.parse_args()

    output = Path(args.output_directory).resolve()
    output.mkdir(parents=True, exist_ok=True)
    clusters = load_clusters(Path(args.clusters))
    config = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
    client = create_client(config["graph"])
    try:
        resolved = [resolve_cluster(client, cluster) for cluster in clusters]
    finally:
        client.close()

    (output / "resolved_issues.json").write_text(
        json.dumps(resolved, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    status_counts: dict[str, int] = {}
    for item in resolved:
        status = item["anchor_resolution"]["status"]
        status_counts[status] = status_counts.get(status, 0) + 1
    report = {
        "cluster_count": len(clusters),
        "resolution_status_counts": status_counts,
        "all_clusters_accounted_for": len(resolved) == len(clusters),
    }
    (output / "anchor_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("========== TASK 31: NEPTUNE ANCHORS ==========")
    for item in resolved:
        anchor = item["anchor_resolution"]
        print(
            f"{item['fingerprint_id']} | {anchor['status']} | "
            f"{anchor['match_method']} | candidates={anchor['candidate_count']} | "
            f"{item['qualified_function']}"
        )
    print(f"Summary: {status_counts}")
    print("==============================================")


if __name__ == "__main__":
    main()
