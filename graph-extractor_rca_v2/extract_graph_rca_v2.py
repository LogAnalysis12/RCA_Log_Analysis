"""Additive RCA evidence extractor built on the validated Step 3 Clang extractor.

The legacy artifact schemas are deliberately delegated unchanged. New artifact
records describe source evidence; they do not assert that a race, use-after-free,
or other defect occurs at runtime.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

import extract_graph_step3_fields_working as base
from clang import cindex


EVIDENCE_KEYS = (
    "variables", "function_parameters", "variable_accesses", "call_arguments",
    "data_flows", "lifetime_events", "thread_spawns", "lock_usage",
    "pointer_events", "synchronization_events", "control_flow_events",
    "macro_events", "types", "type_relationships", "callable_targets", "callable_calls", "return_flows",
    "call_sites", "external_symbols", "function_return_types",
)

VARIABLE_KINDS = {"FIELD_DECL", "VAR_DECL", "PARM_DECL"}
FUNCTION_REFERENCE_KINDS = {"FUNCTION_DECL", "CXX_METHOD", "CONSTRUCTOR", "DESTRUCTOR", "FUNCTION_TEMPLATE", "FUNCTION_TEMPLATE_SPECIALIZATION", "CONVERSION_FUNCTION"}
FUNCTION_KIND_NAMES = {getattr(kind, "name", str(kind)) for kind in base.FUNCTION_KINDS} | {"FUNCTION_TEMPLATE_SPECIALIZATION", "CONVERSION_FUNCTION"}
EXPRESSION_KIND_SUFFIXES = ("_EXPR", "_LITERAL", "_OPERATOR", "_STMT")


def _kind(cursor: Any) -> str:
    try:
        kind = cursor.kind
        return str(kind.name)
    except Exception:
        return "UNKNOWN"


def _children(cursor: Any) -> list[Any]:
    try:
        return list(cursor.get_children())
    except Exception:
        return []


def _walk(cursor: Any) -> Iterable[Any]:
    stack = [cursor]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(_children(current)))


def _tokens(cursor: Any) -> str:
    try:
        return " ".join(token.spelling for token in cursor.get_tokens())[:4000]
    except Exception:
        return str(getattr(cursor, "spelling", "") or "")[:4000]


def _hash_id(prefix: str, *parts: Any) -> str:
    raw = "|".join(str(part or "") for part in parts)
    return f"{prefix}_{hashlib.sha256(raw.encode('utf-8', errors='replace')).hexdigest()[:24]}"


def _relative_path(cursor: Any, root: Path) -> str:
    try:
        name = cursor.location.file.name if cursor.location.file else ""
        if not name:
            return ""
        file_path = Path(name).resolve()
        return file_path.relative_to(root.resolve()).as_posix()
    except Exception:
        return ""


def _location(cursor: Any, root: Path) -> dict[str, Any]:
    return {
        "relative_path": _relative_path(cursor, root),
        "line": int(getattr(getattr(cursor, "location", None), "line", 0) or 0),
        "column": int(getattr(getattr(cursor, "location", None), "column", 0) or 0),
        "start_line": int(getattr(getattr(getattr(cursor, "extent", None), "start", None), "line", 0) or 0),
        "end_line": int(getattr(getattr(getattr(cursor, "extent", None), "end", None), "line", 0) or 0),
    }


def _decl_usr(cursor: Any) -> str:
    try:
        return cursor.get_usr() or ""
    except Exception:
        return ""


def _parent_function(cursor: Any, root: Path, repository_id: str) -> tuple[str, str]:
    current = getattr(cursor, "semantic_parent", None)
    while current is not None:
        # A lambda body is a distinct callable scope. Do not walk through it and
        # incorrectly assign its locals/parameters to the enclosing function.
        if _kind(current) == "LAMBDA_EXPR":
            loc = _location(current, root)
            callable_id = _hash_id("lambda", repository_id, loc["relative_path"], loc["line"], loc["column"])
            return callable_id, f"lambda@{loc['relative_path']}:{loc['line']}:{loc['column']}"
        if _kind(current) in FUNCTION_KIND_NAMES:
            try:
                return base.stable_function_id_from_cursor(current, repository_id, root), base.qualified_name(current)
            except Exception:
                return "", ""
        if _kind(current) == "TRANSLATION_UNIT":
            break
        current = getattr(current, "semantic_parent", None)
    return "", ""


def _declaration_kind(cursor: Any) -> str:
    kind = _kind(cursor)
    if kind == "FIELD_DECL":
        return "FIELD"
    if kind == "PARM_DECL":
        return "PARAM"
    if kind == "VAR_DECL":
        parent = getattr(cursor, "semantic_parent", None)
        parent_kind = _kind(parent) if parent else ""
        if parent_kind in {"CLASS_DECL", "STRUCT_DECL", "CLASS_TEMPLATE", "CLASS_TEMPLATE_PARTIAL_SPECIALIZATION"}:
            return "FIELD"
        if parent_kind in {"TRANSLATION_UNIT", "NAMESPACE"}:
            return "GLOBAL"
        return "LOCAL"
    return kind


def _variable_id(cursor: Any, root: Path, repository_id: str) -> str:
    kind = _declaration_kind(cursor)
    if kind in {"FIELD", "GLOBAL"}:
        try:
            return base.stable_field_id_from_cursor(cursor, repository_id, root)
        except Exception:
            pass
    usr = _decl_usr(cursor)
    if usr:
        identity = f"{repository_id}|SYMBOL_USR|{usr}"
    else:
        loc = _location(cursor, root)
        identity = f"{repository_id}|{loc['relative_path']}|{loc['line']}|{loc['column']}|{kind}|{getattr(cursor, 'spelling', '')}|{getattr(getattr(cursor, 'type', None), 'spelling', '')}"
    return _hash_id("var", identity)


def _type_id(cursor: Any, root: Path, repository_id: str) -> str:
    usr = _decl_usr(cursor)
    loc = _location(cursor, root)
    identity = f"{repository_id}|TYPE_USR|{usr}" if usr else f"{repository_id}|TYPE|{base.qualified_name(cursor)}|{loc['relative_path']}|{loc['line']}"
    return _hash_id("type", identity)


def _type_id_from_clang_type(clang_type: Any, repository_id: str) -> str:
    """Return a stable ID for a type use, desugaring named project types to declaration IDs."""
    if clang_type is None:
        return ""
    try:
        decl = clang_type.get_declaration()
        if decl is not None and _kind(decl) in {"CLASS_DECL", "STRUCT_DECL", "UNION_DECL", "ENUM_DECL", "CLASS_TEMPLATE", "TYPEDEF_DECL", "TYPE_ALIAS_DECL"}:
            usr = _decl_usr(decl)
            if usr:
                return _hash_id("type", f"{repository_id}|TYPE_USR|{usr}")
    except Exception:
        pass
    try:
        canonical = clang_type.get_canonical()
    except Exception:
        canonical = clang_type
    spelling = str(getattr(canonical, "spelling", "") or getattr(clang_type, "spelling", "") or "<unknown>")
    spelling = re.sub(r"\s+", " ", spelling).strip()
    try:
        kind = canonical.kind.name
    except Exception:
        kind = "UNKNOWN"
    return _hash_id("type", f"{repository_id}|TYPE_USE|{kind}|{spelling}")


def _type_shape(clang_type: Any, config: dict[str, Any]) -> tuple[str, Any | None, str]:
    """Describe a type constructor without treating raw pointers as owning."""
    if clang_type is None:
        return "UNKNOWN", None, ""
    props = {"type_spelling": str(getattr(clang_type, "spelling", "") or ""), **{}}
    spelling = props["type_spelling"]
    try:
        canonical_spelling = str(getattr(clang_type.get_canonical(), "spelling", "") or spelling)
    except Exception:
        canonical_spelling = spelling
    try:
        kind = clang_type.kind.name
    except Exception:
        kind = "UNKNOWN"
    try:
        canonical = clang_type.get_canonical()
    except Exception:
        canonical = clang_type
    try:
        canonical_kind = canonical.kind.name
    except Exception:
        canonical_kind = kind
    try:
        const_qualified = bool(clang_type.is_const_qualified())
    except Exception:
        const_qualified = False
    try:
        volatile_qualified = bool(clang_type.is_volatile_qualified())
    except Exception:
        volatile_qualified = False
    smart_kind = "NONE"
    # Match both the written and canonical type spelling so typedef/using aliases
    # such as `using Owner = std::unique_ptr<Foo>` retain ownership-family hints.
    for pattern in config.get("smart_pointer_type_patterns", []):
        regex = pattern.get("match_regex", "(?!)")
        if _pattern_match(regex, spelling) or _pattern_match(regex, canonical_spelling):
            smart_kind = pattern.get("kind", "SMART_POINTER")
            break
    nested = None
    relation = ""
    if canonical_kind == "POINTER":
        shape = "RAW_POINTER"
        relation = "POINTS_TO_TYPE"
        try:
            nested = canonical.get_pointee()
        except Exception:
            pass
    elif canonical_kind == "LVALUEREFERENCE":
        shape = "LVALUE_REFERENCE"
        relation = "REFERENCES_TYPE"
        try:
            nested = canonical.get_pointee()
        except Exception:
            pass
    elif canonical_kind == "RVALUEREFERENCE":
        shape = "RVALUE_REFERENCE"
        relation = "REFERENCES_TYPE"
        try:
            nested = canonical.get_pointee()
        except Exception:
            pass
    elif smart_kind != "NONE":
        shape = smart_kind
        relation = "SMART_POINTER_TO_TYPE" if smart_kind != "WEAK_PTR" else "WEAKLY_REFERENCES_TYPE"
        try:
            argc = clang_type.get_num_template_arguments()
            if argc and argc > 0:
                nested = clang_type.get_template_argument_type(0)
        except Exception:
            try:
                argc = canonical.get_num_template_arguments()
                if argc and argc > 0:
                    nested = canonical.get_template_argument_type(0)
            except Exception:
                pass
    elif "ARRAY" in canonical_kind:
        shape = "ARRAY"
        relation = "ELEMENT_TYPE"
        try:
            nested = canonical.get_array_element_type()
        except Exception:
            pass
    elif canonical_kind in {"RECORD", "ENUM"}:
        shape = canonical_kind
    elif canonical_kind in {"VOID", "BOOL", "CHAR_U", "UCHAR", "CHAR16", "CHAR32", "USHORT", "UINT", "ULONG", "ULONGLONG", "UINT128", "CHAR_S", "SCHAR", "WCHAR", "SHORT", "INT", "LONG", "LONGLONG", "INT128", "FLOAT", "DOUBLE", "LONGDOUBLE", "COMPLEX", "FLOAT128"}:
        shape = "BUILTIN"
    else:
        shape = canonical_kind or "UNKNOWN"
    return shape, nested, relation


def _type_record_from_clang_type(clang_type: Any, repository_root: Path, repository_id: str, config: dict[str, Any], loc: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return a type node row and a direct type relationship row, if any."""
    if clang_type is None:
        return None, None
    try:
        canonical = clang_type.get_canonical()
    except Exception:
        canonical = clang_type
    type_id = _type_id_from_clang_type(clang_type, repository_id)
    spelling = str(getattr(clang_type, "spelling", "") or "<unknown>")
    canonical_spelling = str(getattr(canonical, "spelling", "") or spelling)
    shape, nested, relation = _type_shape(clang_type, config)
    props = {"type_spelling": spelling, **_type_properties_from_type(clang_type, config)}
    location = loc or {"relative_path": "", "line": 0, "start_line": 0, "end_line": 0}
    try:
        decl = clang_type.get_declaration()
    except Exception:
        decl = None
    decl_kind = _kind(decl) if decl is not None else ""
    if decl_kind in {"TYPEDEF_DECL", "TYPE_ALIAS_DECL"}:
        shape = "TYPE_ALIAS"
        relation = "TYPE_ALIAS_OF"
        try:
            nested = decl.underlying_typedef_type
        except Exception:
            nested = None
    if decl is not None:
        decl_loc = _location(decl, repository_root)
        if decl_loc.get("relative_path"):
            location = decl_loc
    decl_usr = _decl_usr(decl) if decl is not None else ""
    node = {
        "type_id": type_id, "repository_id": repository_id,
        "name": spelling, "qualified_name": canonical_spelling, "type_kind": shape,
        "relative_path": location.get("relative_path", ""), "line": int(location.get("line", 0) or 0),
        "start_line": int(location.get("start_line", 0) or 0), "end_line": int(location.get("end_line", 0) or 0),
        "usr": decl_usr, "canonical_type": canonical_spelling,
        "clang_type_kind": getattr(getattr(canonical, "kind", None), "name", "UNKNOWN"),
        "pointer_depth": props.get("pointer_depth", 0), "reference_kind": props.get("reference_kind", "NONE"),
        "pointee_type_id": _type_id_from_clang_type(nested, repository_id) if nested is not None else "",
        "pointee_type": str(getattr(nested, "spelling", "") or "") if nested is not None else "",
        "smart_pointer_kind": props.get("smart_pointer_kind", "NONE"),
        "ownership_model_hint": props.get("ownership_model_hint", "NOT_POINTER_LIKE"),
        "is_const": props.get("is_const", False), "is_volatile": props.get("is_volatile", False),
        "is_function_pointer": props.get("is_function_pointer", False),
        "evidence_grade": "CLANG_TYPE_API",
    }
    edge = None
    if nested is not None and relation:
        nested_id = _type_id_from_clang_type(nested, repository_id)
        edge = {
            "relationship_id": _hash_id("typeedge", type_id, relation, nested_id),
            "relationship_type": relation, "source_type_id": type_id, "source_type_name": spelling,
            "target_type_id": nested_id, "target_type_name": str(getattr(nested, "spelling", "") or ""),
            "relative_path": location.get("relative_path", ""), "line": int(location.get("line", 0) or 0),
            "evidence_grade": "CLANG_TYPE_API" if relation in {"POINTS_TO_TYPE", "REFERENCES_TYPE", "ELEMENT_TYPE", "TYPE_ALIAS_OF"} else "CLANG_TEMPLATE_ARGUMENT_API",
        }
    return node, edge


def _type_properties_from_type(clang_type: Any, config: dict[str, Any]) -> dict[str, Any]:
    # Type objects use the same spelling/canonical type semantics as declaration cursors.
    class CursorLike:
        def __init__(self, typ: Any):
            self.type = typ
            self.spelling = getattr(typ, "spelling", "")
    return _type_properties(CursorLike(clang_type), config)


def _type_properties(cursor: Any, config: dict[str, Any]) -> dict[str, Any]:
    typ = getattr(cursor, "type", None)
    spelling = str(getattr(typ, "spelling", "") or "")
    canonical = spelling
    type_kind = "UNKNOWN"
    pointer_depth = 0
    reference_kind = "NONE"
    pointee_type = ""
    const_qualified = False
    volatile_qualified = False
    is_function_pointer = False
    if typ is not None:
        try:
            canonical = typ.get_canonical().spelling or spelling
        except Exception:
            pass
        try:
            type_kind = typ.kind.name
        except Exception:
            pass
        # Desugar typedefs/elaborated types when inspecting structure, while
        # retaining the original spelling for smart-pointer-family recognition.
        try:
            current = typ.get_canonical()
        except Exception:
            current = typ
        seen = 0
        while current is not None and seen < 12:
            try:
                kind_name = current.kind.name
            except Exception:
                break
            if kind_name == "POINTER":
                pointer_depth += 1
                try:
                    next_type = current.get_pointee()
                    if not pointee_type:
                        pointee_type = next_type.spelling or ""
                    current = next_type
                except Exception:
                    break
                seen += 1
                continue
            if kind_name == "LVALUEREFERENCE":
                reference_kind = "LVALUE_REFERENCE"
                try:
                    current = current.get_pointee()
                except Exception:
                    break
                seen += 1
                continue
            if kind_name == "RVALUEREFERENCE":
                reference_kind = "RVALUE_REFERENCE"
                try:
                    current = current.get_pointee()
                except Exception:
                    break
                seen += 1
                continue
            break
        try:
            const_qualified = bool(typ.is_const_qualified())
            volatile_qualified = bool(typ.is_volatile_qualified())
        except Exception:
            pass
        is_function_pointer = "(*)" in spelling or "(*" in spelling or (pointer_depth > 0 and "(" in spelling and ")(" in spelling)
    smart_kind = "NONE"
    # Type aliases can hide smart-pointer spellings. Classify using canonical and
    # source spellings, while retaining the source spelling on the emitted record.
    for pattern in config.get("smart_pointer_type_patterns", []):
        regex = pattern.get("match_regex", "(?!)")
        try:
            if re.search(regex, spelling, flags=re.IGNORECASE) or re.search(regex, canonical, flags=re.IGNORECASE):
                smart_kind = pattern.get("kind", "SMART_POINTER")
                break
        except re.error:
            continue
    function_pointer_spelling = spelling + " " + canonical
    is_function_pointer = is_function_pointer or "(*)" in function_pointer_spelling or "(*" in function_pointer_spelling or (pointer_depth > 0 and "(" in function_pointer_spelling and ")(" in function_pointer_spelling)
    pointer_like = pointer_depth > 0 or reference_kind != "NONE" or smart_kind != "NONE"
    ownership = {
        "UNIQUE_PTR": "UNIQUE_OWNERSHIP",
        "SHARED_PTR": "SHARED_OWNERSHIP",
        "WEAK_PTR": "NON_OWNING_WEAK_REFERENCE",
        "INTRUSIVE_OR_CUSTOM_SMART_PTR": "CUSTOM_SMART_OWNERSHIP_SEMANTICS",
    }.get(smart_kind, "RAW_POINTER_OR_REFERENCE" if pointer_like else "NOT_POINTER_LIKE")
    return {
        "type_spelling": spelling,
        "canonical_type": canonical,
        "clang_type_kind": type_kind,
        "pointer_depth": pointer_depth,
        "reference_kind": reference_kind,
        "pointee_type": pointee_type,
        "is_pointer_like": pointer_like,
        "is_function_pointer": is_function_pointer,
        "smart_pointer_kind": smart_kind,
        "ownership_model_hint": ownership,
        "is_const": const_qualified,
        "is_volatile": volatile_qualified,
    }


def _variable_record(decl: Any, root: Path, repository_id: str, config: dict[str, Any], owner_function_id: str = "", owner_function_name: str = "") -> dict[str, Any] | None:
    kind = _kind(decl)
    if kind not in VARIABLE_KINDS:
        return None
    try:
        file = decl.location.file
        if file is None or not base.should_export_file(Path(file.name), root):
            return None
    except Exception:
        return None
    loc = _location(decl, root)
    variable_id = _variable_id(decl, root, repository_id)
    if not owner_function_id:
        owner_function_id, owner_function_name = _parent_function(decl, root, repository_id)
    decl_kind = _declaration_kind(decl)
    if decl_kind in {"FIELD", "GLOBAL"}:
        owner_function_id, owner_function_name = "", ""
    # If the AST visitor already supplied its lexical owner, preserve it. This is
    # essential for lambda locals/parameters because Clang's semantic-parent chain
    # may expose the synthesized operator() method rather than the lambda cursor.
    name = str(getattr(decl, "spelling", "") or "")
    result = {
        "variable_id": variable_id,
        "repository_id": repository_id,
        "name": name,
        "qualified_name": base.qualified_name(decl) or name,
        "declaration_kind": decl_kind,
        "clang_cursor_kind": kind,
        "owner_function_id": owner_function_id,
        "owner_function_name": owner_function_name,
        "relative_path": loc["relative_path"],
        "line": loc["line"],
        "column": loc["column"],
        "usr": _decl_usr(decl),
        "type_id": _type_id_from_clang_type(getattr(decl, "type", None), repository_id),
        **_type_properties(decl, config),
    }
    try:
        result["storage_class"] = decl.storage_class.name
    except Exception:
        result["storage_class"] = "UNKNOWN"
    return result


def _is_project_cursor(cursor: Any, root: Path) -> bool:
    path = _relative_path(cursor, root)
    return bool(path)


def _function_ids(cursor: Any, root: Path, repository_id: str) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    def visit(node: Any, inside_lambda: bool = False) -> None:
        kind = _kind(node)
        if kind == "LAMBDA_EXPR":
            for child in _children(node):
                visit(child, True)
            return
        if kind in FUNCTION_KIND_NAMES and not inside_lambda:
            try:
                is_definition = bool(node.is_definition())
            except Exception:
                is_definition = False
            if is_definition and _is_project_cursor(node, root):
                try:
                    fid = base.stable_function_id_from_cursor(node, repository_id, root)
                    loc = _location(node, root)
                    records[fid] = {"cursor": node, "function_id": fid, "qualified_name": base.qualified_name(node), **loc}
                except Exception:
                    pass
        for child in _children(node):
            visit(child, inside_lambda)
    visit(cursor)
    return records


def _function_return_type(cursor: Any) -> str:
    try:
        return cursor.type.get_result().spelling or ""
    except Exception:
        return ""


def _lambda_record(node: Any, root: Path, repository_id: str, lambda_cache: dict[tuple[str, int, int], dict[str, Any]]) -> dict[str, Any]:
    loc = _location(node, root)
    key = (loc["relative_path"], loc["line"], loc["column"])
    if key in lambda_cache:
        return lambda_cache[key]
    callable_id = _hash_id("lambda", repository_id, *key)
    source = _tokens(node)
    rec = {
        "callable_id": callable_id,
        "callable_kind": "LAMBDA",
        "qualified_name": f"lambda@{loc['relative_path']}:{loc['line']}:{loc['column']}",
        "signature": "lambda",
        "relative_path": loc["relative_path"],
        "start_line": loc["start_line"],
        "end_line": loc["end_line"],
        "line": loc["line"],
        "column": loc["column"],
        "content_hash": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "body_function_names": [],
    }
    lambda_cache[key] = rec
    return rec


def _referenced(cursor: Any) -> Any | None:
    try:
        return cursor.referenced
    except Exception:
        return None


def _referenced_variables(node: Any, root: Path, repository_id: str) -> list[tuple[Any, str]]:
    out: list[tuple[Any, str]] = []
    seen: set[str] = set()
    for sub in _walk(node):
        if _kind(sub) not in {"DECL_REF_EXPR", "MEMBER_REF_EXPR", "MEMBER_REF"}:
            continue
        decl = _referenced(sub)
        if decl is None or _kind(decl) not in VARIABLE_KINDS:
            continue
        try:
            vid = _variable_id(decl, root, repository_id)
        except Exception:
            continue
        if vid not in seen:
            seen.add(vid)
            out.append((decl, vid))
    return out


def _pick_target_variable(refs: list[tuple[Any, str]]) -> tuple[Any | None, str]:
    if not refs:
        return None, ""
    # For obj.field.reset()/lock_guard(obj.field), choose the member field rather
    # than the object expression used to reach it. Otherwise keep AST order.
    for preferred_kind in ("FIELD", "GLOBAL"):
        for decl, vid in reversed(refs):
            if _declaration_kind(decl) == preferred_kind:
                return decl, vid
    return refs[-1]


def _referenced_variable_uses(node: Any, root: Path, repository_id: str) -> list[tuple[Any, Any, str]]:
    out: list[tuple[Any, Any, str]] = []
    seen: set[tuple[int, str]] = set()
    for sub in _walk(node):
        if _kind(sub) not in {"DECL_REF_EXPR", "MEMBER_REF_EXPR", "MEMBER_REF"}:
            continue
        decl = _referenced(sub)
        if decl is None or _kind(decl) not in VARIABLE_KINDS:
            continue
        try:
            vid = _variable_id(decl, root, repository_id)
            offset = int(sub.extent.start.offset)
        except Exception:
            continue
        key = (offset, vid)
        if key not in seen:
            seen.add(key)
            out.append((sub, decl, vid))
    def use_offset(item: tuple[Any, Any, str]) -> int:
        try:
            return int(item[0].extent.start.offset)
        except Exception:
            return 0
    return sorted(out, key=use_offset)


def _referenced_functions(node: Any, root: Path, repository_id: str) -> list[tuple[Any, str]]:
    out: list[tuple[Any, str]] = []
    seen: set[str] = set()
    for sub in _walk(node):
        decl = _referenced(sub)
        if decl is None or _kind(decl) not in FUNCTION_REFERENCE_KINDS:
            continue
        try:
            fid = base.stable_function_id_from_cursor(decl, repository_id, root)
        except Exception:
            continue
        if fid not in seen:
            seen.add(fid)
            out.append((decl, fid))
    return out


def _event_id(prefix: str, function_id: str, kind: str, loc: dict[str, Any], text: str) -> str:
    return _hash_id(prefix, function_id, kind, loc.get("relative_path"), loc.get("line"), loc.get("column"), text)


def _call_arguments(node: Any) -> list[Any]:
    try:
        args = list(node.get_arguments())
        if args:
            return args
    except Exception:
        pass
    children = _children(node)
    args = [child for child in children if _kind(child).endswith(EXPRESSION_KIND_SUFFIXES)]
    # For CallExpr the first direct expression is normally the callee, not an argument.
    if _kind(node) == "CALL_EXPR" and args:
        args = args[1:]
    return args


def _callee_decl(node: Any) -> Any | None:
    """Find the direct callee declaration without accidentally selecting a callback argument."""
    ref = _referenced(node)
    if ref is not None and _kind(ref) in FUNCTION_REFERENCE_KINDS:
        return ref
    children = _children(node)
    # In Clang call/construct cursors, the callee expression precedes arguments.
    # Restrict fallback search to the first child subtree so function pointers passed
    # as ordinary arguments are not mislabeled as the called function.
    for child in children[:1]:
        for sub in _walk(child):
            candidate = _referenced(sub)
            if candidate is not None and _kind(candidate) in FUNCTION_REFERENCE_KINDS:
                return candidate
    return None


def _external_symbol_record(decl: Any, repository_id: str) -> dict[str, Any] | None:
    if decl is None:
        return None
    try:
        source_file = decl.location.file.name if decl.location.file else ""
    except Exception:
        source_file = ""
    if not source_file:
        return None
    signature = ""
    try:
        signature = decl.type.spelling or ""
    except Exception:
        pass
    usr = _decl_usr(decl)
    name = base.qualified_name(decl) or getattr(decl, "spelling", "") or signature
    symbol_id = _hash_id("external", repository_id, usr or name, signature)
    try:
        decl_kind = _kind(decl)
    except Exception:
        decl_kind = "UNKNOWN"
    try:
        is_system = bool(decl.location.is_in_system_header())
    except Exception:
        is_system = False
    return {
        "symbol_id": symbol_id, "qualified_name": name, "signature": signature,
        "declaration_kind": decl_kind, "usr": usr, "declaration_file": Path(source_file).as_posix(),
        "is_system_header": is_system, "resolution_basis": "CLANG_REFERENCED_DECLARATION",
        "evidence_grade": "STATIC_AST_RESOLVED_EXTERNAL",
    }


def _callee_details(node: Any, root: Path, repository_id: str) -> tuple[str, str, str]:
    ref = _referenced(node)
    if ref is None:
        for child in _children(node):
            ref = _referenced(child)
            if ref is not None and _kind(ref) in FUNCTION_REFERENCE_KINDS:
                break
    try:
        ref_file = getattr(getattr(ref, "location", None), "file", None) if ref is not None else None
        is_project_decl = bool(ref_file and base.should_export_file(Path(ref_file.name), root))
        fid = base.stable_function_id_from_cursor(ref, repository_id, root) if ref is not None and _kind(ref) in FUNCTION_REFERENCE_KINDS and is_project_decl else ""
    except Exception:
        fid = ""
    qname = base.qualified_name(ref) if ref is not None else ""
    typ = str(getattr(getattr(node, "type", None), "spelling", "") or "")
    expression = _tokens(node)
    return fid, qname or typ or expression, typ


def _lvalue_target_decl(lhs: Any) -> Any | None:
    """Return the declaration directly designated by a simple lvalue expression.

    For `p->field = x`, this returns `field`, not base pointer `p`. For `*p = x`
    and `p[i] = x`, there is no direct variable assignment to `p`, so returns None.
    """
    if lhs is None:
        return None
    kind = _kind(lhs)
    if kind in {"DECL_REF_EXPR", "MEMBER_REF_EXPR", "MEMBER_REF"}:
        decl = _referenced(lhs)
        return decl if decl is not None and _kind(decl) in VARIABLE_KINDS else None
    if kind in {"PAREN_EXPR", "UNEXPOSED_EXPR", "CSTYLE_CAST_EXPR", "CXX_STATIC_CAST_EXPR", "CXX_REINTERPRET_CAST_EXPR", "CXX_CONST_CAST_EXPR", "CXX_FUNCTIONAL_CAST_EXPR"}:
        children = _children(lhs)
        return _lvalue_target_decl(children[-1] if children else None)
    return None


def _inside_extent(inner: Any, outer: Any) -> bool:
    try:
        return (inner.extent.start.offset >= outer.extent.start.offset
                and inner.extent.end.offset <= outer.extent.end.offset)
    except Exception:
        return False


def _classify_access(node: Any, ancestors: list[Any]) -> str:
    """Classify a variable use without calling a pointer base variable a write target."""
    node_decl = _referenced(node)
    for ancestor in reversed(ancestors[-10:]):
        kind = _kind(ancestor)
        if kind == "UNARY_OPERATOR":
            text = _tokens(ancestor)
            if re.search(r"(^|\s)(\+\+|--)|\+\+$|--$", text):
                operand = _lvalue_target_decl(_children(ancestor)[-1] if _children(ancestor) else None)
                if operand is not None and node_decl is not None and _decl_usr(operand) == _decl_usr(node_decl):
                    return "READ_WRITE"
            if text.lstrip().startswith("&"):
                operand = _lvalue_target_decl(_children(ancestor)[-1] if _children(ancestor) else None)
                if operand is not None and node_decl is not None and _decl_usr(operand) == _decl_usr(node_decl):
                    return "POSSIBLE_WRITE"
        if kind in {"BINARY_OPERATOR", "COMPOUND_ASSIGNMENT_OPERATOR"}:
            try:
                tokens = list(ancestor.get_tokens())
            except Exception:
                tokens = []
            ops = ["=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>="]
            op_token = next((token for token in tokens if token.spelling in ops), None)
            if op_token is None:
                continue
            children = _children(ancestor)
            lhs = children[0] if children else None
            if lhs is not None and _inside_extent(node, lhs):
                target_decl = _lvalue_target_decl(lhs)
                if target_decl is not None and node_decl is not None and _decl_usr(target_decl) == _decl_usr(node_decl):
                    return "WRITE" if op_token.spelling == "=" else "READ_WRITE"
                # Pointer/index bases and subexpressions of a member lvalue are read
                # to compute the destination. The pointed-to memory event is separate.
                return "READ"
            return "READ"
        if kind == "CALL_EXPR" and ("&" + _tokens(node)) in _tokens(ancestor):
            return "POSSIBLE_WRITE"
    return "READ"

def _pattern_match(pattern: str, value: str) -> bool:
    try:
        return bool(re.search(pattern, value, flags=re.IGNORECASE))
    except re.error:
        return False


def _find_lambda_descendants(node: Any, root: Path, repository_id: str, lambda_cache: dict) -> list[dict[str, Any]]:
    result = []
    for sub in _walk(node):
        if _kind(sub) == "LAMBDA_EXPR":
            result.append(_lambda_record(sub, root, repository_id, lambda_cache))
    unique = {r["callable_id"]: r for r in result}
    return list(unique.values())


def _nearest_compound(ancestors: list[Any], line: int) -> tuple[int, int]:
    for ancestor in reversed(ancestors):
        if _kind(ancestor) == "COMPOUND_STMT":
            try:
                return int(ancestor.extent.start.line or line), int(ancestor.extent.end.line or line)
            except Exception:
                return line, line
    return line, line


def _target_from_expr(expr: Any, root: Path, repository_id: str) -> tuple[str, str, str]:
    text = _tokens(expr)
    refs = _referenced_variables(expr, root, repository_id)
    if refs:
        decl, vid = refs[0]
        name = base.qualified_name(decl) or getattr(decl, "spelling", "") or text
        return vid, name, text
    return "", "", text


def _build_argument_records(node: Any, function_id: str, root: Path, repository_id: str, config: dict[str, Any], lambda_cache: dict) -> list[dict[str, Any]]:
    args = _call_arguments(node)
    if not args:
        return []
    loc = _location(node, root)
    text = _tokens(node)
    call_site_id = _event_id("callsite", function_id, _kind(node), loc, text)
    callee_id, callee_name, callee_type = _callee_details(node, root, repository_id)
    callee_decl = _referenced(node)
    if callee_decl is None:
        for child in _children(node):
            ref = _referenced(child)
            if ref is not None and _kind(ref) in FUNCTION_REFERENCE_KINDS:
                callee_decl = ref
                break
    try:
        callee_params = list(callee_decl.get_arguments()) if callee_decl is not None else []
    except Exception:
        callee_params = [ch for ch in _children(callee_decl)] if callee_decl is not None else []
        callee_params = [ch for ch in callee_params if _kind(ch) == "PARM_DECL"]
    records = []
    for index, arg in enumerate(args):
        props = _type_properties(arg, config)
        ref_vars = _referenced_variables(arg, root, repository_id)
        refs = [base.qualified_name(d) or getattr(d, "spelling", "") for d, _ in ref_vars]
        ids = [vid for _, vid in ref_vars]
        arg_text = _tokens(arg)
        param_decl = callee_params[index] if index < len(callee_params) and _kind(callee_params[index]) == "PARM_DECL" else None
        param_is_project = False
        if param_decl is not None:
            try:
                param_is_project = bool(param_decl.location.file and base.should_export_file(Path(param_decl.location.file.name), root))
            except Exception:
                param_is_project = False
        param_id = _variable_id(param_decl, root, repository_id) if param_decl is not None and param_is_project else ""
        record = {
            "argument_id": _hash_id("arg", call_site_id, index, arg_text),
            "call_site_id": call_site_id,
            "caller_function_id": "" if function_id.startswith("lambda_") else function_id,
            "caller_callable_id": function_id if function_id.startswith("lambda_") else "",
            "callee_function_id": callee_id,
            "callee_name": callee_name,
            "call_line": loc["line"],
            "call_column": loc["column"],
            "argument_index": index,
            "target_parameter_id": param_id,
            "target_parameter_name": getattr(param_decl, "spelling", "") if param_decl is not None and param_is_project else "",
            "flow_kind": "ARGUMENT_TO_PARAMETER" if param_id else "ARGUMENT_TO_CALL_BOUNDARY",
            "argument_text": arg_text,
            "argument_type_id": _type_id_from_clang_type(getattr(arg, "type", None), repository_id),
            "argument_type": props["type_spelling"],
            "argument_canonical_type": props["canonical_type"],
            "referenced_variable_ids": "|".join(ids),
            "referenced_variable_names": "|".join(refs),
            "address_of": bool(arg_text.lstrip().startswith("&")),
            "uses_std_ref": "std :: ref" in arg_text or "std::ref" in arg_text,
            "uses_std_move": "std :: move" in arg_text or "std::move" in arg_text,
            "uses_std_forward": "std :: forward" in arg_text or "std::forward" in arg_text,
            "pointer_depth": props["pointer_depth"],
            "smart_pointer_kind": props["smart_pointer_kind"],
            "evidence_grade": "STATIC_AST",
        }
        records.append(record)
    return records


def _is_lvalue_write_context(node: Any, ancestors: list[Any]) -> bool:
    for ancestor in reversed(ancestors[-8:]):
        if _kind(ancestor) not in {"BINARY_OPERATOR", "COMPOUND_ASSIGNMENT_OPERATOR"}:
            continue
        try:
            tokens = list(ancestor.get_tokens())
        except Exception:
            tokens = []
        if not any(token.spelling in {"=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>="} for token in tokens):
            continue
        children = _children(ancestor)
        if children and _inside_extent(node, children[0]):
            return True
    return False


def extract_rca_evidence(cursor: Any, repository_root: Path, repository_id: str, config_path: str | Path | None = None) -> dict[str, list[dict[str, Any]]]:
    """Extract additive RCA evidence from one translation unit AST."""
    config_path = Path(config_path) if config_path else Path(__file__).with_name("thread_api_config.json")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    results: dict[str, list[dict[str, Any]]] = {key: [] for key in EVIDENCE_KEYS}
    variables_by_id: dict[str, dict[str, Any]] = {}
    functions = _function_ids(cursor, repository_root, repository_id)
    return_type_by_function = {fid: _function_return_type(rec["cursor"]) for fid, rec in functions.items()}
    lambda_cache: dict[tuple[str, int, int], dict[str, Any]] = {}
    macro_seen: set[tuple[str, int, int, str]] = set()
    type_seen: set[str] = set()
    type_nodes_by_id: dict[str, dict[str, Any]] = {}
    type_edges_by_id: dict[str, dict[str, Any]] = {}
    external_symbols_by_id: dict[str, dict[str, Any]] = {}

    def register_type(clang_type: Any, location: dict[str, Any] | None = None, depth: int = 0) -> str:
        if clang_type is None or depth > 5:
            return ""
        node_record, edge_record = _type_record_from_clang_type(clang_type, repository_root, repository_id, config, location)
        if node_record:
            type_id = str(node_record.get("type_id", ""))
            if type_id:
                existing = type_nodes_by_id.get(type_id)
                if existing is None or (not existing.get("relative_path") and node_record.get("relative_path")):
                    type_nodes_by_id[type_id] = node_record
                type_seen.add(type_id)
        if edge_record:
            edge_id = str(edge_record.get("relationship_id", ""))
            if edge_id:
                type_edges_by_id[edge_id] = edge_record
        try:
            _shape, nested_type, _relation = _type_shape(clang_type, config)
        except Exception:
            nested_type = None
        try:
            type_decl = clang_type.get_declaration()
            if type_decl is not None and _kind(type_decl) in {"TYPEDEF_DECL", "TYPE_ALIAS_DECL"}:
                nested_type = type_decl.underlying_typedef_type
        except Exception:
            pass
        if nested_type is not None and depth < 5:
            register_type(nested_type, location, depth + 1)
        return str(node_record.get("type_id", "")) if node_record else ""

    # Add regular function parameters and project-visible variables before visits,
    # so access and flow rows can refer to stable identifiers.
    for function_record in functions.values():
        fn = function_record["cursor"]
        try:
            params = list(fn.get_arguments())
        except Exception:
            params = [ch for ch in _children(fn) if _kind(ch) == "PARM_DECL"]
        for index, param in enumerate(params):
            rec = _variable_record(param, repository_root, repository_id, config, function_record["function_id"], function_record["qualified_name"])
            if not rec:
                continue
            variables_by_id[rec["variable_id"]] = rec
            register_type(getattr(param, "type", None), _location(param, repository_root))
            results["function_parameters"].append({
                "parameter_id": rec["variable_id"], "function_id": function_record["function_id"],
                "function_name": function_record["qualified_name"], "parameter_index": index,
                "parameter_name": rec["name"], "type_id": rec.get("type_id", ""), "type_spelling": rec["type_spelling"],
                "canonical_type": rec["canonical_type"], "pointer_depth": rec["pointer_depth"],
                "reference_kind": rec["reference_kind"], "smart_pointer_kind": rec["smart_pointer_kind"],
                "ownership_model_hint": rec["ownership_model_hint"], "is_const": rec["is_const"],
                "relative_path": rec["relative_path"], "line": rec["line"], "column": rec["column"],
            })

    # Model declared return types independently from individual return statements.
    for function_record in functions.values():
        fn = function_record["cursor"]
        try:
            return_type = fn.type.get_result()
        except Exception:
            return_type = None
        fn_loc = {key: function_record.get(key, 0 if key in {"line", "start_line", "end_line"} else "") for key in ("relative_path", "line", "start_line", "end_line")}
        return_type_id = register_type(return_type, fn_loc)
        props = _type_properties_from_type(return_type, config) if return_type is not None else {}
        results["function_return_types"].append({
            "function_id": function_record["function_id"], "function_name": function_record["qualified_name"],
            "return_type_id": return_type_id, "return_type_spelling": props.get("type_spelling", ""),
            "canonical_type": props.get("canonical_type", ""), "pointer_depth": props.get("pointer_depth", 0),
            "reference_kind": props.get("reference_kind", "NONE"), "smart_pointer_kind": props.get("smart_pointer_kind", "NONE"),
            "ownership_model_hint": props.get("ownership_model_hint", "NOT_POINTER_LIKE"),
            "relative_path": function_record.get("relative_path", ""), "line": function_record.get("line", 0),
            "evidence_grade": "CLANG_FUNCTION_TYPE_API",
        })

    # Process every project function body independently; nested lambda bodies get a
    # synthetic callable identity, not incorrectly assigned to the outer function.
    def visit_body(node: Any, current_function_id: str, current_function_name: str, ancestors: list[Any], root_node: Any, allow_nested_function: bool = False) -> None:
        kind = _kind(node)
        loc = _location(node, repository_root)
        if node is not root_node and kind in FUNCTION_KIND_NAMES and not allow_nested_function:
            return

        if kind == "LAMBDA_EXPR" and node is not root_node:
            lambda_rec = _lambda_record(node, repository_root, repository_id, lambda_cache)
            results["callable_targets"].append(lambda_rec)
            # These are lambda-body references. Do not call them confirmed captures:
            # the cursor walk can include body-local variables as well as captures.
            for decl, vid in _referenced_variables(node, repository_root, repository_id):
                variables_by_id.setdefault(vid, _variable_record(decl, repository_root, repository_id, config) or {})
                results["data_flows"].append({
                    "flow_id": _hash_id("flow", lambda_rec["callable_id"], "CAPTURE", vid, loc["line"]),
                    "flow_kind": "LAMBDA_BODY_REFERENCE", "source_variable_id": vid,
                    "target_callable_id": lambda_rec["callable_id"], "source_text": base.qualified_name(decl) or decl.spelling,
                    "target_text": lambda_rec["qualified_name"], "relative_path": loc["relative_path"], "line": loc["line"],
                    "evidence_grade": "STATIC_AST_SUBTREE_REFERENCE", "interpretation": "variable is referenced in lambda subtree; explicit capture mode and lifetime safety are not proven",
                })
            # Calls in the lambda are attributed to a synthetic lambda callable.
            for child in _children(node):
                visit_body(child, lambda_rec["callable_id"], lambda_rec["qualified_name"], ancestors + [node], node, True)
            return

        if kind in {"DECL_REF_EXPR", "MEMBER_REF_EXPR"}:
            decl = _referenced(node)
            if decl is not None and _kind(decl) in VARIABLE_KINDS:
                rec = _variable_record(decl, repository_root, repository_id, config, current_function_id, current_function_name)
                if rec:
                    variables_by_id.setdefault(rec["variable_id"], rec)
                    access_type = _classify_access(node, ancestors)
                    access_loc = _location(node, repository_root)
                    results["variable_accesses"].append({
                        "access_id": _event_id("access", current_function_id, access_type, access_loc, _tokens(node)),
                        "function_id": current_function_id, "function_name": current_function_name,
                        "variable_id": rec["variable_id"], "variable_name": rec["qualified_name"],
                        "declaration_kind": rec["declaration_kind"], "access_type": access_type,
                        "expression": _tokens(node), "relative_path": access_loc["relative_path"],
                        "line": access_loc["line"], "column": access_loc["column"],
                        "type_id": rec.get("type_id", ""),
                        "pointer_like": rec["is_pointer_like"], "smart_pointer_kind": rec["smart_pointer_kind"],
                        "evidence_grade": "STATIC_AST",
                    })

        if kind == "VAR_DECL":
            target_record = _variable_record(node, repository_root, repository_id, config, current_function_id, current_function_name)
            if target_record:
                variables_by_id.setdefault(target_record["variable_id"], target_record)
                target_id = target_record["variable_id"]
                target_props = target_record
                expression_text = _tokens(node)
                source_refs = [(decl, vid) for decl, vid in _referenced_variables(node, repository_root, repository_id) if vid != target_id]
                # Record initial alias/copy flows only when the source or destination
                # is pointer-like. std::move is handled by the dedicated move event below.
                if source_refs and "std :: move" not in expression_text and "std::move" not in expression_text:
                    source_decl, source_id = source_refs[-1]
                    source_rec = _variable_record(source_decl, repository_root, repository_id, config, current_function_id, current_function_name) or {}
                    pointer_like = bool(target_props.get("is_pointer_like") or source_rec.get("is_pointer_like"))
                    if pointer_like:
                        smart_copy = target_props.get("smart_pointer_kind") not in {"NONE", ""} or source_rec.get("smart_pointer_kind") not in {"NONE", ""}
                        flow_kind = "SMART_POINTER_COPY_INITIALIZATION" if smart_copy else "POINTER_ALIAS_INITIALIZATION"
                        results["data_flows"].append({
                            "flow_id": _hash_id("flow", current_function_id, flow_kind, loc, source_id, target_id),
                            "flow_kind": flow_kind, "source_variable_id": source_id, "target_variable_id": target_id,
                            "source_text": base.qualified_name(source_decl) or source_decl.spelling,
                            "target_text": base.qualified_name(node) or node.spelling,
                            "relative_path": loc["relative_path"], "line": loc["line"],
                            "evidence_grade": "STATIC_AST_INITIALIZER_REFERENCE",
                            "interpretation": "initialization references an existing pointer-like variable; raw aliasing or shared ownership semantics depend on the resolved type and constructor",
                        })
                        results["pointer_events"].append({
                            "event_id": _event_id("ptr", current_function_id, flow_kind, loc, expression_text),
                            "function_id": current_function_id, "kind": "SMART_POINTER_COPY" if smart_copy else "POINTER_ALIAS_INITIALIZATION",
                            "target_id": target_id, "target_name": base.qualified_name(node) or node.spelling,
                            "source_id": source_id, "source_name": base.qualified_name(source_decl) or source_decl.spelling,
                            "target_type_id": target_record.get("type_id", ""), "source_type_id": source_rec.get("type_id", ""),
                            "expression": expression_text, "line": loc["line"], "column": loc["column"],
                            "relative_path": loc["relative_path"], "evidence_grade": "STATIC_AST_INITIALIZER_REFERENCE",
                        })

        if kind in {"CALL_EXPR", "CXX_CONSTRUCT_EXPR"}:
            args_records = _build_argument_records(node, current_function_id, repository_root, repository_id, config, lambda_cache)
            results["call_arguments"].extend(args_records)
            for argument in _call_arguments(node):
                register_type(getattr(argument, "type", None), _location(argument, repository_root))
            for arg_record in args_records:
                source_ids = [x for x in str(arg_record.get("referenced_variable_ids", "")).split("|") if x]
                source_names = [x for x in str(arg_record.get("referenced_variable_names", "")).split("|") if x]
                target_param = str(arg_record.get("target_parameter_id", ""))
                if target_param:
                    # Use one source ID per flow row. A pipe-joined value is useful
                    # as an argument summary but must never masquerade as a graph node ID.
                    for source_index, source_id in enumerate(source_ids):
                        source_name = source_names[source_index] if source_index < len(source_names) else arg_record["argument_text"]
                        results["data_flows"].append({
                            "flow_id": _hash_id("flow", arg_record["call_site_id"], arg_record["argument_index"], target_param, source_id),
                            "flow_kind": "ARGUMENT_TO_PARAMETER", "source_variable_id": source_id,
                            "target_variable_id": target_param, "source_text": source_name,
                            "target_text": arg_record["target_parameter_name"],
                            "relative_path": _location(node, repository_root)["relative_path"],
                            "line": arg_record["call_line"], "evidence_grade": "STATIC_AST_POSITIONAL_BINDING",
                            "interpretation": "static argument-to-parameter positional evidence; expression can reference multiple variables, and templates/overloads can limit certainty",
                        })
            expression = _tokens(node)
            callee_id, callee_name, callee_type = _callee_details(node, repository_root, repository_id)
            lower_name = (callee_name + " " + callee_type + " " + expression + " " + re.sub(r"\s+", "", expression)).lower()
            call_loc = _location(node, repository_root)
            call_site_id = _event_id("callsite", current_function_id, kind, call_loc, expression)
            callee_decl = _callee_decl(node)
            external_record = None
            callee_external_id = ""
            if callee_decl is not None:
                try:
                    decl_file = callee_decl.location.file
                    is_project_decl = bool(decl_file and base.should_export_file(Path(decl_file.name), repository_root))
                except Exception:
                    is_project_decl = False
                if not is_project_decl:
                    external_record = _external_symbol_record(callee_decl, repository_id)
                    if external_record:
                        callee_external_id = external_record["symbol_id"]
                        external_symbols_by_id[callee_external_id] = external_record
            callee_variable_id = ""
            if not callee_decl:
                direct_refs = _referenced_variables(node, repository_root, repository_id)
                for variable_decl, variable_id in direct_refs:
                    variable_type = _type_properties(variable_decl, config)
                    if variable_type.get("is_function_pointer"):
                        callee_variable_id = variable_id
                        break
            if callee_id:
                resolution_status = "RESOLVED_PROJECT"
                callee_node_id = callee_id
            elif callee_external_id:
                resolution_status = "RESOLVED_EXTERNAL"
                callee_node_id = callee_external_id
            elif callee_variable_id:
                resolution_status = "RESOLVED_FUNCTION_POINTER_VARIABLE"
                callee_node_id = callee_variable_id
            else:
                resolution_status = "UNRESOLVED"
                callee_node_id = ""
            results["call_sites"].append({
                "call_site_id": call_site_id,
                "caller_function_id": "" if current_function_id.startswith("lambda_") else current_function_id,
                "caller_callable_id": current_function_id if current_function_id.startswith("lambda_") else "",
                "callee_node_id": callee_node_id, "callee_function_id": callee_id,
                "callee_external_symbol_id": callee_external_id, "callee_variable_id": callee_variable_id,
                "callee_name": callee_name, "callee_signature": callee_type,
                "call_type": "CONSTRUCTION" if kind == "CXX_CONSTRUCT_EXPR" else "CALL",
                "resolution_status": resolution_status, "call_expression": expression,
                "line": call_loc["line"], "column": call_loc["column"],
                "relative_path": call_loc["relative_path"], "evidence_grade": "STATIC_AST_CALL_SITE",
            })

            # Calls from a lambda callable are also retained as explicit edges.
            if current_function_id.startswith("lambda_") and callee_id:
                results["callable_calls"].append({
                    "call_site_id": call_site_id, "caller_callable_id": current_function_id,
                    "callee_function_id": callee_id, "callee_name": callee_name,
                    "relative_path": call_loc["relative_path"], "line": call_loc["line"], "column": call_loc["column"],
                    "call_expression": expression, "resolution_status": "RESOLVED_DECLARATION",
                })
                for rec in results["callable_targets"]:
                    if rec["callable_id"] == current_function_id and callee_name not in rec["body_function_names"]:
                        rec["body_function_names"].append(callee_name)

            # Config-driven spawn detection supports constructors and regular calls.
            for rule in config.get("spawn_apis", []):
                if rule.get("node_kinds") and kind not in rule["node_kinds"]:
                    continue
                if not _pattern_match(rule.get("match_regex", "(?!)"), lower_name):
                    continue
                args = _call_arguments(node)
                target_indices = [int(x) for x in rule.get("target_arg_indices", [rule.get("target_arg_index", 0)])]
                if rule.get("api") == "std::async" and args:
                    first_arg_text = _tokens(args[0])
                    # The standard overloads are async(callable, ...) and
                    # async(launch_policy, callable, ...). Do not mistake a
                    # function pointer passed as a callable's argument for target.
                    target_indices = [1] if re.search(r"std\s*::\s*launch|launch\s*::", first_arg_text) else [0]
                candidate_targets = []
                for candidate_index in target_indices:
                    if candidate_index < 0 or candidate_index >= len(args):
                        continue
                    candidate_expr = args[candidate_index]
                    candidate_lambdas = _find_lambda_descendants(candidate_expr, repository_root, repository_id, lambda_cache)
                    candidate_functions = _referenced_functions(candidate_expr, repository_root, repository_id)
                    candidate_text = _tokens(candidate_expr)
                    score = 2 if candidate_lambdas or candidate_functions else 0
                    if rule.get("api") == "std::async" and re.search(r"std\s*::\s*launch|launch\s*::", candidate_text):
                        score -= 3
                    candidate_targets.append((score, candidate_index, candidate_expr, candidate_lambdas, candidate_functions))
                if not candidate_targets:
                    continue
                # Function and lambda expressions outrank launch-policy or generic arguments.
                candidate_targets.sort(key=lambda item: (-item[0], item[1]))
                _, target_index, target_expr, lambda_targets, function_targets = candidate_targets[0]
                arg_texts = [_tokens(arg) for arg in args]
                target_vars = _referenced_variables(target_expr, repository_root, repository_id)
                target_text = _tokens(target_expr)
                target_id = ""
                target_external_id = ""
                target_kind = "UNRESOLVED"
                target_name = target_text
                target_decl = None
                if lambda_targets:
                    lambda_rec = lambda_targets[0]
                    target_id = lambda_rec["callable_id"]
                    target_kind = "LAMBDA"
                    body_names = lambda_rec.get("body_function_names", [])
                    if body_names:
                        target_name = "lambda->" + body_names[0]
                    else:
                        target_name = lambda_rec["qualified_name"]
                elif function_targets:
                    target_decl, target_id = function_targets[0]
                    target_name = base.qualified_name(target_decl) or target_decl.spelling
                    try:
                        target_file = target_decl.location.file
                        target_is_project = bool(target_file and base.should_export_file(Path(target_file.name), repository_root))
                    except Exception:
                        target_is_project = False
                    if target_is_project:
                        target_kind = "FUNCTION_REF"
                    else:
                        external_record = _external_symbol_record(target_decl, repository_id)
                        if external_record:
                            external_symbols_by_id[external_record["symbol_id"]] = external_record
                            target_external_id = external_record["symbol_id"]
                            target_id = target_external_id
                            target_kind = "EXTERNAL_FUNCTION_REF"
                        else:
                            target_id = ""
                            target_kind = "UNRESOLVED"
                if rule.get("target_kind_override"):
                    # A wrapper rule labels direct function references as wrapper
                    # arguments, but a lambda remains a Callable and must not be
                    # relabelled as a Function merely because it crosses a wrapper.
                    if target_kind == "FUNCTION_REF" and target_id:
                        target_kind = rule["target_kind_override"]
                    elif not target_id:
                        target_kind = "UNRESOLVED"
                start_options = rule.get("user_args_start_index_options", [])
                if start_options:
                    # Pair policy-bearing async overload (target index 1) with user args from 2.
                    extra_start = int(start_options[1] if target_index == 1 and len(start_options) > 1 else start_options[0])
                else:
                    extra_start = int(rule.get("user_args_start_index", target_index + 1))
                if rule.get("api") in {"std::thread", "std::jthread"} and "::" in target_text and ("&" in target_text or "::*" in target_text):
                    # Member-function thread constructors carry a separate object argument.
                    extra_start = max(extra_start, target_index + 2)
                extra_end = min(len(args), extra_start + int(rule["user_args_count"])) if rule.get("user_args_count") is not None else len(args)
                passed_args = arg_texts[extra_start:extra_end] if extra_start < len(arg_texts) else []
                extra_refs = []
                if target_decl is not None:
                    try:
                        target_params = list(target_decl.get_arguments())
                    except Exception:
                        target_params = [ch for ch in _children(target_decl) if _kind(ch) == "PARM_DECL"]
                elif target_kind == "LAMBDA":
                    target_lambda_node = next((sub for sub in _walk(target_expr) if _kind(sub) == "LAMBDA_EXPR"), None)
                    target_params = [ch for ch in _children(target_lambda_node) if _kind(ch) == "PARM_DECL"] if target_lambda_node is not None else []
                else:
                    target_params = []
                for arg_index in range(extra_start, extra_end):
                    arg = args[arg_index]
                    extra_refs.extend(name for name in (base.qualified_name(d) or d.spelling for d, _ in _referenced_variables(arg, repository_root, repository_id)))
                    if target_decl is not None:
                        target_param_index = arg_index - extra_start
                        if target_param_index < len(target_params) and _kind(target_params[target_param_index]) == "PARM_DECL":
                            param = target_params[target_param_index]
                            try:
                                is_project_param = bool(param.location.file and base.should_export_file(Path(param.location.file.name), repository_root))
                            except Exception:
                                is_project_param = False
                            if is_project_param:
                                param_id = _variable_id(param, repository_root, repository_id)
                                for src_decl, src_id in _referenced_variables(arg, repository_root, repository_id):
                                    results["data_flows"].append({
                                        "flow_id": _hash_id("flow", current_function_id, "SPAWN_ARGUMENT_TO_PARAMETER", call_loc["relative_path"], call_loc["line"], src_id, param_id),
                                        "flow_kind": "SPAWN_ARGUMENT_TO_PARAMETER", "source_variable_id": src_id,
                                        "target_variable_id": param_id, "source_text": base.qualified_name(src_decl) or src_decl.spelling,
                                        "target_text": base.qualified_name(param) or param.spelling,
                                        "relative_path": call_loc["relative_path"], "line": call_loc["line"],
                                        "evidence_grade": "STATIC_AST_CONFIG_RULE", "interpretation": "explicit thread argument mapped positionally to target parameter; alias lifetime is not proven",
                                    })
                results["thread_spawns"].append({
                    "spawn_id": _event_id("spawn", current_function_id, rule.get("api", ""), call_loc, expression),
                    "spawner_function_id": "" if current_function_id.startswith("lambda_") else current_function_id,
                    "spawner_callable_id": current_function_id if current_function_id.startswith("lambda_") else "",
                    "spawner_function_name": current_function_name,
                    "api": rule.get("api", ""), "spawn_kind": rule.get("spawn_kind", "THREAD_CREATE"),
                    "target_function_id": target_id if target_kind in {"FUNCTION_REF", "WRAPPER_ARG"} else "",
                    "target_callable_id": target_id if target_kind == "LAMBDA" else "",
                    "target_external_symbol_id": target_external_id,
                    "target_variable_id": (target_vars[0][1] if target_vars and target_kind == "UNRESOLVED" and _type_properties(target_vars[0][0], config).get("is_function_pointer") else ""),
                    "target_kind": ("FUNCTION_POINTER_VARIABLE" if target_kind == "UNRESOLVED" and target_vars and _type_properties(target_vars[0][0], config).get("is_function_pointer") else target_kind),
                    "target_text": target_name,
                    "target_expression": target_text, "target_body_calls": "|".join(lambda_targets[0].get("body_function_names", [])) if lambda_targets else "",
                    "args_text": ", ".join(passed_args), "argument_variable_names": "|".join(extra_refs),
                    "line": call_loc["line"], "column": call_loc["column"], "relative_path": call_loc["relative_path"],
                    "resolution_status": "RESOLVED_EXTERNAL" if target_external_id else ("RESOLVED" if target_id else "UNRESOLVED"),
                    "evidence_grade": "STATIC_AST_CONFIG_RULE",
                })
                break

            # Lifetime-changing or ownership-related APIs. This list is semantic
            # interpretation of AST call spelling, deliberately tagged as a hint.
            operation = ""
            simple_name = callee_name.split("::")[-1].split(".")[-1]
            if simple_name in {"reset"}:
                operation = "RESET"
            elif simple_name == "release":
                operation = "RELEASE"
            elif simple_name == "clear":
                operation = "CLEAR"
            elif simple_name == "erase":
                operation = "ERASE"
            elif simple_name in {"free", "cfree"}:
                operation = "FREE"
            elif simple_name in {"malloc", "calloc", "realloc", "aligned_alloc", "operator new", "operator new[]"}:
                operation = "REALLOCATE" if simple_name == "realloc" else "ALLOCATE_C_OR_OPERATOR_NEW"
            elif simple_name == "move":
                operation = "MOVE"
            elif simple_name == "forward":
                operation = "FORWARD"
            elif simple_name in {"make_unique", "make_shared", "make_unique_for_overwrite", "make_shared_for_overwrite"}:
                operation = "MAKE_UNIQUE" if "unique" in simple_name else "MAKE_SHARED"
            elif simple_name in {"lock", "expired", "use_count", "get", "weak_from_this", "shared_from_this"} and ("ptr" in lower_name or "pointer" in lower_name or "weak" in lower_name or "shared" in lower_name or "unique" in lower_name):
                operation = "SMART_POINTER_" + simple_name.upper()
            if operation:
                target_decl = None
                target_id = ""
                if operation in {"MAKE_UNIQUE", "MAKE_SHARED"}:
                    for ancestor in reversed(ancestors[-6:]):
                        if _kind(ancestor) == "VAR_DECL":
                            target_decl = ancestor
                            break
                if target_decl is not None:
                    target_id = _variable_id(target_decl, repository_root, repository_id)
                else:
                    refs = _referenced_variables(node, repository_root, repository_id)
                    target_decl, target_id = _pick_target_variable(refs)
                target_name = base.qualified_name(target_decl) if target_decl is not None else ""
                if operation == "MOVE":
                    source_refs = _referenced_variables(node, repository_root, repository_id)
                    source_decl, source_id = _pick_target_variable(source_refs)
                    destination_decl = target_decl
                    # A move construction has destination VAR_DECL; a move assignment
                    # may have a nearby assignment LHS in its ancestors.
                    if destination_decl is None:
                        for ancestor in reversed(ancestors[-6:]):
                            if _kind(ancestor) == "BINARY_OPERATOR":
                                left_refs = _referenced_variable_uses(ancestor, repository_root, repository_id)
                                if left_refs:
                                    destination_decl = left_refs[0][1]
                                    break
                    if destination_decl is not None and source_decl is not None:
                        destination_id = _variable_id(destination_decl, repository_root, repository_id)
                        results["data_flows"].append({
                            "flow_id": _event_id("flow", current_function_id, "MOVE_CONSTRUCTION", call_loc, expression),
                            "flow_kind": "MOVE_CONSTRUCTION", "source_variable_id": source_id,
                            "target_variable_id": destination_id, "source_text": base.qualified_name(source_decl) or source_decl.spelling,
                            "target_text": base.qualified_name(destination_decl) or destination_decl.spelling,
                            "relative_path": call_loc["relative_path"], "line": call_loc["line"],
                            "evidence_grade": "STATIC_AST_HEURISTIC", "interpretation": "std::move expression; actual move/ownership transfer depends on selected overload and object type",
                        })
                results["lifetime_events"].append({
                    "event_id": _event_id("life", current_function_id, operation, call_loc, expression),
                    "function_id": current_function_id, "function_name": current_function_name, "target_kind": _declaration_kind(target_decl) if target_decl is not None else "UNKNOWN",
                    "target_id": target_id, "target_name": target_name, "kind": operation,
                    "line": call_loc["line"], "column": call_loc["column"], "expression": expression,
                    "relative_path": call_loc["relative_path"], "event_semantics": "AST call-name classification; target binding may be approximate",
                    "evidence_grade": "STATIC_AST_HEURISTIC",
                })

            # Synchronization calls and configurable direct lock APIs.
            for rule in config.get("lock_apis", []):
                if _pattern_match(rule.get("match_regex", "(?!)"), lower_name):
                    refs = _referenced_variables(node, repository_root, repository_id)
                    mutex_decl, mutex_id = _pick_target_variable(refs)
                    results["lock_usage"].append({
                        "lock_event_id": _event_id("lock", current_function_id, rule.get("event", ""), call_loc, expression),
                        "function_id": current_function_id, "function_name": current_function_name,
                        "mutex_id": mutex_id, "mutex_text": base.qualified_name(mutex_decl) if mutex_decl is not None else expression,
                        "kind": rule.get("kind", "API_LOCK"), "event": rule.get("event", "UNKNOWN"),
                        "line": call_loc["line"], "scope_start_line": call_loc["line"], "scope_end_line": call_loc["line"],
                        "relative_path": call_loc["relative_path"], "expression": expression,
                        "scope_semantics": "explicit lock API; lifetime requires paired control-flow inspection",
                        "evidence_grade": "STATIC_AST_CONFIG_RULE",
                    })
                    results["synchronization_events"].append({
                        "event_id": _event_id("sync", current_function_id, rule.get("event", ""), call_loc, expression),
                        "function_id": current_function_id, "kind": rule.get("event", "UNKNOWN"),
                        "primitive": rule.get("kind", "API_LOCK"), "target_id": mutex_id,
                        "target_name": base.qualified_name(mutex_decl) if mutex_decl is not None else expression,
                        "line": call_loc["line"], "column": call_loc["column"], "relative_path": call_loc["relative_path"],
                        "expression": expression, "evidence_grade": "STATIC_AST_CONFIG_RULE",
                    })
                    break
            for rule in config.get("synchronization_apis", []):
                if _pattern_match(rule.get("match_regex", "(?!)"), lower_name):
                    call_children = _children(node)
                    receiver_refs = _referenced_variables(call_children[0], repository_root, repository_id) if call_children else []
                    sync_refs = receiver_refs or _referenced_variables(node, repository_root, repository_id)
                    sync_target_decl, sync_target_id = _pick_target_variable(sync_refs)
                    results["synchronization_events"].append({
                        "event_id": _event_id("sync", current_function_id, rule.get("kind", ""), call_loc, expression),
                        "function_id": current_function_id, "kind": rule.get("kind", "UNKNOWN"),
                        "primitive": rule.get("api", ""), "target_id": sync_target_id, "target_name": base.qualified_name(sync_target_decl) if sync_target_decl is not None else expression,
                        "line": call_loc["line"], "column": call_loc["column"], "relative_path": call_loc["relative_path"],
                        "expression": expression, "evidence_grade": "STATIC_AST_CONFIG_RULE",
                    })
                    break

            # Atomic operations are marked as operation sites, not automatically
            # classified as races or as sufficient synchronization.
            atomic_ops = {"load": "ATOMIC_READ", "is_lock_free": "ATOMIC_QUERY", "store": "ATOMIC_WRITE", "exchange": "ATOMIC_RMW", "fetch_add": "ATOMIC_RMW", "fetch_sub": "ATOMIC_RMW", "fetch_and": "ATOMIC_RMW", "fetch_or": "ATOMIC_RMW", "fetch_xor": "ATOMIC_RMW", "compare_exchange_weak": "ATOMIC_COMPARE_EXCHANGE", "compare_exchange_strong": "ATOMIC_COMPARE_EXCHANGE", "wait": "ATOMIC_WAIT", "notify_one": "ATOMIC_NOTIFY_ONE", "notify_all": "ATOMIC_NOTIFY_ALL"}
            if simple_name in atomic_ops:
                refs = _referenced_variables(node, repository_root, repository_id)
                target_decl, target_id = refs[0] if refs else (None, "")
                results["synchronization_events"].append({
                    "event_id": _event_id("sync", current_function_id, atomic_ops[simple_name], call_loc, expression),
                    "function_id": current_function_id, "kind": atomic_ops[simple_name], "primitive": "std::atomic-like method name",
                    "target_id": target_id, "target_name": base.qualified_name(target_decl) if target_decl is not None else expression,
                    "line": call_loc["line"], "column": call_loc["column"], "relative_path": call_loc["relative_path"],
                    "expression": expression, "memory_order_text": next((tok for tok in expression.split() if tok.startswith("memory_order")), ""),
                    "evidence_grade": "STATIC_AST_HEURISTIC",
                })

        if kind in {"CXX_NEW_EXPR", "CXX_DELETE_EXPR"}:
            op = "NEW" if kind == "CXX_NEW_EXPR" else "DELETE"
            target_decl = None
            target_id = ""
            target_name = ""
            # delete/new within an assignment or declaration: nearest left/decl
            for ancestor in reversed(ancestors[-5:]):
                if _kind(ancestor) == "VAR_DECL" and op == "NEW":
                    target_decl = ancestor
                    break
                if _kind(ancestor) in {"BINARY_OPERATOR", "COMPOUND_ASSIGNMENT_OPERATOR"} and op == "NEW":
                    refs = _referenced_variables(ancestor, repository_root, repository_id)
                    if refs:
                        target_decl = refs[0][0]
                        break
            if target_decl is None:
                refs = _referenced_variables(node, repository_root, repository_id)
                if refs:
                    target_decl = refs[0][0]
            if target_decl is not None:
                target_id = _variable_id(target_decl, repository_root, repository_id)
                target_name = base.qualified_name(target_decl) or target_decl.spelling
                rec = _variable_record(target_decl, repository_root, repository_id, config, current_function_id, current_function_name)
                if rec:
                    variables_by_id.setdefault(target_id, rec)
            life_loc = _location(node, repository_root)
            results["lifetime_events"].append({
                "event_id": _event_id("life", current_function_id, op, life_loc, _tokens(node)),
                "function_id": current_function_id, "function_name": current_function_name, "target_kind": _declaration_kind(target_decl) if target_decl is not None else "UNKNOWN",
                "target_id": target_id, "target_name": target_name, "kind": op,
                "line": life_loc["line"], "column": life_loc["column"], "expression": _tokens(node),
                "relative_path": life_loc["relative_path"], "event_semantics": "explicit C++ new/delete expression",
                "evidence_grade": "STATIC_AST",
            })
            results["pointer_events"].append({
                "event_id": _event_id("ptr", current_function_id, op, life_loc, _tokens(node)),
                "function_id": current_function_id, "kind": "ALLOCATION" if op == "NEW" else "DEALLOCATION",
                "target_id": target_id, "target_name": target_name,
                "target_type_id": _type_id_from_clang_type(getattr(target_decl, "type", None), repository_id) if target_decl is not None else "",
                "source_type_id": "", "expression": _tokens(node),
                "line": life_loc["line"], "column": life_loc["column"], "relative_path": life_loc["relative_path"],
                "evidence_grade": "STATIC_AST",
            })

        if kind == "UNARY_OPERATOR":
            expr = _tokens(node)
            op_kind = ""
            if expr.lstrip().startswith("*"):
                op_kind = "POINTER_DEREFERENCE_WRITE" if _is_lvalue_write_context(node, ancestors) else "POINTER_DEREFERENCE"
            elif expr.lstrip().startswith("&"):
                op_kind = "ADDRESS_OF"
            elif "++" in expr or "--" in expr:
                op_kind = "INCREMENT_OR_DECREMENT"
            if op_kind:
                refs = _referenced_variables(node, repository_root, repository_id)
                decl, vid = refs[0] if refs else (None, "")
                results["pointer_events"].append({
                    "event_id": _event_id("ptr", current_function_id, op_kind, loc, expr),
                    "function_id": current_function_id, "kind": op_kind,
                    "target_id": vid, "target_name": base.qualified_name(decl) if decl is not None else expr,
                    "target_type_id": _type_id_from_clang_type(getattr(decl, "type", None), repository_id) if decl is not None else "", "source_type_id": "",
                    "expression": expr, "line": loc["line"], "column": loc["column"], "relative_path": loc["relative_path"],
                    "evidence_grade": "STATIC_AST_SYNTAX",
                })

        if kind == "ARRAY_SUBSCRIPT_EXPR":
            expr = _tokens(node)
            refs = _referenced_variables(node, repository_root, repository_id)
            decl, vid = refs[0] if refs else (None, "")
            index_kind = "INDEXED_WRITE" if _is_lvalue_write_context(node, ancestors) else "INDEXED_ACCESS"
            results["pointer_events"].append({
                "event_id": _event_id("ptr", current_function_id, index_kind, loc, expr),
                "function_id": current_function_id, "kind": index_kind,
                "target_id": vid, "target_name": base.qualified_name(decl) if decl is not None else expr,
                "target_type_id": _type_id_from_clang_type(getattr(decl, "type", None), repository_id) if decl is not None else "", "source_type_id": "",
                "expression": expr, "line": loc["line"], "column": loc["column"], "relative_path": loc["relative_path"],
                "evidence_grade": "STATIC_AST_SYNTAX",
            })

        if kind == "MEMBER_REF_EXPR":
            expr = _tokens(node)
            if "->" in expr:
                refs = _referenced_variables(node, repository_root, repository_id)
                decl, vid = refs[-1] if refs else (None, "")
                arrow_kind = "ARROW_FIELD_WRITE" if _is_lvalue_write_context(node, ancestors) else "ARROW_DEREFERENCE"
                results["pointer_events"].append({
                    "event_id": _event_id("ptr", current_function_id, arrow_kind, loc, expr),
                    "function_id": current_function_id, "kind": arrow_kind,
                    "target_id": vid, "target_name": base.qualified_name(decl) if decl is not None else expr,
                    "target_type_id": _type_id_from_clang_type(getattr(decl, "type", None), repository_id) if decl is not None else "", "source_type_id": "",
                    "expression": expr, "line": loc["line"], "column": loc["column"], "relative_path": loc["relative_path"],
                    "evidence_grade": "STATIC_AST_SYNTAX",
                })

        if kind == "IF_STMT":
            children = _children(node)
            condition_cursor = next((child for child in children if _kind(child).endswith("_EXPR") or _kind(child) in {"BINARY_OPERATOR", "UNARY_OPERATOR", "CXX_BOOL_LITERAL_EXPR"}), children[0] if children else node)
            expr = _tokens(condition_cursor)
            refs = _referenced_variables(condition_cursor, repository_root, repository_id)
            has_null_literal = bool(re.search(r"(nullptr|NULL|\b0\b)", expr) and re.search(r"(!=|==|!\s*\w+|\.get\s*\()", expr))
            pointer_refs = [(decl, vid) for decl, vid in refs if _type_properties(decl, config)["is_pointer_like"]]
            is_negated_pointer = expr.lstrip().startswith("!") and bool(pointer_refs)
            uses_get = ". get (" in expr or ".get (" in expr or ".get(" in re.sub(r"\s+", "", expr)
            has_comparison = bool(re.search(r"(==|!=|<=|>=|(?<![=!<>])[<>](?![=]))", expr))
            pointer_truthiness = bool(pointer_refs) and not has_comparison and not re.search(r"(?<![=!<>])[+*/%](?![=])", expr)
            if has_null_literal or is_negated_pointer or uses_get or pointer_truthiness:
                for decl, vid in refs:
                    props = _type_properties(decl, config)
                    if has_null_literal or (props["is_pointer_like"] and (is_negated_pointer or uses_get or pointer_truthiness)):
                        event_kind = "NULL_CHECK" if has_null_literal or is_negated_pointer or uses_get else "POINTER_PRESENCE_TEST"
                        results["control_flow_events"].append({
                            "event_id": _event_id("cf", current_function_id, event_kind, loc, expr + vid),
                            "function_id": current_function_id, "function_name": current_function_name, "kind": event_kind, "target_id": vid,
                            "target_name": base.qualified_name(decl) or decl.spelling,
                            "line": loc["line"], "column": loc["column"], "relative_path": loc["relative_path"],
                            "condition_text": expr, "evidence_grade": "STATIC_AST_HEURISTIC",
                        })

        if kind in {"BINARY_OPERATOR", "COMPOUND_ASSIGNMENT_OPERATOR"}:
            expression = _tokens(node)
            try:
                op_token = next((token for token in node.get_tokens() if token.spelling in {"=", "+=", "-=", "*=", "/=", "%="}), None)
            except Exception:
                op_token = None
            if op_token is not None:
                ref_uses = _referenced_variable_uses(node, repository_root, repository_id)
                try:
                    op_offset = op_token.extent.start.offset
                    left = [(d, vid) for use, d, vid in ref_uses if use.extent.start.offset <= op_offset]
                    right = [(d, vid) for use, d, vid in ref_uses if use.extent.start.offset > op_offset]
                except Exception:
                    refs = _referenced_variables(node, repository_root, repository_id)
                    left, right = refs[:1], refs[1:]
                if left and right:
                    target_decl, target_id = left[0]
                    source_decl, source_id = right[0]
                    target_rec = _variable_record(target_decl, repository_root, repository_id, config, current_function_id, current_function_name) or {}
                    source_rec = _variable_record(source_decl, repository_root, repository_id, config, current_function_id, current_function_name) or {}
                    for rec in (target_rec, source_rec):
                        if rec.get("variable_id"):
                            variables_by_id.setdefault(rec["variable_id"], rec)
                    pointer_assignment = bool(target_rec.get("is_pointer_like") or source_rec.get("is_pointer_like"))
                    results["data_flows"].append({
                        "flow_id": _event_id("flow", current_function_id, "POINTER_ALIAS_ASSIGNMENT" if pointer_assignment else "ASSIGNMENT", loc, expression),
                        "flow_kind": "POINTER_ALIAS_ASSIGNMENT" if pointer_assignment else "ASSIGNMENT",
                        "source_variable_id": source_id, "target_variable_id": target_id,
                        "source_text": base.qualified_name(source_decl) or source_decl.spelling,
                        "target_text": base.qualified_name(target_decl) or target_decl.spelling,
                        "relative_path": loc["relative_path"], "line": loc["line"],
                        "evidence_grade": "STATIC_AST_POSITIONAL_ASSIGNMENT",
                        "interpretation": "syntax-level assignment; does not prove complete alias/lifetime flow",
                    })
                    if pointer_assignment:
                        results["pointer_events"].append({
                            "event_id": _event_id("ptr", current_function_id, "POINTER_REASSIGNMENT", loc, expression),
                            "function_id": current_function_id, "kind": "POINTER_REASSIGNMENT",
                            "target_id": target_id, "target_name": base.qualified_name(target_decl) or target_decl.spelling,
                            "source_id": source_id, "source_name": base.qualified_name(source_decl) or source_decl.spelling,
                            "target_type_id": target_rec.get("type_id", ""), "source_type_id": source_rec.get("type_id", ""),
                            "expression": expression, "line": loc["line"], "column": loc["column"],
                            "relative_path": loc["relative_path"], "evidence_grade": "STATIC_AST_HEURISTIC",
                        })
                elif op_token.spelling == "=":
                    children = _children(node)
                    left_child = children[0] if children else None
                    left_decl = _referenced(left_child) if left_child is not None and _kind(left_child) in {"DECL_REF_EXPR", "MEMBER_REF_EXPR"} else None
                    if left_decl is not None and _kind(left_decl) in VARIABLE_KINDS:
                        left_props = _type_properties(left_decl, config)
                        if left_props.get("is_pointer_like") and re.search(r"(nullptr|NULL|\b0\b)", expression):
                            left_id = _variable_id(left_decl, repository_root, repository_id)
                            results["pointer_events"].append({
                                "event_id": _event_id("ptr", current_function_id, "POINTER_NULLING", loc, expression),
                                "function_id": current_function_id, "kind": "POINTER_NULLING",
                                "target_id": left_id, "target_name": base.qualified_name(left_decl) or left_decl.spelling,
                                "source_id": "", "source_name": "", "target_type_id": _type_id_from_clang_type(getattr(left_decl, "type", None), repository_id),
                                "source_type_id": "", "expression": expression, "line": loc["line"], "column": loc["column"],
                                "relative_path": loc["relative_path"], "evidence_grade": "STATIC_AST_NULL_ASSIGNMENT",
                            })

        if kind == "BINARY_OPERATOR":
            # Pointer arithmetic is a binary + or - expression, not an assignment.
            try:
                operator_tokens = [token for token in node.get_tokens() if token.spelling in {"+", "-"}]
            except Exception:
                operator_tokens = []
            if operator_tokens:
                uses = _referenced_variable_uses(node, repository_root, repository_id)
                if len(uses) >= 2:
                    left = (uses[0][1], uses[0][2])
                    right = (uses[-1][1], uses[-1][2])
                    left_props = _type_properties(left[0], config)
                    right_props = _type_properties(right[0], config)
                    if left_props.get("pointer_depth", 0) > 0 or right_props.get("pointer_depth", 0) > 0:
                        results["pointer_events"].append({
                            "event_id": _event_id("ptr", current_function_id, "POINTER_ARITHMETIC", loc, _tokens(node)),
                            "function_id": current_function_id, "kind": "POINTER_ARITHMETIC",
                            "target_id": left[1], "target_name": base.qualified_name(left[0]) or left[0].spelling,
                            "source_id": right[1], "source_name": base.qualified_name(right[0]) or right[0].spelling,
                            "target_type_id": _type_id_from_clang_type(getattr(left[0], "type", None), repository_id),
                            "source_type_id": _type_id_from_clang_type(getattr(right[0], "type", None), repository_id),
                            "expression": _tokens(node), "line": loc["line"], "column": loc["column"],
                            "relative_path": loc["relative_path"], "evidence_grade": "STATIC_AST_HEURISTIC",
                        })

        if kind in {"CXX_THROW_EXPR", "THROW_EXPR"}:
            results["control_flow_events"].append({
                "event_id": _event_id("cf", current_function_id, "THROW", loc, _tokens(node)),
                "function_id": current_function_id, "function_name": current_function_name, "kind": "THROW", "target_id": "", "target_name": "",
                "line": loc["line"], "column": loc["column"], "relative_path": loc["relative_path"],
                "condition_text": _tokens(node), "evidence_grade": "STATIC_AST_SYNTAX",
            })

        if kind in {"CXX_CATCH_STMT", "CATCH_STMT"}:
            results["control_flow_events"].append({
                "event_id": _event_id("cf", current_function_id, "CATCH", loc, _tokens(node)),
                "function_id": current_function_id, "function_name": current_function_name, "kind": "CATCH", "target_id": "", "target_name": "",
                "line": loc["line"], "column": loc["column"], "relative_path": loc["relative_path"],
                "condition_text": _tokens(node), "evidence_grade": "STATIC_AST_SYNTAX",
            })

        if kind == "RETURN_STMT":
            expr = _tokens(node)
            refs = _referenced_variables(node, repository_root, repository_id)
            return_type = return_type_by_function.get(current_function_id, "")
            results["return_flows"].append({
                "return_id": _event_id("return", current_function_id, "RETURN", loc, expr),
                "function_id": current_function_id, "function_name": current_function_name,
                "expression": expr, "referenced_variable_ids": "|".join(vid for _, vid in refs),
                "referenced_variable_names": "|".join(base.qualified_name(d) or d.spelling for d, _ in refs),
                "return_line": loc["line"], "relative_path": loc["relative_path"],
                "expression_pointer_like": "*" in return_type or "&" in return_type or any(word in return_type for word in ("unique_ptr", "shared_ptr", "weak_ptr")),
                "evidence_grade": "STATIC_AST_SYNTAX",
            })
            if expr.strip() in {"return ;", "return \"\" ;", "return {} ;", "return nullptr ;", "return NULL ;", "return 0 ;"} or re.search(r"return\s+\"\"\s*;?$", expr):
                results["control_flow_events"].append({
                    "event_id": _event_id("cf", current_function_id, "EMPTY_OR_NULL_RETURN", loc, expr),
                    "function_id": current_function_id, "function_name": current_function_name, "kind": "EMPTY_OR_NULL_RETURN", "target_id": "",
                    "target_name": "", "line": loc["line"], "column": loc["column"],
                    "relative_path": loc["relative_path"], "condition_text": expr,
                    "evidence_grade": "STATIC_AST_SYNTAX",
                })

        # All children retain nearest ancestors for scope/access classification.
        next_ancestors = ancestors + [node]
        for child in _children(node):
            visit_body(child, current_function_id, current_function_name, next_ancestors, root_node, allow_nested_function)

    for fn_record in functions.values():
        visit_body(fn_record["cursor"], fn_record["function_id"], fn_record["qualified_name"], [], fn_record["cursor"])

    # Project variables declared outside a body, plus variables found via references.
    for node in _walk(cursor):
        kind = _kind(node)
        if kind in {"VAR_DECL", "FIELD_DECL", "PARM_DECL"} and _is_project_cursor(node, repository_root):
            rec = _variable_record(node, repository_root, repository_id, config)
            if rec:
                variables_by_id.setdefault(rec["variable_id"], rec)
                register_type(getattr(node, "type", None), _location(node, repository_root))

        # Class hierarchy, aliases and aggregate types.
        if kind in {"CLASS_DECL", "STRUCT_DECL", "UNION_DECL", "ENUM_DECL", "CLASS_TEMPLATE", "CLASS_TEMPLATE_PARTIAL_SPECIALIZATION", "TYPEDEF_DECL", "TYPE_ALIAS_DECL"} and _is_project_cursor(node, repository_root):
            loc = _location(node, repository_root)
            if kind in {"TYPEDEF_DECL", "TYPE_ALIAS_DECL"}:
                type_id = _type_id(node, repository_root, repository_id)
                try:
                    underlying_type = node.underlying_typedef_type
                except Exception:
                    underlying_type = getattr(node, "type", None)
                target_type_id = register_type(underlying_type, loc)
                type_nodes_by_id[type_id] = {
                    "type_id": type_id, "repository_id": repository_id,
                    "name": getattr(node, "spelling", "") or "", "qualified_name": base.qualified_name(node),
                    "type_kind": "TYPE_ALIAS", "relative_path": loc["relative_path"], "line": loc["line"],
                    "start_line": loc["start_line"], "end_line": loc["end_line"], "usr": _decl_usr(node),
                    "canonical_type": str(getattr(underlying_type, "spelling", "") or ""), "clang_type_kind": _kind(node),
                    "pointer_depth": _type_properties_from_type(underlying_type, config).get("pointer_depth", 0) if underlying_type is not None else 0,
                    "reference_kind": _type_properties_from_type(underlying_type, config).get("reference_kind", "NONE") if underlying_type is not None else "NONE",
                    "pointee_type_id": target_type_id, "pointee_type": str(getattr(underlying_type, "spelling", "") or ""),
                    "smart_pointer_kind": _type_properties_from_type(underlying_type, config).get("smart_pointer_kind", "NONE") if underlying_type is not None else "NONE",
                    "ownership_model_hint": _type_properties_from_type(underlying_type, config).get("ownership_model_hint", "NOT_POINTER_LIKE") if underlying_type is not None else "NOT_POINTER_LIKE",
                    "is_const": False, "is_volatile": False, "is_function_pointer": False, "evidence_grade": "STATIC_AST_TYPE_ALIAS",
                }
                type_edges_by_id[_hash_id("typeedge", type_id, "TYPE_ALIAS_OF", target_type_id)] = {
                    "relationship_id": _hash_id("typeedge", type_id, "TYPE_ALIAS_OF", target_type_id),
                    "relationship_type": "TYPE_ALIAS_OF", "source_type_id": type_id,
                    "source_type_name": base.qualified_name(node), "target_type_id": target_type_id,
                    "target_type_name": str(getattr(underlying_type, "spelling", "") or ""),
                    "relative_path": loc["relative_path"], "line": loc["line"], "evidence_grade": "STATIC_AST_TYPE_ALIAS",
                }
            else:
                type_id = register_type(getattr(node, "type", None), loc)
                if not type_id:
                    type_id = _type_id(node, repository_root, repository_id)
                    type_nodes_by_id[type_id] = {
                        "type_id": type_id, "repository_id": repository_id,
                        "name": getattr(node, "spelling", "") or "", "qualified_name": base.qualified_name(node),
                        "type_kind": kind, "relative_path": loc["relative_path"], "line": loc["line"],
                        "start_line": loc["start_line"], "end_line": loc["end_line"], "usr": _decl_usr(node),
                        "canonical_type": base.qualified_name(node), "clang_type_kind": kind,
                        "pointer_depth": 0, "reference_kind": "NONE", "pointee_type_id": "", "pointee_type": "",
                        "smart_pointer_kind": "NONE", "ownership_model_hint": "NOT_POINTER_LIKE", "is_const": False,
                        "is_volatile": False, "is_function_pointer": False, "evidence_grade": "STATIC_AST_DECLARATION",
                    }
            type_seen.add(type_id)
            for child in _children(node):
                if _kind(child) == "CXX_BASE_SPECIFIER":
                    base_decl = _referenced(child)
                    base_name = base.qualified_name(base_decl) if base_decl is not None else _tokens(child)
                    if base_decl is not None:
                        register_type(getattr(base_decl, "type", None), _location(child, repository_root))
                    base_id = _type_id(base_decl, repository_root, repository_id) if base_decl is not None else _hash_id("type", repository_id, base_name)
                    b_loc = _location(child, repository_root)
                    results["type_relationships"].append({
                        "relationship_id": _event_id("inherits", type_id, "INHERITS", b_loc, base_name),
                        "relationship_type": "INHERITS", "source_type_id": type_id,
                        "source_type_name": getattr(node, "spelling", ""), "target_type_id": base_id,
                        "target_type_name": base_name, "child_type_id": type_id,
                        "child_type_name": getattr(node, "spelling", ""), "base_type_id": base_id,
                        "base_type_name": base_name, "relative_path": b_loc["relative_path"], "line": b_loc["line"],
                        "evidence_grade": "STATIC_AST_RESOLVED" if base_decl is not None else "STATIC_AST_UNRESOLVED",
                    })

        # Macro data is available only with detailed preprocessing records enabled.
        if kind in {"MACRO_DEFINITION", "MACRO_INSTANTIATION", "MACRO_EXPANSION"}:
            loc = _location(node, repository_root)
            name = str(getattr(node, "spelling", "") or "")
            macro_key = (kind, loc["relative_path"], loc["line"], name)
            if macro_key not in macro_seen and loc["relative_path"]:
                macro_seen.add(macro_key)
                results["macro_events"].append({
                    "macro_event_id": _event_id("macro", "", kind, loc, name + _tokens(node)),
                    "event_kind": kind, "macro_name": name, "relative_path": loc["relative_path"],
                    "line": loc["line"], "column": loc["column"], "tokens": _tokens(node),
                    "evidence_grade": "CLANG_PREPROCESSING_RECORD",
                    "note": "Expansion locations can map to invocation sites; macro definition text may be elsewhere.",
                })

    # Emit lambda parameters as Callable-owned parameters, rather than silently
    # attaching them to the enclosing Function.
    for lambda_node in (node for node in _walk(cursor) if _kind(node) == "LAMBDA_EXPR" and _is_project_cursor(node, repository_root)):
        lambda_rec = _lambda_record(lambda_node, repository_root, repository_id, lambda_cache)
        lambda_params = [child for child in _children(lambda_node) if _kind(child) == "PARM_DECL"]
        for index, param in enumerate(lambda_params):
            rec = _variable_record(param, repository_root, repository_id, config, lambda_rec["callable_id"], lambda_rec["qualified_name"])
            if not rec:
                continue
            variables_by_id[rec["variable_id"]] = rec
            register_type(getattr(param, "type", None), _location(param, repository_root))
            results["function_parameters"].append({
                "parameter_id": rec["variable_id"], "function_id": "", "callable_id": lambda_rec["callable_id"],
                "function_name": lambda_rec["qualified_name"], "parameter_index": index,
                "parameter_name": rec["name"], "type_id": rec.get("type_id", ""), "type_spelling": rec["type_spelling"],
                "canonical_type": rec["canonical_type"], "pointer_depth": rec["pointer_depth"],
                "reference_kind": rec["reference_kind"], "smart_pointer_kind": rec["smart_pointer_kind"],
                "ownership_model_hint": rec["ownership_model_hint"], "is_const": rec["is_const"],
                "relative_path": rec["relative_path"], "line": rec["line"], "column": rec["column"],
            })

    # Lambda-body calls are visited after the enclosing spawn expression is seen.
    # Resolve the display text once those calls have been collected.
    callable_by_id = {rec.get("callable_id"): rec for rec in results["callable_targets"]}
    for spawn in results["thread_spawns"]:
        if spawn.get("target_kind") == "LAMBDA":
            callable_rec = callable_by_id.get(spawn.get("target_callable_id"), {})
            body_names = callable_rec.get("body_function_names", [])
            if body_names:
                spawn["target_text"] = "lambda->" + body_names[0]
                spawn["target_body_calls"] = "|".join(body_names)
    # Associate AST lifetime/pointer sites with macro invocation records on the same
    # expanded source line. This is evidence for review, not proof that the macro
    # caused the operation; expansion/source-range mapping is Clang-version dependent.
    macro_expansions = [m for m in results["macro_events"] if str(m.get("event_kind", "")).upper() in {"MACRO_INSTANTIATION", "MACRO_EXPANSION"}]
    for artifact_key in ("lifetime_events", "pointer_events"):
        for event in results[artifact_key]:
            related = [m for m in macro_expansions if m.get("relative_path") == event.get("relative_path") and int(m.get("line", 0) or 0) == int(event.get("line", -1) or -1)]
            event["macro_event_ids"] = "|".join(str(m.get("macro_event_id", "")) for m in related if m.get("macro_event_id"))
            event["macro_event_names"] = "|".join(str(m.get("macro_name", "")) for m in related if m.get("macro_name"))
            if related:
                event["event_semantics"] = (str(event.get("event_semantics", "")) + "; same-line macro expansion association is heuristic") if artifact_key == "lifetime_events" else event.get("event_semantics", "")
    # Event schemas keep legacy-compatible `function_id` fields where applicable,
    # but lambda-owned evidence must link to a Callable, not a fabricated Function.
    event_owner_keys = {
        "variable_accesses": "function_id", "lifetime_events": "function_id", "lock_usage": "function_id",
        "pointer_events": "function_id", "synchronization_events": "function_id", "control_flow_events": "function_id",
        "return_flows": "function_id",
    }
    for artifact_key, owner_key in event_owner_keys.items():
        for record in results[artifact_key]:
            owner_id = str(record.get(owner_key, "") or "")
            if owner_id.startswith("lambda_"):
                record["callable_id"] = owner_id
                record[owner_key] = ""
    for record in variables_by_id.values():
        owner_id = str(record.get("owner_function_id", "") or "")
        if owner_id.startswith("lambda_"):
            record["owner_callable_id"] = owner_id
            record["owner_function_id"] = ""
    for record in results["call_arguments"]:
        owner_id = str(record.get("caller_function_id", "") or "")
        if owner_id.startswith("lambda_"):
            record["caller_callable_id"] = owner_id
            record["caller_function_id"] = ""
    results["variables"] = list(variables_by_id.values())
    results["types"] = list(type_nodes_by_id.values())
    # Preserve inheritance records created above, then append structural type edges.
    existing_type_edges = {str(rec.get("relationship_id")): rec for rec in results["type_relationships"] if rec.get("relationship_id")}
    existing_type_edges.update(type_edges_by_id)
    results["type_relationships"] = list(existing_type_edges.values())
    results["external_symbols"] = list(external_symbols_by_id.values())

    # Emit lock RAII records from lock-wrapper variable declarations. Scope is the
    # nearest enclosing compound; this is lexical scope evidence, not full control flow.
    for fn_record in functions.values():
        fn = fn_record["cursor"]
        ancestors: list[Any] = []
        def lock_walk(node: Any, stack: list[Any]) -> None:
            kind = _kind(node)
            loc = _location(node, repository_root)
            if kind == "VAR_DECL":
                rec = variables_by_id.get(_variable_id(node, repository_root, repository_id), {})
                type_text = rec.get("type_spelling", "")
                guard_kind = next((name for name in ("lock_guard", "unique_lock", "scoped_lock", "shared_lock") if name in type_text), "")
                if guard_kind:
                    arg_refs = _referenced_variables(node, repository_root, repository_id)
                    mutex_decl, mutex_id = _pick_target_variable(arg_refs)
                    scope_start, scope_end = _nearest_compound(stack, loc["line"])
                    guard_expression = _tokens(node)
                    if "defer_lock" in guard_expression:
                        guard_event = "RAII_DEFERRED_LOCK"
                    elif "try_to_lock" in guard_expression:
                        guard_event = "RAII_TRY_ACQUIRE"
                    elif "adopt_lock" in guard_expression:
                        guard_event = "RAII_ADOPT_EXISTING_LOCK"
                    else:
                        guard_event = "RAII_ACQUIRE_AND_SCOPE_RELEASE"
                    results["lock_usage"].append({
                        "lock_event_id": _event_id("lock", fn_record["function_id"], guard_kind, loc, guard_expression),
                        "function_id": fn_record["function_id"], "function_name": fn_record["qualified_name"],
                        "mutex_id": mutex_id, "mutex_text": base.qualified_name(mutex_decl) if mutex_decl is not None else guard_expression,
                        "lock_variable_id": rec.get("variable_id", ""), "kind": guard_kind, "event": guard_event,
                        "line": loc["line"], "scope_start_line": scope_start, "scope_end_line": scope_end,
                        "relative_path": loc["relative_path"], "expression": guard_expression,
                        "scope_semantics": "RAII lexical lifetime; defer/try/adopt tags are not equivalent to confirmed acquisition",
                        "evidence_grade": "STATIC_AST_TYPE_AND_SCOPE",
                    })
                    results["synchronization_events"].append({
                        "event_id": _event_id("sync", fn_record["function_id"], "RAII_LOCK", loc, _tokens(node)),
                        "function_id": fn_record["function_id"], "kind": "RAII_LOCK_SCOPE", "primitive": guard_kind,
                        "target_id": mutex_id, "target_name": base.qualified_name(mutex_decl) if mutex_decl is not None else _tokens(node),
                        "line": loc["line"], "column": loc["column"], "relative_path": loc["relative_path"],
                        "expression": _tokens(node), "evidence_grade": "STATIC_AST_TYPE_AND_SCOPE",
                    })
            for child in _children(node):
                lock_walk(child, stack + [node])
        lock_walk(fn, [])

    # Clean duplicates caused by repeated declarations/overloaded-TU visibility.
    for key in EVIDENCE_KEYS:
        unique = {}
        for rec in results[key]:
            if key == "variables":
                dedupe_key = rec.get("variable_id")
            elif key == "function_parameters":
                dedupe_key = (rec.get("function_id"), rec.get("parameter_index"), rec.get("parameter_id"))
            elif key == "callable_targets":
                dedupe_key = rec.get("callable_id")
            elif key == "types":
                dedupe_key = rec.get("type_id")
            elif key == "external_symbols":
                dedupe_key = rec.get("symbol_id")
            elif key == "call_sites":
                dedupe_key = rec.get("call_site_id")
            elif key == "function_return_types":
                dedupe_key = rec.get("function_id")
            elif key == "type_relationships":
                dedupe_key = rec.get("relationship_id")
            else:
                dedupe_key = rec.get("event_id") or rec.get("spawn_id") or rec.get("lock_event_id") or rec.get("macro_event_id") or rec.get("relationship_id") or rec.get("argument_id") or rec.get("access_id") or json.dumps(rec, sort_keys=True, default=str)
            unique[str(dedupe_key)] = rec
        results[key] = sorted(unique.values(), key=lambda r: (str(r.get("relative_path", "")), int(r.get("line", r.get("call_line", 0)) or 0), str(r.get("event_id", r.get("spawn_id", r.get("variable_id", ""))))))
    return results
