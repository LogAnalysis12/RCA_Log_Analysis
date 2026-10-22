"""Validate a demo log against ground_truth.json using the SAME rules as the real project.

Uses the exact LOG_LINE regex from parse_logs.py and the exact normalisation regexes from fingerprint.py,
then checks that every scenario produced its expected ERROR cluster and that the culprit function was
active on ANOTHER thread shortly before the error (the runtime evidence a get_log_context tool would use).

Usage:  python tools/check_logs.py logs/sample_run_01.log [ground_truth.json]
"""
import json
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# ---- copied from parse_logs.py ----
LOG_LINE = re.compile(
    r"^(?P<timestamp>\d{2}-\d{2}-\d{4}_\d{2}:\d{2}:\d{2}\.\d{3})\s*\|"
    r"\s*(?P<perf_timer>[^|]+?)\s*\|"
    r"\s*(?P<thread_id>[^|]+?)\s*\|"
    r"\s*(?P<level>[^|]+?)\s*\|"
    r"\s*(?P<payload>.*)$"
)
# ---- copied from fingerprint.py ----
UUID = re.compile(r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b")
WINDOWS_PATH = re.compile(r"\b[A-Za-z]:\\[^\s|]+")
HEX = re.compile(r"\b0x[0-9a-fA-F]+\b")
QUOTED = re.compile(r"(['\"])(?:\\.|(?!\1).)*\1")
POINTER = re.compile(r"\b[0-9a-fA-F]{12,16}\b")
INTEGER = re.compile(r"(?<![A-Za-z_])[-+]?\d+(?![A-Za-z_])")
WHITESPACE = re.compile(r"\s+")
FUNCTION_SPLIT = re.compile(r"^([A-Za-z_]\w*(?:::~?[A-Za-z_]\w*)*)\s*(.*)$")


def normalize(message: str) -> str:
    value = message.strip()
    value = UUID.sub("<UUID>", value)
    value = WINDOWS_PATH.sub("<PATH>", value)
    value = HEX.sub("<HEX>", value)
    value = POINTER.sub("<POINTER>", value)
    value = QUOTED.sub("<VALUE>", value)
    value = INTEGER.sub("<N>", value)
    return WHITESPACE.sub(" ", value).strip()


def load(path: Path):
    records, bad = [], 0
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        match = LOG_LINE.match(raw)
        if not match:
            bad += 1
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
        })
    return records, bad


def main() -> int:
    log_path = Path(sys.argv[1])
    truth_path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).resolve().parents[1] / "ground_truth.json"
    truth = json.loads(truth_path.read_text(encoding="utf-8"))
    records, bad = load(log_path)
    errors = [r for r in records if r["level"] == "ERROR"]
    threads = {r["thread"] for r in records}
    print(f"{log_path.name}: {len(records)} parsed lines, {bad} unparsed, {len(errors)} ERROR, {len(threads)} threads")

    clusters = defaultdict(list)
    for record in errors:
        clusters[(record["function"], normalize(record["message"]))].append(record)
    print("\nERROR clusters (same key as fingerprint.py):")
    for (function, message), items in sorted(clusters.items(), key=lambda kv: -len(kv[1])):
        print(f"  {len(items):>3} x {function} | {message}")

    window = truth["log_window_ms"] / 1000.0
    failures = 0
    print("\nScenario checks:")
    for scenario in truth["scenarios"]:
        expected = scenario["error_log"]
        items = [
            r
            for (function, message), group in clusters.items()
            for r in group
            if function == expected["function"] and re.search(re.escape(expected["message_regex"]), message)
        ]
        ok_cluster = len(items) >= expected["min_count"]
        culprit = next((e["function"] for e in scenario["edges"] if e["role"] == "culprit"), "")
        evidence = False
        gap_ms = None
        if items and culprit:
            first = min(items, key=lambda r: r["time"])
            starts = [
                r for r in records
                if r["function"] == culprit and "FUNCTION_START" in r["message"]
                and r["thread"] != first["thread"]
                and 0 <= (first["time"] - r["time"]).total_seconds() <= window
            ]
            if starts:
                evidence = True
                gap_ms = round((first["time"] - max(starts, key=lambda r: r["time"])["time"]).total_seconds() * 1000)
        wants = scenario["expects_cross_thread_evidence"]
        ok_evidence = evidence == wants
        status = "PASS" if ok_cluster and ok_evidence else "FAIL"
        failures += status == "FAIL"
        detail = f"errors={len(items)} (need >= {expected['min_count']})"
        detail += f", other-thread '{culprit}' active {gap_ms} ms before error" if evidence else ", no other-thread culprit activity"
        print(f"  [{status}] {scenario['id']} {scenario['class']}: {detail}")
    print("\nRESULT:", "ALL SCENARIOS OK" if not failures else f"{failures} FAILED")
    return 1 if failures or bad else 0


if __name__ == "__main__":
    sys.exit(main())
