from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

LOG_LINE = re.compile(
    r"^(?P<timestamp>\d{2}-\d{2}-\d{4}_\d{2}:\d{2}:\d{2}\.\d{3})\s*\|"
    r"\s*(?P<perf_timer>[^|]+?)\s*\|"
    r"\s*(?P<thread_id>[^|]+?)\s*\|"
    r"\s*(?P<level>[^|]+?)\s*\|"
    r"\s*(?P<payload>.*)$"
)

# Handles names such as A::B::Function and lambda operator names.
OPERATOR_FUNCTION = re.compile(
    r"^(?P<function>.+?::operator\s*\(\))(?P<message>.*)$"
)
QUALIFIED_FUNCTION = re.compile(
    r"^(?P<function>(?:[A-Za-z_~][A-Za-z0-9_~<>]*::)+"
    r"[A-Za-z_~][A-Za-z0-9_~<>]*)"
    r"(?:(?:\s+)|(?::(?=[A-Za-z])))?"
    r"(?P<message>.*)$"
)


@dataclass(frozen=True)
class LogRecord:
    record_number: int
    source_line: int
    timestamp: str
    timestamp_iso: str
    perf_timer: float | None
    thread_id: str
    level: str
    qualified_function: str
    function_name: str
    class_scope: str
    message: str
    raw_message: str
    raw_line: str


def parse_metadata(line: str) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for part in line.strip().split("\t"):
        if ":" not in part:
            continue
        key, value = part.split(":", 1)
        key = key.strip().lower().replace(" ", "_")
        metadata[key] = value.strip()
    return metadata


def split_function_and_message(payload: str) -> tuple[str, str]:
    payload = payload.strip()
    operator_match = OPERATOR_FUNCTION.match(payload)
    if operator_match:
        return (
            operator_match.group("function").strip(),
            operator_match.group("message").lstrip(" :"),
        )

    function_match = QUALIFIED_FUNCTION.match(payload)
    if function_match:
        return (
            function_match.group("function").strip(),
            function_match.group("message").lstrip(" :"),
        )

    # Keep the record parseable, but do not invent a function identity.
    return "", payload


def to_iso_timestamp(value: str) -> str:
    parsed = datetime.strptime(value, "%d-%m-%Y_%H:%M:%S.%f")
    return parsed.isoformat(timespec="milliseconds")


def parse_log_file(path: str | Path) -> tuple[dict[str, Any], list[LogRecord], dict[str, Any]]:
    log_path = Path(path)
    metadata: dict[str, Any] = {"source_file": log_path.name}
    records: list[LogRecord] = []
    malformed_lines: list[dict[str, Any]] = []
    header_seen = False

    with log_path.open("r", encoding="utf-8-sig", errors="replace") as source:
        for source_line, raw in enumerate(source, start=1):
            line = raw.rstrip("\r\n")
            if not line.strip():
                continue

            if source_line == 1 and "IP Address:" in line:
                metadata.update(parse_metadata(line))
                continue

            if "Date_Time" in line and "MessageType" in line and "Message" in line:
                header_seen = True
                continue

            match = LOG_LINE.match(line)
            if not match:
                malformed_lines.append({"source_line": source_line, "raw_line": line})
                continue

            values = match.groupdict()
            qualified_function, message = split_function_and_message(values["payload"])
            scopes = qualified_function.split("::") if qualified_function else []
            function_name = scopes[-1] if scopes else ""
            class_scope = "::".join(scopes[:-1]) if len(scopes) > 1 else ""

            try:
                perf_timer = float(values["perf_timer"].strip())
            except ValueError:
                perf_timer = None

            records.append(
                LogRecord(
                    record_number=len(records) + 1,
                    source_line=source_line,
                    timestamp=values["timestamp"],
                    timestamp_iso=to_iso_timestamp(values["timestamp"]),
                    perf_timer=perf_timer,
                    thread_id=values["thread_id"].strip(),
                    level=values["level"].strip().upper(),
                    qualified_function=qualified_function,
                    function_name=function_name,
                    class_scope=class_scope,
                    message=message.strip(),
                    raw_message=values["payload"].strip(),
                    raw_line=line,
                )
            )

    report = {
        "header_seen": header_seen,
        "parsed_record_count": len(records),
        "malformed_line_count": len(malformed_lines),
        "malformed_lines": malformed_lines,
        "level_counts": {},
        "blank_function_count": sum(1 for record in records if not record.qualified_function),
    }
    for record in records:
        report["level_counts"][record.level] = report["level_counts"].get(record.level, 0) + 1
    return metadata, records, report


def filter_errors(records: list[LogRecord]) -> list[LogRecord]:
    # Deliberately exact: WARN, EXCEPTION, INFO, TRUE, etc. are excluded.
    return [record for record in records if record.level == "ERROR"]


def write_jsonl(path: Path, records: list[LogRecord]) -> None:
    with path.open("w", encoding="utf-8") as output:
        for record in records:
            output.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Parse real DWrapper logs and retain only ERROR records.")
    parser.add_argument("--log", required=True)
    parser.add_argument("--output-directory", required=True)
    args = parser.parse_args()

    output = Path(args.output_directory).resolve()
    output.mkdir(parents=True, exist_ok=True)

    metadata, records, report = parse_log_file(args.log)
    errors = filter_errors(records)
    report["error_record_count"] = len(errors)

    write_jsonl(output / "parsed_records.jsonl", records)
    write_jsonl(output / "errors.jsonl", errors)
    (output / "run_metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    (output / "parse_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("========== TASK 29: LOG PARSING ==========")
    print(f"Parsed records: {len(records)}")
    print(f"ERROR records: {len(errors)}")
    print(f"Malformed lines: {report['malformed_line_count']}")
    print(f"Blank function identities: {report['blank_function_count']}")
    print(f"Output directory: {output}")
    print("==========================================")


if __name__ == "__main__":
    main()
