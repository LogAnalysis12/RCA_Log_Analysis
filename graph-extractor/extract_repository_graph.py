from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from clang import cindex


CALL_KINDS = {
    cindex.CursorKind.CALL_EXPR,
}

FIELD_ACCESS_KINDS = {
    cindex.CursorKind.MEMBER_REF_EXPR,
    cindex.CursorKind.DECL_REF_EXPR,
}

TRACKABLE_DATA_DECL_KINDS = {
    cindex.CursorKind.FIELD_DECL,
    cindex.CursorKind.VAR_DECL,
}

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

def stable_function_id_from_cursor(
    cursor: cindex.Cursor,
    repository_id: str,
    repository_root: Path,
) -> str:
    """
    Generate a stable ID for a C/C++ function or method.

    Prefer Clang's USR because the USR normally remains identical across
    a declaration in a header and its definition in a source file.
    Fall back to path, qualified name, and signature if no USR exists.
    """

    try:
        usr = cursor.get_usr()
    except Exception:
        usr = ""

    if usr:
        identity = f"{repository_id}|USR|{usr}"

        digest = hashlib.sha256(
            identity.encode("utf-8")
        ).hexdigest()

        return f"fn_{digest[:24]}"

    location_file = cursor.location.file

    relative_path = ""

    if location_file is not None:
        file_path = Path(location_file.name)

        try:
            relative_path = (
                file_path.resolve()
                .relative_to(repository_root.resolve())
                .as_posix()
            )
        except (OSError, ValueError):
            relative_path = file_path.as_posix()

    return stable_function_id(
        repository_id=repository_id,
        relative_path=relative_path,
        qualified_name=qualified_name(cursor),
        signature=cursor.type.spelling,
    )

def stable_field_id_from_cursor(
    cursor: cindex.Cursor,
    repository_id: str,
    repository_root: Path,
) -> str:
    """
    Generate a stable ID for a C/C++ field or global variable.

    Prefer the Clang USR so that declarations and references to the
    same field receive the same identifier.
    """

    try:
        usr = cursor.get_usr()
    except Exception:
        usr = ""

    if usr:
        identity = f"{repository_id}|FIELD_USR|{usr}"

    else:
        location_file = cursor.location.file
        relative_path = ""

        if location_file is not None:
            file_path = Path(location_file.name)

            try:
                relative_path = (
                    file_path.resolve()
                    .relative_to(repository_root.resolve())
                    .as_posix()
                )
            except (OSError, ValueError):
                relative_path = file_path.as_posix()

        identity = "|".join(
            [
                repository_id,
                "FIELD",
                relative_path,
                qualified_name(cursor),
                cursor.type.spelling,
            ]
        )

    digest = hashlib.sha256(
        identity.encode("utf-8")
    ).hexdigest()

    return f"field_{digest[:24]}"


def cursor_expression(cursor: cindex.Cursor) -> str:
    """
    Reconstruct a compact source expression from a Clang cursor.
    """

    try:
        tokens = [
            token.spelling
            for token in cursor.get_tokens()
        ]

        expression = "".join(tokens)

        return expression[:1000]

    except Exception:
        return cursor.spelling or ""

def expression_suffix2(expression: str) -> str:
    """
    Return the final two meaningful parts of a field expression.

    Examples:
        order.customer.address -> customer.address
        object->status         -> object.status
        value                  -> value
    """

    normalized = (
        expression
        .replace("->", ".")
        .replace("::", ".")
    )

    parts = [
        part
        for part in normalized.split(".")
        if part
    ]

    if len(parts) >= 2:
        return ".".join(parts[-2:])

    return normalized

def is_trackable_data_declaration(
    declaration: cindex.Cursor,
) -> bool:
    """
    Track class/struct fields and global or namespace-level variables.

    Local variables and function parameters are intentionally excluded
    from the first POC version to avoid creating excessive graph noise.
    """

    declaration_kind = safe_cursor_kind(declaration)

    if declaration_kind == cindex.CursorKind.FIELD_DECL:
        return True

    if declaration_kind != cindex.CursorKind.VAR_DECL:
        return False

    parent = declaration.semantic_parent

    if parent is None:
        return False

    parent_kind = safe_cursor_kind(parent)

    return parent_kind in {
        cindex.CursorKind.TRANSLATION_UNIT,
        cindex.CursorKind.NAMESPACE,
        cindex.CursorKind.CLASS_DECL,
        cindex.CursorKind.STRUCT_DECL,
        cindex.CursorKind.CLASS_TEMPLATE,
    }

def classify_field_access(
    field_cursor: cindex.Cursor,
    ancestors: list[cindex.Cursor],
) -> str:
    """
    Classify a field reference as READ, WRITE, READ_WRITE,
    or POSSIBLE_WRITE.

    The nearest relevant parent expression is inspected first.
    """

    field_start_offset = field_cursor.extent.start.offset

    for ancestor in reversed(ancestors[-5:]):
        ancestor_kind = safe_cursor_kind(ancestor)

        if ancestor_kind == cindex.CursorKind.UNARY_OPERATOR:
            expression = cursor_expression(ancestor)

            if "++" in expression or "--" in expression:
                return "READ_WRITE"

            if expression.lstrip().startswith("&"):
                return "POSSIBLE_WRITE"

        if ancestor_kind in {
            cindex.CursorKind.BINARY_OPERATOR,
            cindex.CursorKind.COMPOUND_ASSIGNMENT_OPERATOR,
        }:
            try:
                tokens = list(ancestor.get_tokens())
            except Exception:
                tokens = []

            assignment_operators = {
                "=",
                "+=",
                "-=",
                "*=",
                "/=",
                "%=",
                "&=",
                "|=",
                "^=",
                "<<=",
                ">>=",
            }

            operator_token = None

            for token in tokens:
                if token.spelling in assignment_operators:
                    operator_token = token
                    break

            if operator_token is None:
                continue

            field_is_left_side = (
                field_start_offset
                < operator_token.extent.start.offset
            )

            if not field_is_left_side:
                return "READ"

            if operator_token.spelling == "=":
                return "WRITE"

            return "READ_WRITE"

        if ancestor_kind == cindex.CursorKind.CALL_EXPR:
            expression = cursor_expression(ancestor)
            field_expression = cursor_expression(field_cursor)

            if (
                f"&{field_expression}" in expression
                or f"&({field_expression})" in expression
            ):
                return "POSSIBLE_WRITE"

    return "READ"

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

                    function_id = stable_function_id_from_cursor(
                        cursor=node,
                        repository_id=repository_id,
                        repository_root=repository_root,
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

def resolve_called_function(
    referenced_cursor: cindex.Cursor | None,
    repository_root: Path,
    repository_id: str,
) -> dict:
    """
    Resolve a called declaration to a project Function ID when possible.

    External-library calls and calls that cannot be resolved statically
    are retained without a callee Function ID.
    """

    result = {
        "callee_function_id": None,
        "callee_name": None,
        "qualified_callee_name": None,
        "callee_signature": None,
        "resolution_status": "UNRESOLVED",
    }

    if referenced_cursor is None:
        return result

    try:
        result["callee_name"] = referenced_cursor.spelling or None
        result["qualified_callee_name"] = (
            qualified_name(referenced_cursor) or None
        )
        result["callee_signature"] = (
            referenced_cursor.type.spelling or None
        )
    except Exception:
        return result

    target_cursor = referenced_cursor

    try:
        definition_cursor = referenced_cursor.get_definition()

        if definition_cursor is not None:
            target_cursor = definition_cursor
    except Exception:
        pass

    try:
        location_file = target_cursor.location.file

        if location_file is None:
            result["resolution_status"] = "EXTERNAL_OR_BUILTIN"
            return result

        target_file = Path(location_file.name)

        if not should_export_file(
            file_path=target_file,
            repository_root=repository_root,
        ):
            result["resolution_status"] = "EXTERNAL"
            return result

        relative_path = (
            target_file.resolve()
            .relative_to(repository_root.resolve())
            .as_posix()
        )

        target_qualified_name = qualified_name(target_cursor)
        target_signature = target_cursor.type.spelling

        result["callee_function_id"] = stable_function_id_from_cursor(
            cursor=target_cursor,
            repository_id=repository_id,
            repository_root=repository_root,
        )

        result["callee_name"] = (
            target_cursor.spelling
            or result["callee_name"]
        )

        result["qualified_callee_name"] = (
            target_qualified_name
            or result["qualified_callee_name"]
        )

        result["callee_signature"] = (
            target_signature
            or result["callee_signature"]
        )

        result["resolution_status"] = "RESOLVED_PROJECT"

    except (OSError, ValueError):
        result["resolution_status"] = "UNRESOLVED"

    return result

def extract_calls(
    cursor: cindex.Cursor,
    repository_root: Path,
    repository_id: str,
) -> list[dict]:
    """
    Extract calls project-owned function definitions.

    Every call is linked to its caller by stable Function ID. Calls to
    project functions receive a callee Function ID when LibClang can
    resolve the target. External and unresolved calls are retained.
    """

    records: list[dict] = []

    def visit_function_body(
        node: cindex.Cursor,
        caller_function_id: str,
        root_function_cursor: cindex.Cursor,
    ) -> None:
        node_kind = safe_cursor_kind(node)

        if (
            node is not root_function_cursor
            and node_kind in FUNCTION_KINDS
            and node.is_definition()
        ):
            # Do not assign calls from a nested function, lambda call
            # operator or another encountered definition to this caller.
            return

        if node_kind in CALL_KINDS:
            referenced_cursor = None

            try:
                referenced_cursor = node.referenced
            except Exception:
                pass

            resolved = resolve_called_function(
                referenced_cursor=referenced_cursor,
                repository_root=repository_root,
                repository_id=repository_id,
            )

            call_type = classify_call_type(
                referenced_cursor=referenced_cursor,
            )

            call_expression = ""

            try:
                call_expression = "".join(
                    token.spelling
                    for token in node.get_tokens()
                )
            except Exception:
                pass

            records.append(
                {
                    "caller_function_id": caller_function_id,
                    "callee_function_id": (
                        resolved["callee_function_id"]
                    ),
                    "callee_name": resolved["callee_name"],
                    "qualified_callee_name": (
                        resolved["qualified_callee_name"]
                    ),
                    "callee_signature": (
                        resolved["callee_signature"]
                    ),
                    "call_line": node.location.line,
                    "call_column": node.location.column,
                    "call_type": call_type,
                    "resolution_status": (
                        resolved["resolution_status"]
                    ),
                    "call_expression": call_expression,
                }
            )

        try:
            for child in node.get_children():
                visit_function_body(
                    node=child,
                    caller_function_id=caller_function_id,
                    root_function_cursor=root_function_cursor,
                )
        except ValueError:
            pass

    def visit(node: cindex.Cursor) -> None:
        node_kind = safe_cursor_kind(node)

        if node_kind in FUNCTION_KINDS and node.is_definition():
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

                    caller_function_id = stable_function_id_from_cursor(
                        cursor=node,
                        repository_id=repository_id,
                        repository_root=repository_root,
                    )

                    visit_function_body(
                        node=node,
                        caller_function_id=caller_function_id,
                        root_function_cursor=node,
                    )

                    # The function body was already traversed.
                    return

        try:
            for child in node.get_children():
                visit(child)
        except ValueError:
            pass

        
    visit(cursor)

    unique_records: list[dict] = []
    seen_call_keys: set[tuple] = set()

    for record in records:
        call_key = (
            record["caller_function_id"],
            record["callee_function_id"],
            record["qualified_callee_name"],
            record["call_line"],
            record["call_column"],
            record["call_type"],
        )

        if call_key in seen_call_keys:
            continue

        seen_call_keys.add(call_key)
        unique_records.append(record)

    return unique_records

def extract_field_accesses(
    cursor: cindex.Cursor,
    repository_root: Path,
    repository_id: str,
) -> tuple[list[dict], list[dict]]:
    """
    Extract project fields and field-access relationships.

    Returns:
        data_fields:
            Unique field/global-variable records.

        data_accesses:
            Function-to-field READ, WRITE, READ_WRITE, and
            POSSIBLE_WRITE records.
    """

    data_fields_by_id: dict[str, dict] = {}
    data_access_records: list[dict] = []

    def visit_function_body(
        node: cindex.Cursor,
        caller_function_id: str,
        root_function_cursor: cindex.Cursor,
        ancestors: list[cindex.Cursor],
    ) -> None:
        node_kind = safe_cursor_kind(node)

        if (
            node is not root_function_cursor
            and node_kind in FUNCTION_KINDS
            and node.is_definition()
        ):
            return

        if node_kind in FIELD_ACCESS_KINDS:
            referenced_cursor = None

            try:
                referenced_cursor = node.referenced
            except Exception:
                referenced_cursor = None

            if (
                referenced_cursor is not None
                and is_trackable_data_declaration(
                    referenced_cursor
                )
            ):
                field_location = referenced_cursor.location.file

                field_relative_path = None

                if field_location is not None:
                    field_file = Path(field_location.name)

                    try:
                        field_relative_path = (
                            field_file.resolve()
                            .relative_to(
                                repository_root.resolve()
                            )
                            .as_posix()
                        )
                    except (OSError, ValueError):
                        field_relative_path = None

                field_id = stable_field_id_from_cursor(
                    cursor=referenced_cursor,
                    repository_id=repository_id,
                    repository_root=repository_root,
                )

                field_expression = cursor_expression(node)

                field_name = (
                    referenced_cursor.spelling
                    or node.spelling
                    or field_expression
                )

                field_qualified_name = (
                    qualified_name(referenced_cursor)
                    or field_name
                )

                field_type = (
                    referenced_cursor.type.spelling
                    or None
                )

                declaration_line = (
                    referenced_cursor.location.line
                    if referenced_cursor.location
                    else None
                )

                data_fields_by_id[field_id] = {
                    "field_id": field_id,
                    "repository_id": repository_id,
                    "field_name": field_name,
                    "qualified_name": field_qualified_name,
                    "field_type": field_type,
                    "declaration_relative_path": (
                        field_relative_path
                    ),
                    "declaration_line": declaration_line,
                    "declaration_kind": (
                        safe_cursor_kind(
                            referenced_cursor
                        ).name
                        if safe_cursor_kind(
                            referenced_cursor
                        )
                        is not None
                        else None
                    ),
                }

                access_type = classify_field_access(
                    field_cursor=node,
                    ancestors=ancestors,
                )

                data_access_records.append(
                    {
                        "function_id": caller_function_id,
                        "field_id": field_id,
                        "chain": field_expression,
                        "suffix2": expression_suffix2(
                            field_expression
                        ),
                        "field_name": field_name,
                        "field_type": field_type,
                        "access_type": access_type,
                        "line": node.location.line,
                        "column": node.location.column,
                    }
                )

        try:
            for child in node.get_children():
                visit_function_body(
                    node=child,
                    caller_function_id=caller_function_id,
                    root_function_cursor=root_function_cursor,
                    ancestors=[*ancestors, node],
                )
        except ValueError:
            pass

    def visit(node: cindex.Cursor) -> None:
        node_kind = safe_cursor_kind(node)

        if node_kind in FUNCTION_KINDS and node.is_definition():
            location_file = node.location.file

            if location_file is not None:
                file_path = Path(location_file.name)

                if should_export_file(
                    file_path=file_path,
                    repository_root=repository_root,
                ):
                    caller_function_id = (
                        stable_function_id_from_cursor(
                            cursor=node,
                            repository_id=repository_id,
                            repository_root=repository_root,
                        )
                    )

                    visit_function_body(
                        node=node,
                        caller_function_id=caller_function_id,
                        root_function_cursor=node,
                        ancestors=[],
                    )

                    return

        try:
            for child in node.get_children():
                visit(child)
        except ValueError:
            pass

    visit(cursor)

    unique_accesses: list[dict] = []
    seen_access_keys: set[tuple] = set()

    for record in data_access_records:
        access_key = (
            record["function_id"],
            record["field_id"],
            record["access_type"],
            record["line"],
            record["column"],
            record["chain"],
        )

        if access_key in seen_access_keys:
            continue

        seen_access_keys.add(access_key)
        unique_accesses.append(record)

    return (
        list(data_fields_by_id.values()),
        unique_accesses,
    )

def classify_call_type(
    referenced_cursor: cindex.Cursor | None,
) -> str:
    """
    Classify a function call using the referenced Clang declaration.
    """

    if referenced_cursor is None:
        return "UNRESOLVED"

    referenced_kind = safe_cursor_kind(referenced_cursor)

    if referenced_kind is None:
        return "UNRESOLVED"

    if referenced_kind == cindex.CursorKind.CONSTRUCTOR:
        return "CONSTRUCTOR"

    if referenced_kind == cindex.CursorKind.CXX_METHOD:
        try:
            if referenced_cursor.is_virtual_method():
                return "VIRTUAL"
        except Exception:
            pass

        return "MEMBER"

    if referenced_kind in {
        cindex.CursorKind.FUNCTION_DECL,
        cindex.CursorKind.FUNCTION_TEMPLATE,
    }:
        return "DIRECT"

    return "UNRESOLVED"

def load_validated_extractor(
    extractor_path: Path,
):
    """
    Load the validated single-translation-unit extractor without
    copying or rewriting its extraction logic.
    """

    spec = importlib.util.spec_from_file_location(
        "validated_graph_extractor",
        str(extractor_path),
    )

    if spec is None or spec.loader is None:
        raise RuntimeError(
            f"Could not load extractor module: {extractor_path}"
        )

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    return module

def discover_project_translation_units(
    compilation_database: cindex.CompilationDatabase,
    repository_root: Path,
) -> list[Path]:
    """
    Return the unique project-owned C and C++ translation units found
    in compile_commands.json.

    Generated sources, CMake files, build-directory sources, and files
    outside the repository are excluded.
    """

    allowed_extensions = {
        ".c",
        ".cc",
        ".cpp",
        ".cxx",
    }

    excluded_path_fragments = {
        "/out/",
        "/build/",
        "/generated/",
        "/cmakefiles/",
        "/.git/",
    }

    translation_units_by_path: dict[str, Path] = {}

    all_commands = compilation_database.getAllCompileCommands()

    if all_commands is None:
        raise RuntimeError(
            "No compilation commands were found in the "
            "compilation database."
        )

    repository_root_resolved = repository_root.resolve()

    for command in all_commands:
        source_file = Path(command.filename)

        try:
            source_file_resolved = source_file.resolve()
        except OSError:
            continue

        if not source_file_resolved.exists():
            continue

        if source_file_resolved.suffix.lower() not in allowed_extensions:
            continue

        try:
            relative_path = (
                source_file_resolved
                .relative_to(repository_root_resolved)
                .as_posix()
            )

        except ValueError:
            # The source file is outside the project repository.
            continue

        normalized_path = f"/{relative_path.lower()}"

        if any(
            fragment in normalized_path
            for fragment in excluded_path_fragments
        ):
            continue

        translation_units_by_path[
            str(source_file_resolved).lower()
        ] = source_file_resolved

    return sorted(
        translation_units_by_path.values(),
        key=lambda path: path.as_posix().lower(),
    )

def write_jsonl(
    output_path: Path,
    records: list[dict],
) -> None:
    """
    Write one JSON object per line.
    """

    with output_path.open(
        "w",
        encoding="utf-8",
    ) as output:
        for record in records:
            output.write(
                json.dumps(
                    record,
                    ensure_ascii=False,
                )
            )
            output.write("\n")

def write_csv(
    output_path: Path,
    field_names: list[str],
    records: list[dict],
) -> None:
    """
    Write a UTF-8 CSV file with a fixed header.
    """

    with output_path.open(
        "w",
        encoding="utf-8",
        newline="",
    ) as output:
        writer = csv.DictWriter(
            output,
            fieldnames=field_names,
            extrasaction="ignore",
        )

        writer.writeheader()
        writer.writerows(records)

def deduplicate_calls(
    records: list[dict],
) -> list[dict]:
    """Remove duplicate calls repeated across translation units."""

    unique_records: list[dict] = []
    seen_keys: set[tuple] = set()

    for record in records:
        call_key = (
            record.get("caller_function_id"),
            record.get("callee_function_id"),
            record.get("qualified_callee_name"),
            record.get("call_line"),
            record.get("call_column"),
            record.get("call_type"),
            record.get("call_expression"),
        )

        if call_key in seen_keys:
            continue

        seen_keys.add(call_key)
        unique_records.append(record)

    return unique_records

def deduplicate_accesses(
    records: list[dict],
) -> list[dict]:
    """
    Remove duplicate field accesses repeated across translation units.
    """

    unique_records: list[dict] = []
    seen_keys: set[tuple] = set()

    for record in records:
        access_key = (
            record.get("function_id"),
            record.get("field_id"),
            record.get("access_type"),
            record.get("line"),
            record.get("column"),
            record.get("chain"),
        )

        if access_key in seen_keys:
            continue

        seen_keys.add(access_key)
        unique_records.append(record)

    return unique_records

def process_translation_unit(
    source_file: Path,
    compilation_database: cindex.CompilationDatabase,
    repository_root: Path,
    repository_id: str,
    extractor_module,
) -> dict:
    """
    Parse one translation unit and return its extracted graph records.

    A failure is returned as a status record instead of terminating the
    complete repository extraction.
    """

    started_at = time.perf_counter()

    result = {
        "source_file": str(source_file),
        "relative_path": (
            source_file
            .relative_to(repository_root)
            .as_posix()
        ),
        "status": "FAILED",
        "duration_seconds": 0.0,
        "functions": [],
        "calls": [],
        "data_fields": [],
        "data_accesses": [],
        "diagnostics": [],
        "diagnostic_summary": {},
        "error": None,
        "traceback": None,
    }

    try:
        compile_commands = (
            compilation_database.getCompileCommands(
                str(source_file)
            )
        )

        if compile_commands is None:
            raise RuntimeError(
                "No compilation command was found."
            )

        command = next(
            iter(compile_commands),
            None,
        )

        if command is None:
            raise RuntimeError(
                "Compilation command collection was empty."
            )

        compile_arguments = list(
            command.arguments
        )

        clang_arguments = (
            extractor_module.sanitize_compile_arguments(
                arguments=compile_arguments,
                source_file=source_file,
            )
        )

        index = cindex.Index.create()

        translation_unit = index.parse(
            path=str(source_file),
            args=clang_arguments,
            options=0,
        )

        diagnostics: list[dict] = []

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

        diagnostic_summary = (
            extractor_module.classify_diagnostics(
                diagnostics=diagnostics,
                repository_root=repository_root,
            )
        )

        functions = extractor_module.extract_functions(
            cursor=translation_unit.cursor,
            repository_root=repository_root,
            repository_id=repository_id,
        )

        calls = extractor_module.extract_calls(
            cursor=translation_unit.cursor,
            repository_root=repository_root,
            repository_id=repository_id,
        )

        data_fields, data_accesses = (
            extractor_module.extract_field_accesses(
                cursor=translation_unit.cursor,
                repository_root=repository_root,
                repository_id=repository_id,
            )
        )

        result.update(
            {
                "status": "SUCCESS",
                "functions": functions,
                "calls": calls,
                "data_fields": data_fields,
                "data_accesses": data_accesses,
                "diagnostics": diagnostics,
                "diagnostic_summary": diagnostic_summary,
            }
        )

    except Exception as error:
        result["error"] = str(error)
        result["traceback"] = traceback.format_exc()

    result["duration_seconds"] = round(
        time.perf_counter() - started_at,
        3,
    )

    return result

def single_transalation_unit_main() -> None:
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

    calls = extract_calls(
    cursor=translation_unit.cursor,
    repository_root=repository_root,
    repository_id=repository_id,
    )

    data_fields, data_accesses = extract_field_accesses(
    cursor=translation_unit.cursor,
    repository_root=repository_root,
    repository_id=repository_id,
    )

    if data_fields is None or data_accesses is None:
        raise RuntimeError(
            "Field extraction unexpectedly returned None."
        )

    if calls is None:
        raise RuntimeError(
            "extract_calls() returned None. Check the indentation "
            "of the deduplication block and return statement."
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

    calls_file = output_directory / "calls.csv"

    call_field_names = [
        "caller_function_id",
        "callee_function_id",
        "callee_name",
        "qualified_callee_name",
        "callee_signature",
        "call_line",
        "call_column",
        "call_type",
        "resolution_status",
        "call_expression",
    ]

    with calls_file.open(
        "w",
        encoding="utf-8",
        newline="",
    ) as output:
        writer = csv.DictWriter(
            output,
            fieldnames=call_field_names,
        )

        writer.writeheader()
        writer.writerows(calls)
    diagnostic_summary = classify_diagnostics(
    diagnostics=diagnostics,
    repository_root=repository_root,
    )

    data_fields_file = (
        output_directory / "data_fields.csv"
    )

    data_field_names = [
        "field_id",
        "repository_id",
        "field_name",
        "qualified_name",
        "field_type",
        "declaration_relative_path",
        "declaration_line",
        "declaration_kind",
    ]

    with data_fields_file.open(
        "w",
        encoding="utf-8",
        newline="",
    ) as output:
        writer = csv.DictWriter(
            output,
            fieldnames=data_field_names,
        )

        writer.writeheader()
        writer.writerows(data_fields)

    data_access_file = (
        output_directory / "data_access.csv"
    )

    data_access_field_names = [
        "function_id",
        "field_id",
        "chain",
        "suffix2",
        "field_name",
        "field_type",
        "access_type",
        "line",
        "column",
    ]

    with data_access_file.open(
        "w",
        encoding="utf-8",
        newline="",
    ) as output:
        writer = csv.DictWriter(
            output,
            fieldnames=data_access_field_names,
        )

        writer.writeheader()
        writer.writerows(data_accesses)

    report = {
        "repository_id": repository_id,
        "repository_root": str(repository_root),
        "source_file": str(source_file),
        "build_directory": str(build_directory),
        "functions_extracted": len(functions),
        "calls_extracted": len(calls),
        "diagnostic_count": len(diagnostics),
        "diagnostics": diagnostics,
        "diagnostic_summary": diagnostic_summary,
        "data_fields_extracted": len(data_fields),
        "data_accesses_extracted": len(data_accesses)
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
    print(
        f"Calls extracted: {len(calls)}"
    )

    print(
        f"Calls output: {calls_file}"
    )
    print(
        f"Data fields extracted: {len(data_fields)}"
    )

    print(
        f"Data accesses extracted: {len(data_accesses)}"
    )

    print(
        f"Data fields output: {data_fields_file}"
    )

    print(
        f"Data access output: {data_access_file}"
    )


def discovery_main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Extract and merge a repository-wide C/C++ call graph "
            "and field-level knowledge graph."
        )
    )

    parser.add_argument(
        "--repository-root",
        required=True,
        help="Root folder of the C/C++ repository.",
    )

    parser.add_argument(
        "--build-directory",
        required=True,
        help=(
            "Directory containing compile_commands.json."
        ),
    )

    parser.add_argument(
        "--output-directory",
        required=True,
        help="Directory for repository-wide graph artifacts.",
    )

    parser.add_argument(
        "--libclang",
        required=True,
        help="Full path to libclang.dll.",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help=(
            "Optional translation-unit limit for testing. "
            "Zero processes all translation units."
        ),
    )

    arguments = parser.parse_args()

    repository_root = Path(
        arguments.repository_root
    ).resolve()

    build_directory = Path(
        arguments.build_directory
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

    translation_units = discover_project_translation_units(
        compilation_database=compilation_database,
        repository_root=repository_root,
    )

    if arguments.limit > 0:
        translation_units = translation_units[
            :arguments.limit
        ]

    print(
        f"Repository root: {repository_root}"
    )

    print(
        f"Build directory: {build_directory}"
    )

    print(
        f"Project translation units discovered: "
        f"{len(translation_units)}"
    )

    for index, source_file in enumerate(
        translation_units,
        start=1,
    ):
        relative_path = (
            source_file
            .relative_to(repository_root)
            .as_posix()
        )

        print(
            f"[{index}/{len(translation_units)}] "
            f"{relative_path}"
        )

def repository_main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Extract and merge the complete C/C++ repository graph."
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
        "--output-directory",
        required=True,
    )

    parser.add_argument(
        "--libclang",
        required=True,
    )

    parser.add_argument(
        "--extractor-module",
        default=(
            ".\\graph-extractor\\"
            "extract_graph_step3_fields_working.py"
        ),
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help=(
            "Process only the first N translation units. "
            "Zero processes the entire repository."
        ),
    )

    arguments = parser.parse_args()

    repository_started_at = time.perf_counter()

    repository_root = Path(
        arguments.repository_root
    ).resolve()

    build_directory = Path(
        arguments.build_directory
    ).resolve()

    output_directory = Path(
        arguments.output_directory
    ).resolve()

    extractor_path = Path(
        arguments.extractor_module
    ).resolve()

    output_directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    if not repository_root.exists():
        raise FileNotFoundError(
            f"Repository root not found: {repository_root}"
        )

    if not build_directory.exists():
        raise FileNotFoundError(
            f"Build directory not found: {build_directory}"
        )

    if not extractor_path.exists():
        raise FileNotFoundError(
            f"Validated extractor not found: {extractor_path}"
        )

    cindex.Config.set_library_file(
        arguments.libclang
    )

    extractor_module = load_validated_extractor(
        extractor_path=extractor_path,
    )

    compilation_database = (
        cindex.CompilationDatabase.fromDirectory(
            str(build_directory)
        )
    )

    translation_units = discover_project_translation_units(
        compilation_database=compilation_database,
        repository_root=repository_root,
    )

    if arguments.limit > 0:
        translation_units = translation_units[
            :arguments.limit
        ]

    repository_id = repository_root.name

    functions_by_id: dict[str, dict] = {}
    fields_by_id: dict[str, dict] = {}

    all_calls: list[dict] = []
    all_accesses: list[dict] = []
    translation_unit_reports: list[dict] = []

    print(
        f"Project translation units selected: "
        f"{len(translation_units)}"
    )

    for position, source_file in enumerate(
        translation_units,
        start=1,
    ):
        relative_path = (
            source_file
            .relative_to(repository_root)
            .as_posix()
        )

        print(
            f"\n[{position}/{len(translation_units)}] "
            f"Parsing {relative_path}"
        )

        result = process_translation_unit(
            source_file=source_file,
            compilation_database=compilation_database,
            repository_root=repository_root,
            repository_id=repository_id,
            extractor_module=extractor_module,
        )

        for function in result["functions"]:
            function_id = function.get(
                "function_id"
            )

            if function_id:
                functions_by_id[
                    function_id
                ] = function

        for field in result["data_fields"]:
            field_id = field.get("field_id")

            if field_id:
                fields_by_id[field_id] = field

        all_calls.extend(result["calls"])
        all_accesses.extend(
            result["data_accesses"]
        )

        translation_unit_reports.append(
            {
                "relative_path": result[
                    "relative_path"
                ],
                "status": result["status"],
                "duration_seconds": result[
                    "duration_seconds"
                ],
                "functions": len(
                    result["functions"]
                ),
                "calls": len(result["calls"]),
                "data_fields": len(
                    result["data_fields"]
                ),
                "data_accesses": len(
                    result["data_accesses"]
                ),
                "project_errors": (
                    result[
                        "diagnostic_summary"
                    ].get(
                        "project_error_count",
                        0,
                    )
                    if result[
                        "diagnostic_summary"
                    ]
                    else 0
                ),
                "external_errors": (
                    result[
                        "diagnostic_summary"
                    ].get(
                        "external_error_count",
                        0,
                    )
                    if result[
                        "diagnostic_summary"
                    ]
                    else 0
                ),
                "error": result["error"],
            }
        )

        print(
            f"Status: {result['status']} | "
            f"Functions: {len(result['functions'])} | "
            f"Calls: {len(result['calls'])} | "
            f"Fields: {len(result['data_fields'])} | "
            f"Accesses: {len(result['data_accesses'])} | "
            f"Duration: {result['duration_seconds']}s"
        )

        if result["error"]:
            print(
                f"Error: {result['error']}"
            )

    functions = list(
        functions_by_id.values()
    )

    data_fields = list(
        fields_by_id.values()
    )

    calls = deduplicate_calls(all_calls)
    data_accesses = deduplicate_accesses(
        all_accesses
    )

    function_ids = {
        record["function_id"]
        for record in functions
        if record.get("function_id")
    }

    field_ids = {
        record["field_id"]
        for record in data_fields
        if record.get("field_id")
    }

    missing_callers = [
        record
        for record in calls
        if record.get("caller_function_id")
        not in function_ids
    ]

    missing_resolved_callees = [
        record
        for record in calls
        if (
            record.get("resolution_status")
            == "RESOLVED_PROJECT"
            and record.get("callee_function_id")
            and record.get("callee_function_id")
            not in function_ids
        )
    ]

    missing_access_functions = [
        record
        for record in data_accesses
        if record.get("function_id")
        not in function_ids
    ]

    missing_access_fields = [
        record
        for record in data_accesses
        if record.get("field_id")
        not in field_ids
    ]

    functions.sort(
        key=lambda record: (
            record.get("relative_path") or "",
            int(record.get("start_line") or 0),
            record.get("qualified_name") or "",
        )
    )

    data_fields.sort(
        key=lambda record: (
            record.get(
                "declaration_relative_path"
            ) or "",
            int(
                record.get(
                    "declaration_line"
                ) or 0
            ),
            record.get("qualified_name") or "",
        )
    )

    calls.sort(
        key=lambda record: (
            record.get("caller_function_id") or "",
            int(record.get("call_line") or 0),
            int(record.get("call_column") or 0),
            record.get(
                "qualified_callee_name"
            ) or "",
        )
    )

    data_accesses.sort(
        key=lambda record: (
            record.get("function_id") or "",
            int(record.get("line") or 0),
            int(record.get("column") or 0),
            record.get("chain") or "",
        )
    )

    write_jsonl(
        output_directory / "functions.jsonl",
        functions,
    )

    write_csv(
        output_directory / "calls.csv",
        [
            "caller_function_id",
            "callee_function_id",
            "callee_name",
            "qualified_callee_name",
            "callee_signature",
            "call_line",
            "call_column",
            "call_type",
            "resolution_status",
            "call_expression",
        ],
        calls,
    )

    write_csv(
        output_directory / "data_fields.csv",
        [
            "field_id",
            "repository_id",
            "field_name",
            "qualified_name",
            "field_type",
            "declaration_relative_path",
            "declaration_line",
            "declaration_kind",
        ],
        data_fields,
    )

    write_csv(
        output_directory / "data_access.csv",
        [
            "function_id",
            "field_id",
            "chain",
            "suffix2",
            "field_name",
            "field_type",
            "access_type",
            "line",
            "column",
        ],
        data_accesses,
    )

    write_csv(
        output_directory / "translation_units.csv",
        [
            "relative_path",
            "status",
            "duration_seconds",
            "functions",
            "calls",
            "data_fields",
            "data_accesses",
            "project_errors",
            "external_errors",
            "error",
        ],
        translation_unit_reports,
    )

    successful_units = sum(
        1
        for record in translation_unit_reports
        if record["status"] == "SUCCESS"
    )

    failed_units = len(
        translation_unit_reports
    ) - successful_units

    access_type_counts: dict[str, int] = {}

    for record in data_accesses:
        access_type = (
            record.get("access_type")
            or "UNKNOWN"
        )

        access_type_counts[access_type] = (
            access_type_counts.get(
                access_type,
                0,
            )
            + 1
        )

    resolution_status_counts: dict[str, int] = {}

    for record in calls:
        resolution_status = (
            record.get("resolution_status")
            or "UNKNOWN"
        )

        resolution_status_counts[
            resolution_status
        ] = (
            resolution_status_counts.get(
                resolution_status,
                0,
            )
            + 1
        )

    total_duration_seconds = round(
        time.perf_counter()
        - repository_started_at,
        3,
    )

    report = {
        "repository_id": repository_id,
        "repository_root": str(
            repository_root
        ),
        "build_directory": str(
            build_directory
        ),
        "extractor_module": str(
            extractor_path
        ),
        "extraction_timestamp_utc": (
            datetime.now(
                timezone.utc
            ).isoformat()
        ),
        "translation_units_selected": len(
            translation_units
        ),
        "translation_units_successful": (
            successful_units
        ),
        "translation_units_failed": (
            failed_units
        ),
        "unique_functions": len(functions),
        "unique_calls": len(calls),
        "unique_data_fields": len(
            data_fields
        ),
        "unique_data_accesses": len(
            data_accesses
        ),
        "missing_caller_ids": len(
            missing_callers
        ),
        "missing_resolved_callee_ids": len(
            missing_resolved_callees
        ),
        "missing_access_function_ids": len(
            missing_access_functions
        ),
        "missing_access_field_ids": len(
            missing_access_fields
        ),
        "call_resolution_status_counts": (
            resolution_status_counts
        ),
        "access_type_counts": (
            access_type_counts
        ),
        "duration_seconds": (
            total_duration_seconds
        ),
    }

    report_file = (
        output_directory
        / "extraction_report.json"
    )

    report_file.write_text(
        json.dumps(
            report,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    manifest = {
        "schema_version": "1.0",
        "repository_id": repository_id,
        "extraction_timestamp_utc": report[
            "extraction_timestamp_utc"
        ],
        "source_root_removed": True,
        "paths_are_repository_relative": True,
        "stable_function_ids": (
            "Clang USR with fallback"
        ),
        "artifacts": [
            "functions.jsonl",
            "calls.csv",
            "data_fields.csv",
            "data_access.csv",
            "translation_units.csv",
            "extraction_report.json",
        ],
    }

    (
        output_directory / "manifest.json"
    ).write_text(
        json.dumps(
            manifest,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    print(
        "\n========== REPOSITORY EXTRACTION SUMMARY =========="
    )

    print(
        f"Translation units selected: "
        f"{len(translation_units)}"
    )

    print(
        f"Translation units successful: "
        f"{successful_units}"
    )

    print(
        f"Translation units failed: "
        f"{failed_units}"
    )

    print(
        f"Unique functions: {len(functions)}"
    )

    print(
        f"Unique calls: {len(calls)}"
    )

    print(
        f"Unique data fields: "
        f"{len(data_fields)}"
    )

    print(
        f"Unique data accesses: "
        f"{len(data_accesses)}"
    )

    print(
        f"Missing caller IDs: "
        f"{len(missing_callers)}"
    )

    print(
        f"Missing resolved callee IDs: "
        f"{len(missing_resolved_callees)}"
    )

    print(
        f"Missing access function IDs: "
        f"{len(missing_access_functions)}"
    )

    print(
        f"Missing access field IDs: "
        f"{len(missing_access_fields)}"
    )

    print(
        f"Total duration: "
        f"{total_duration_seconds}s"
    )

    print(
        f"Output directory: "
        f"{output_directory}"
    )

    print(
        "=================================================="
    )

if __name__ == "__main__":
    repository_main()
