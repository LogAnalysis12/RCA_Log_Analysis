from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

UUID = re.compile(r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b")
WINDOWS_PATH = re.compile(r"\b[A-Za-z]:\\[^\s|]+")
HEX = re.compile(r"\b0x[0-9a-fA-F]+\b")
QUOTED = re.compile(r"(['\"])(?:\\.|(?!\1).)*\1")
POINTER = re.compile(r"\b[0-9a-fA-F]{12,16}\b")
INTEGER = re.compile(r"(?<![A-Za-z_])[-+]?\d+(?![A-Za-z_])")
WHITESPACE = re.compile(r"\s+")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records = []
    with path.open("r", encoding="utf-8-sig") as source:
        for line in source:
            if line.strip():
                records.append(json.loads(line))
    return records


def normalize_message(message: str) -> str:
    value = message.strip()
    value = UUID.sub("<UUID>", value)
    value = WINDOWS_PATH.sub("<PATH>", value)
    value = HEX.sub("<HEX>", value)
    value = POINTER.sub("<POINTER>", value)
    value = QUOTED.sub("<VALUE>", value)
    value = INTEGER.sub("<N>", value)
    value = WHITESPACE.sub(" ", value)
    return value.strip()


def make_fingerprint(record: dict[str, Any]) -> tuple[str, str]:
    qualified_function = record.get("qualified_function") or "<UNRESOLVED_FUNCTION>"
    normalized_message = normalize_message(record.get("message") or "")
    return qualified_function, normalized_message


def fingerprint_id(key: tuple[str, str]) -> str:
    digest = hashlib.sha256("|".join(key).encode("utf-8")).hexdigest()
    return f"fp_{digest[:24]}"


def cluster_errors(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        if record.get("level") != "ERROR":
            raise ValueError("Task 30 received a non-ERROR record.")
        groups[make_fingerprint(record)].append(record)

    clusters = []
    for key, occurrences in groups.items():
        representative = occurrences[0]
        clusters.append(
            {
                "fingerprint_id": fingerprint_id(key),
                "qualified_function": key[0],
                "function_name": representative.get("function_name") or "",
                "class_scope": representative.get("class_scope") or "",
                "normalized_message": key[1],
                "occurrence_count": len(occurrences),
                "first_timestamp": occurrences[0].get("timestamp_iso"),
                "last_timestamp": occurrences[-1].get("timestamp_iso"),
                "representative_error": representative,
                "occurrences": occurrences,
            }
        )

    return sorted(
        clusters,
        key=lambda item: (-item["occurrence_count"], item["qualified_function"], item["normalized_message"]),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Normalize, fingerprint, and cluster ERROR records.")
    parser.add_argument("--errors", required=True)
    parser.add_argument("--output-directory", required=True)
    args = parser.parse_args()

    output = Path(args.output_directory).resolve()
    output.mkdir(parents=True, exist_ok=True)
    records = read_jsonl(Path(args.errors))
    clusters = cluster_errors(records)

    (output / "error_clusters.json").write_text(
        json.dumps(clusters, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    report = {
        "input_error_count": len(records),
        "distinct_cluster_count": len(clusters),
        "clustered_occurrence_count": sum(c["occurrence_count"] for c in clusters),
        "all_inputs_are_error": all(r.get("level") == "ERROR" for r in records),
    }
    (output / "fingerprint_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("========== TASK 30: FINGERPRINTING ==========")
    print(f"ERROR records: {len(records)}")
    print(f"Distinct issues: {len(clusters)}")
    for cluster in clusters:
        print(
            f"[{cluster['occurrence_count']}x] {cluster['fingerprint_id']} | "
            f"{cluster['qualified_function']} | {cluster['normalized_message']}"
        )
    print("=============================================")


if __name__ == "__main__":
    main()
