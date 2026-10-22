from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any


class SourceReaderError(RuntimeError):
    """Raised when local source cannot be read safely."""


@dataclass(frozen=True)
class SourceReadResult:
    source_root: str
    relative_path: str
    absolute_path: str
    start_line: int
    end_line: int
    actual_end_line: int
    total_file_lines: int
    source: str
    numbered_source: str
    expected_content_hash: str
    computed_content_hash: str
    content_hash_matches: bool | None
    hash_match_variant: str
    file_exists: bool
    source_available: bool
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_relative_path(relative_path: str) -> Path:
    """
    Convert a Neptune repository-relative POSIX path into a local Path.

    Rejects absolute paths and parent-directory traversal.
    """
    if not relative_path or not relative_path.strip():
        raise SourceReaderError("The source relative path is blank.")

    normalized_text = relative_path.strip().replace("\\", "/")
    posix_path = PurePosixPath(normalized_text)

    if posix_path.is_absolute():
        raise SourceReaderError(
            f"Absolute source paths are not allowed: {relative_path}"
        )

    if ".." in posix_path.parts:
        raise SourceReaderError(
            f"Parent-directory traversal is not allowed: {relative_path}"
        )

    return Path(*posix_path.parts)


def resolve_source_path(
    source_root: str | Path,
    relative_path: str,
) -> Path:
    """
    Resolve a graph-relative path under source_root.

    The final path must remain inside source_root.
    """
    root = Path(source_root).expanduser().resolve()
    relative = normalize_relative_path(relative_path)
    candidate = (root / relative).resolve()

    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise SourceReaderError(
            f"Resolved source path escapes the repository root: {candidate}"
        ) from error

    return candidate


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _hash_variants(lines: list[str]) -> dict[str, str]:
    """
    Compute common source-snippet hash variants.

    We do not silently treat every mismatch as success. The variant is recorded
    so the extraction hash implementation can later be matched exactly.
    """
    source_lf = "\n".join(lines)
    source_lf_final = source_lf + "\n"
    source_crlf = "\r\n".join(lines)
    source_crlf_final = source_crlf + "\r\n"

    return {
        "LF_NO_FINAL_NEWLINE": _sha256_text(source_lf),
        "LF_FINAL_NEWLINE": _sha256_text(source_lf_final),
        "CRLF_NO_FINAL_NEWLINE": _sha256_text(source_crlf),
        "CRLF_FINAL_NEWLINE": _sha256_text(source_crlf_final),
    }


def read_source_range(
    source_root: str | Path,
    relative_path: str,
    start_line: int,
    end_line: int,
    expected_content_hash: str = "",
) -> SourceReadResult:
    """
    Read an exact inclusive source line range.

    Line numbers are one-based. The returned source uses LF line endings.
    """
    warnings: list[str] = []

    try:
        start_line = int(start_line)
        end_line = int(end_line)
    except (TypeError, ValueError) as error:
        raise SourceReaderError(
            "start_line and end_line must be integers."
        ) from error

    if start_line < 1:
        raise SourceReaderError(
            f"start_line must be at least 1, received {start_line}."
        )

    if end_line < start_line:
        raise SourceReaderError(
            f"end_line {end_line} is before start_line {start_line}."
        )

    root = Path(source_root).expanduser().resolve()
    source_path = resolve_source_path(root, relative_path)

    if not source_path.exists():
        raise SourceReaderError(
            f"Source file does not exist: {source_path}"
        )

    if not source_path.is_file():
        raise SourceReaderError(
            f"Source path is not a file: {source_path}"
        )

    try:
        text = source_path.read_text(
            encoding="utf-8-sig",
            errors="strict",
        )
    except UnicodeDecodeError:
        warnings.append(
            "File was not valid UTF-8; decoded using Windows cp1252."
        )
        text = source_path.read_text(
            encoding="cp1252",
            errors="replace",
        )

    all_lines = text.splitlines()
    total_lines = len(all_lines)

    if start_line > total_lines:
        raise SourceReaderError(
            f"start_line {start_line} exceeds the file's "
            f"{total_lines} lines: {source_path}"
        )

    actual_end_line = min(end_line, total_lines)

    if end_line > total_lines:
        warnings.append(
            f"Requested end line {end_line} exceeds file length "
            f"{total_lines}; source was truncated at line {actual_end_line}."
        )

    selected_lines = all_lines[start_line - 1:actual_end_line]
    source = "\n".join(selected_lines)

    width = len(str(actual_end_line))
    numbered_source = "\n".join(
        f"{line_number:>{width}} | {line_text}"
        for line_number, line_text in enumerate(
            selected_lines,
            start=start_line,
        )
    )

    hash_variants = _hash_variants(selected_lines)
    expected_hash = (expected_content_hash or "").strip().lower()

    content_hash_matches: bool | None
    hash_match_variant = ""

    if expected_hash:
        matching_variants = [
            variant
            for variant, digest in hash_variants.items()
            if digest.lower() == expected_hash
        ]

        content_hash_matches = bool(matching_variants)
        hash_match_variant = (
            matching_variants[0]
            if matching_variants
            else "NO_SUPPORTED_VARIANT_MATCHED"
        )

        if not content_hash_matches:
            warnings.append(
                "The local source range does not match the content hash "
                "stored in Neptune. The source snapshot, line range, "
                "encoding, or extractor hash convention may differ."
            )
    else:
        content_hash_matches = None
        hash_match_variant = "EXPECTED_HASH_NOT_PROVIDED"
        warnings.append(
            "No expected content hash was provided, so hash verification "
            "was not performed."
        )

    return SourceReadResult(
        source_root=str(root),
        relative_path=relative_path.replace("\\", "/"),
        absolute_path=str(source_path),
        start_line=start_line,
        end_line=end_line,
        actual_end_line=actual_end_line,
        total_file_lines=total_lines,
        source=source,
        numbered_source=numbered_source,
        expected_content_hash=expected_hash,
        computed_content_hash=hash_variants[
            "LF_NO_FINAL_NEWLINE"
        ],
        content_hash_matches=content_hash_matches,
        hash_match_variant=hash_match_variant,
        file_exists=True,
        source_available=True,
        warnings=warnings,
    )