from __future__ import annotations

import re
from typing import Any


_GT_MARKER = re.compile(
    r"@GT\b|ground[\s_-]*truth",
    re.IGNORECASE,
)

_SCENARIO_ROLE = re.compile(
    r"\bS\d+\b.{0,160}\b(?:culprit|victim)\b",
    re.IGNORECASE,
)

_COMMENT_ONLY = re.compile(
    r"^\s*(?://|/\*|\*|\*/)\s*(.*?)\s*(?:\*/)?\s*$"
)


def _redact_line(line: str) -> tuple[str, bool]:
    """Redact evaluation annotations without changing a line's position."""
    marker = line.find("//")

    if marker >= 0:
        code_prefix = line[:marker]
        comment = line[marker + 2:]

        is_annotation = bool(_GT_MARKER.search(comment))
        is_role_header = (
            not code_prefix.strip()
            and bool(_SCENARIO_ROLE.search(comment))
        )

        if is_annotation or is_role_header:
            return code_prefix + "// [evaluation annotation redacted]", True

    # Also handle comment-only block-comment lines.
    match = _COMMENT_ONLY.match(line)
    if match:
        comment = match.group(1)
        if _GT_MARKER.search(comment) or _SCENARIO_ROLE.search(comment):
            indent = line[:len(line) - len(line.lstrip())]
            return indent + "// [evaluation annotation redacted]", True

    return line, False


def redact_source_payload(
    source_payload: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Return a presentation-safe source payload.

    The caller must verify the original source hash before calling this
    function. Hash metadata is preserved as provenance for the original,
    unredacted source. Original source files are never modified.
    """
    result = dict(source_payload)
    original = result.get("source", "")

    if not isinstance(original, str):
        original = ""

    original_lines = original.splitlines()
    redacted_lines: list[str] = []
    redacted_line_numbers: list[int] = []

    try:
        start_line = int(result.get("start_line", 1))
    except (TypeError, ValueError):
        start_line = 1

    for offset, line in enumerate(original_lines):
        cleaned, changed = _redact_line(line)
        redacted_lines.append(cleaned)
        if changed:
            redacted_line_numbers.append(start_line + offset)

    redacted_source = "\n".join(redacted_lines)

    try:
        end_line = int(result.get("actual_end_line", result.get("end_line", start_line)))
    except (TypeError, ValueError):
        end_line = start_line + max(0, len(redacted_lines) - 1)

    width = len(str(max(end_line, start_line)))
    numbered_source = "\n".join(
        f"{line_number:>{width}} | {line_text}"
        for line_number, line_text in enumerate(
            redacted_lines,
            start=start_line,
        )
    )

    result["source"] = redacted_source
    result["numbered_source"] = numbered_source

    metadata = {
        "applied": bool(redacted_line_numbers),
        "policy": "thread-demo-ground-truth-annotation-redaction-v1",
        "redacted_line_numbers": redacted_line_numbers,
        "line_numbers_preserved": True,
        "hash_verified_before_redaction": (
            result.get("content_hash_matches") is True
        ),
    }

    result["redaction"] = metadata
    return result, metadata
