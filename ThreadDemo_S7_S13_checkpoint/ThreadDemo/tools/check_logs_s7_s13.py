#!/usr/bin/env python3
"""Validate opt-in S7-S13 error anchors without source @GT markers."""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
import sys

LOG_LINE = re.compile(
    r"^(?P<timestamp>\d{2}-\d{2}-\d{4}_\d{2}:\d{2}:\d{2}\.\d{3})\s*\|"
    r"\s*(?P<perf_timer>[^|]+?)\s*\|"
    r"\s*(?P<thread_id>[^|]+?)\s*\|"
    r"\s*(?P<level>[^|]+?)\s*\|"
    r"\s*(?P<payload>.*)$"
)
FUNCTION_SPLIT = re.compile(r"^([A-Za-z_]\w*(?:::~?[A-Za-z_]\w*)*)\s*(.*)$")
UUID = re.compile(r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b")
WINDOWS_PATH = re.compile(r"\b[A-Za-z]:\\[^\s|]+")
HEX = re.compile(r"\b0x[0-9a-fA-F]+\b")
QUOTED = re.compile(r"(['\"])(?:\\.|(?!\1).)*\1")
POINTER = re.compile(r"\b[0-9a-fA-F]{12,16}\b")
INTEGER = re.compile(r"(?<![A-Za-z_])[-+]?\d+(?![A-Za-z_])")
WHITESPACE = re.compile(r"\s+")


def normalize(message: str) -> str:
    value = message.strip()
    value = UUID.sub("<UUID>", value)
    value = WINDOWS_PATH.sub("<PATH>", value)
    value = HEX.sub("<HEX>", value)
    value = POINTER.sub("<POINTER>", value)
    value = QUOTED.sub("<VALUE>", value)
    value = INTEGER.sub("<N>", value)
    return WHITESPACE.sub(" ", value).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path)
    parser.add_argument("--ground-truth", type=Path, default=None)
    args = parser.parse_args()
    gt_path = args.ground_truth or (Path(__file__).resolve().parents[1] / "docs" / "ground_truth_s7_s13.json")
    if not args.log.is_file() or not gt_path.is_file():
        print("ERROR: log or ground-truth file does not exist")
        return 2

    truth = json.loads(gt_path.read_text(encoding="utf-8"))
    records = []
    bad_lines = []
    for number, raw in enumerate(args.log.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        match = LOG_LINE.match(raw)
        if not match:
            bad_lines.append(number)
            continue
        payload = match["payload"].strip()
        split = FUNCTION_SPLIT.match(payload)
        function, message = (split.group(1), split.group(2)) if split else ("", payload)
        records.append({
            "line": number,
            "time": datetime.strptime(match["timestamp"], "%d-%m-%Y_%H:%M:%S.%f"),
            "thread": match["thread_id"].strip(),
            "level": match["level"].strip(),
            "function": function,
            "message": message,
            "normalized_message": normalize(message),
        })

    failures = []
    print(f"{args.log.name}: {len(records)} parsed records, {len(bad_lines)} malformed lines")
    for scenario in truth.get("scenarios", []):
        sid = scenario["scenario_id"]
        anchor = scenario["log_anchor"]
        expected_message = anchor["normalized_message"]
        matches = [r for r in records if r["level"] == "ERROR" and r["function"] == anchor["function"]
                   and r["normalized_message"] == expected_message]
        starts = [r for r in records if r["message"] == f"Scenario {sid} start"]
        status = "PASS" if matches and starts else "FAIL"
        print(f"  [{status}] {sid}: scenario starts={len(starts)}, anchored ERRORs={len(matches)}")
        if not matches or not starts:
            failures.append(f"{sid}: missing scenario start or anchored error")

    if bad_lines:
        failures.append(f"{len(bad_lines)} malformed log line(s), first at line {bad_lines[0]}")
    if failures:
        for item in failures:
            print("FAIL:", item)
        print(f"RESULT: FAIL ({len(failures)} problem(s))")
        return 1
    print(f"RESULT: PASS ({len(truth['scenarios'])} scenarios anchored)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
