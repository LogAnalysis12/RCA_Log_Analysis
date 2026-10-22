#!/usr/bin/env python3
"""Validate that S7-S13 ground truth snippets match source lines exactly."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--ground-truth", type=Path, default=None)
    args = parser.parse_args()
    repo_root = args.repo_root.resolve()
    gt_path = (args.ground_truth or (repo_root / "docs" / "ground_truth_s7_s13.json")).resolve()
    src_root = (repo_root / "src").resolve()

    if inside(gt_path, src_root):
        print(f"ERROR: ground-truth file must be outside source_root=src: {gt_path}")
        return 2
    if not gt_path.is_file():
        print(f"ERROR: ground-truth file not found: {gt_path}")
        return 2

    try:
        data = json.loads(gt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: cannot read valid JSON: {exc}")
        return 2

    if data.get("source_root") != "src":
        print("ERROR: source_root must be exactly 'src'")
        return 2

    scenarios = data.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        print("ERROR: scenarios must be a non-empty list")
        return 2

    seen = set()
    failures = []
    checked = 0
    for scenario in scenarios:
        sid = scenario.get("scenario_id", "<missing>")
        if sid in seen:
            failures.append(f"{sid}: duplicate scenario_id")
        seen.add(sid)
        required = ["log_anchor", "root_cause_function", "variable_or_resource", "thread_entry_points",
                    "propagation_path", "explanation", "required_evidence", "decoy",
                    "file", "line_start", "line_end", "exact_source_snippet"]
        missing = [k for k in required if k not in scenario]
        if missing:
            failures.append(f"{sid}: missing fields {missing}")
            continue
        sites = scenario.get("root_cause_sites") or [{
            "file": scenario["file"], "line_start": scenario["line_start"],
            "line_end": scenario["line_end"], "exact_source_snippet": scenario["exact_source_snippet"]
        }]
        primary = sites[0]
        for key in ("file", "line_start", "line_end", "exact_source_snippet"):
            if scenario.get(key) != primary.get(key):
                failures.append(f"{sid}: top-level {key} does not match first root_cause_site")
        for site_index, site in enumerate(sites):
            rel = Path(site.get("file", ""))
            source_path = (src_root / rel).resolve()
            label = f"{sid} site {site_index + 1} ({rel}:{site.get('line_start')}-{site.get('line_end')})"
            if not inside(source_path, src_root):
                failures.append(f"{label}: path escapes source_root")
                continue
            if not source_path.is_file():
                failures.append(f"{label}: source file does not exist")
                continue
            try:
                lines = source_path.read_text(encoding="utf-8").splitlines()
                start = int(site["line_start"])
                end = int(site["line_end"])
                if start < 1 or end < start or end > len(lines):
                    failures.append(f"{label}: invalid source line range")
                    continue
                actual = "\n".join(lines[start - 1:end])
                expected = site.get("exact_source_snippet", "")
                if actual != expected:
                    failures.append(f"{label}: exact source snippet mismatch")
                    continue
                checked += 1
            except (KeyError, TypeError, ValueError, OSError) as exc:
                failures.append(f"{label}: invalid site data ({exc})")

    if failures:
        for failure in failures:
            print("FAIL:", failure)
        print(f"Ground truth validation failed: {len(failures)} issue(s), {checked} source sites checked.")
        return 1

    expected_ids = {f"S{i}" for i in range(7, 14)}
    if seen != expected_ids:
        print(f"ERROR: expected scenarios S7-S13 exactly; found {sorted(seen)}")
        return 1
    print(f"Ground truth validation passed: {len(seen)} scenarios, {checked} exact source sites checked.")
    print(f"Ground-truth file is outside source_root: {gt_path.relative_to(repo_root)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
