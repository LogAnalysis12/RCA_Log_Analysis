from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from clang import cindex


FUNCTION_KINDS = {
    cindex.CursorKind.FUNCTION_DECL,
    cindex.CursorKind.CXX_METHOD,
    cindex.CursorKind.CONSTRUCTOR,
    cindex.CursorKind.DESTRUCTOR,
    cindex.CursorKind.FUNCTION_TEMPLATE,
}


def stable_function_id(
    repository_id: str,
    relative_path: str,
    qualified_name: str,
    signature: str,
) -> str:
    identity = "|".join(
        [
            repository_id,
            relative_path.replace("\\", "/"),
            qualified_name,
            signature,
        ]
    )

    digest = hashlib.sha256(
        identity.encode("utf-8")
    ).hexdigest()

    return f"fn_{digest[:24]}"


def qualified_name(cursor: cindex.Cursor) -> str:
    parts: list[str] = []
    current = cursor

    while current is not None:
        if current.kind == cindex.CursorKind.TRANSLATION_UNIT:
            break

        if current.spelling:
            parts.append(current.spelling)

        current = current.semantic_parent

    return "::".join(reversed(parts))


def class_name(cursor: cindex.Cursor) -> str | None:
    current = cursor.semantic_parent

    class_kinds = {
        cindex.CursorKind.CLASS_DECL,
        cindex.CursorKind.STRUCT_DECL,
        cindex.CursorKind.CLASS_TEMPLATE,
    }

    while current is not None:
        if current.kind in class_kinds:
            return current.spelling or None

        if current.kind == cindex.CursorKind.TRANSLATION_UNIT:
            break

        current = current.semantic_parent

    return None


def read_source_lines(
    file_path: Path,
    start_line: int,
    end_line: int,
) -> str:
    try:
        lines = file_path.read_text(
            encoding="utf-8",
            errors="replace",
        ).splitlines(keepends=True)

        start_index = max(start_line - 1, 0)
        end_index = min(end_line, len(lines))

        return "".join(lines[start_index:end_index])

    except OSError:
        return ""


def content_hash(source_code: str) -> str:
    normalized = source_code.replace("\r\n", "\n")

    return hashlib.sha256(
        normalized.encode("utf-8")
    ).hexdigest()


def is_inside_repository(
    file_path: Path,
    repository_root: Path,
) -> bool:
    try:
        file_path.resolve().relative_to(
            repository_root.resolve()
        )
        return True

    except ValueError:
        return False


def sanitize_compile_arguments(
    arguments: list[str],
    source_file: Path,
) -> list[str]:
    """
    Convert a compilation-databaseto arguments suitable
    for parsing the source file with libclang.

    The function keeps include paths, macros, language-standard flags,
    and other parsing-relevant options. It removes compiler-output,
    linking, debugging, and precompiled-header options that are not
    required for AST extraction.
    """

    sanitized: list[str] = []

    skip_exact = {
        "/c",
        "-c",
        "/MP",
        "/FS",
        "/Zi",
        "/RTC1",
        "/nologo",
    }

    removable_prefixes = (
        "/Fo",
        "/Fd",
        "/Fp",
        "/Yu",
        "/Yc",
        "-o",
    )

    source_resolved = source_file.resolve()

    for index, argument in enumerate(arguments):
        # The first argument is the original compiler executable,
        # such as cl.exe. LibClang does not need this argument.
        if index == 0:
            continue

        cleaned_argument = argument.strip('"')

        if argument in skip_exact:
            continue

        if argument.startswith(removable_prefixes):
            continue

        try:
            candidate_path = Path(cleaned_argument)

            if (
                candidate_path.exists()
                and candidate_path.resolve() == source_resolved
            ):
                # LibClang receives the source file separately.
                continue

        except OSError:
            # Some compiler arguments resemble paths but are not
            # valid filesystem paths. Keep those arguments.
            pass

        sanitized.append(argument)

    # The compilation database contains Microsoft compiler-style
    # options such as /I, /D, /TP, and /std:c++20.
    sanitized.insert(0, "--target=i686-pc-windows-msvc")
    sanitized.insert(0, "--driver-mode=cl")

    return sanitized

def safe_cursor_kind(cursor: cindex.Cursor):
    """
    Return the cursor kind when it is known by the Python Clang binding.

    Some LibClang AST cursor kinds are not registered in the Python
    bindings. In that situation, return None instead of stopping the
    complete extraction.
    """

    try:
        return cursor.kind

    except ValueError:
        return None

def should_export_file(
    file_path: Path,
    repository_root: Path,
) -> bool:
    """
    Export only project-owned source files.

    Generated build files and dependency implementations remain available
    to Clang for parsing, but they are excluded from graph output.
    """

    if not is_inside_repository(
        file_path=file_path,
        repository_root=repository_root,
    ):
        return False

    relative_path = (
        file_path.resolve()
        .relative_to(repository_root.resolve())
        .as_posix()
    )

    excluded_prefixes = (
        "out/",
        "build/",
        ".git/",
    )

    if relative_path.startswith(excluded_prefixes):
        return False

    excluded_fragments = (
        "/Generated/",
        "/CMakeFiles/",
    )

    if any(fragment in relative_path for fragment in excluded_fragments):
        return False

    return file_path.suffix.lower() in {
        ".c",
        ".cc",
        ".cpp",
        ".cxx",
        ".h",
        ".hh",
        ".hpp",
        ".hxx",
    }


def extract_functions(
    cursor: cindex.Cursor,
    repository_root: Path,
    repository_id: str,
) -> list[dict]:
    """
    Traverse the Clang AST project-owned function definitions.

    Unknown cursor kinds are skipped safely, but their child cursors are
    still visited so that valid function definitions below them are not lost.
    """

    records: list[dict] = []
    unknown_cursor_count = 0

    def visit(node: cindex.Cursor) -> None:
        nonlocal unknown_cursor_count

        node_kind = safe_cursor_kind(node)

        if node_kind is None:
            unknown_cursor_count += 1

        elif node_kind in FUNCTION_KINDS and node.is_definition():
            location_file = node.location.file

            if location_file is not None:
                file_path = Path(location_file.name)

                if should_export_file(
                    file_path=file_path,
                    repository_root=repository_root,
                ):
                    relative_path = (
                        file_path.resolve()
                        .relative_to(repository_root.resolve())
                        .as_posix()
                    )

                    start_line = node.extent.start.line
                    end_line = node.extent.end.line

                    source_code = read_source_lines(
                        file_path=file_path,
                        start_line=start_line,
                        end_line=end_line,
                    )

                    function_name = node.spelling
                    function_qualified_name = qualified_name(node)
                    function_signature = node.type.spelling

                    function_id = stable_function_id(
                        repository_id=repository_id,
                        relative_path=relative_path,
                        qualified_name=function_qualified_name,
                        signature=function_signature,
                    )

                    record = {
                        "function_id": function_id,
                        "repository_id": repository_id,
                        "class_name": class_name(node),
                        "function_name": function_name,
                        "qualified_name": function_qualified_name,
                        "signature": function_signature,
                        "relative_path": relative_path,
                        "start_line": start_line,
                        "end_line": end_line,
                        "content_hash": content_hash(source_code),
                        "source_code": source_code,
                        "cursor_kind": node_kind.name,
                    }

                    records.append(record)

        # Continue traversal even when this node has an unknown cursor kind.
        try:
            children = node.get_children()

            for child in children:
                visit(child)

        except ValueError:
            unknown_cursor_count += 1

    visit(cursor)

    print(
        f"Unknown cursor occurrences skipped safely: "
        f"{unknown_cursor_count}"
    )

    return records

def classify_diagnostics(
    diagnostics: list[dict],
    repository_root: Path,
) -> dict:
    """
    Split diagnostics into project-owned and external diagnostics.
    """

    project_diagnostics: list[dict] = []
    external_diagnostics: list[dict] = []

    repository_text = str(repository_root.resolve()).lower()

    for diagnostic in diagnostics:
        location = str(
            diagnostic.get("location") or ""
        ).lower()

        if repository_text in location:
            project_diagnostics.append(diagnostic)
        else:
            external_diagnostics.append(diagnostic)

    project_errors = [
        item
        for item in project_diagnostics
        if item["severity"] >= 3
    ]

    external_errors = [
        item
        for item in external_diagnostics
        if item["severity"] >= 3
    ]

    return {
        "project_diagnostic_count": len(
            project_diagnostics
        ),
        "external_diagnostic_count": len(
            external_diagnostics
        ),
        "project_error_count": len(
            project_errors
        ),
        "external_error_count": len(
            external_errors
        ),
        "project_errors": project_errors,
        "external_errors": external_errors,
    }

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Extract C/C++ function definitions from one "
            "translation unit using libclang."
        )
    )

    parser.add_argument(
        "--repository-root",
        required=True,
    )

    parser.add_argument(
        "--build-directory",
        required=True,
    )

    parser.add_argument(
        "--source-file",
        required=True,
    )

    parser.add_argument(
        "--output-directory",
        required=True,
    )

    parser.add_argument(
        "--libclang",
        required=True,
    )

    arguments = parser.parse_args()

    repository_root = Path(
        arguments.repository_root
    ).resolve()

    build_directory = Path(
        arguments.build_directory
    ).resolve()

    source_file = Path(
        arguments.source_file
    ).resolve()

    output_directory = Path(
        arguments.output_directory
    ).resolve()

    output_directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    cindex.Config.set_library_file(
        arguments.libclang
    )

    compilation_database = (
        cindex.CompilationDatabase.fromDirectory(
            str(build_directory)
        )
    )

    compile_commands = (
        compilation_database.getCompileCommands(
            str(source_file)
        )
    )

    if compile_commands is None:
        raise RuntimeError(
            "No compilation command found for "
            f"{source_file}"
        )

    command = next(iter(compile_commands), None)

    if command is None:
        raise RuntimeError(
            "Compilation command collection was empty."
        )

    compile_arguments = list(command.arguments)

    clang_arguments = sanitize_compile_arguments(
        arguments=compile_arguments,
        source_file=source_file,
    )

    index = cindex.Index.create()

    translation_unit = index.parse(
        path=str(source_file),
        args=clang_arguments,
        options=(
            cindex.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD
        ),
    )

    diagnostics = []

    for diagnostic in translation_unit.diagnostics:
        diagnostics.append(
            {
                "severity": diagnostic.severity,
                "spelling": diagnostic.spelling,
                "location": (
                    str(diagnostic.location)
                    if diagnostic.location
                    else None
                ),
            }
        )

    repository_id = repository_root.name

    functions = extract_functions(
        cursor=translation_unit.cursor,
        repository_root=repository_root,
        repository_id=repository_id,
    )

    functions_file = (
        output_directory / "functions.jsonl"
    )

    with functions_file.open(
        "w",
        encoding="utf-8",
    ) as output:
        for function in functions:
            output.write(
                json.dumps(
                    function,
                    ensure_ascii=False,
                )
            )
            output.write("\n")
    
    diagnostic_summary = classify_diagnostics(
    diagnostics=diagnostics,
    repository_root=repository_root,
    )

    report = {
        "repository_id": repository_id,
        "repository_root": str(repository_root),
        "source_file": str(source_file),
        "build_directory": str(build_directory),
        "functions_extracted": len(functions),
        "diagnostic_count": len(diagnostics),
        "diagnostics": diagnostics,
        "diagnostic_summary": diagnostic_summary,
    }

    report_file = (
        output_directory / "extraction_report.json"
    )

    report_file.write_text(
        json.dumps(
            report,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    print(
        f"Functions extracted: {len(functions)}"
    )

    print(
        f"Diagnostics reported: {len(diagnostics)}"
    )

    print(
        f"Output: {functions_file}"
    )


if __name__ == "__main__":
    main()