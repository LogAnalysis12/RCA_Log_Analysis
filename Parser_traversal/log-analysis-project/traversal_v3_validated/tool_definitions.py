from __future__ import annotations

TOOLS = [
    {
        "name": "read_function_source",
        "description": "Read and hash-verify exact C++ source for a Function ID.",
        "parameters": {
            "type": "object",
            "properties": {"function_id": {"type": "string"}},
            "required": ["function_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "find_callers",
        "description": "Find unique upstream caller functions and all call sites.",
        "parameters": {
            "type": "object",
            "properties": {"function_id": {"type": "string"}},
            "required": ["function_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "find_callees",
        "description": "Find relevant project callees and external API boundaries for a function.",
        "parameters": {
            "type": "object",
            "properties": {"function_id": {"type": "string"}},
            "required": ["function_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_fields_read",
        "description": "List relevant non-logging DataField nodes read by a function.",
        "parameters": {
            "type": "object",
            "properties": {"function_id": {"type": "string"}},
            "required": ["function_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "find_field_writers",
        "description": "Find functions writing an exact DataField ID.",
        "parameters": {
            "type": "object",
            "properties": {
                "field_id": {"type": "string"},
                "exclude_function_id": {"type": "string"},
            },
            "required": ["field_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "propose_conclusion",
        "description": "Propose a source-supported root cause for verification and Critic review.",
        "parameters": {
            "type": "object",
            "properties": {
                "root_cause_function_id": {"type": "string"},
                "root_cause_qualified_name": {"type": "string"},
                "suspicious_value": {"type": "string"},
                "value_origin": {"type": "string", "enum": [
                    "FUNCTION_PARAMETER", "MEMBER_FIELD", "GLOBAL_FIELD",
                    "RETURN_VALUE", "LOCAL_ASSIGNMENT", "EXTERNAL_RESULT", "UNKNOWN"
                ]},
                "relative_path": {"type": "string"},
                "line": {"type": "integer"},
                "explanation": {"type": "string"},
                "confidence": {"type": "number"},
                "external_boundary": {"type": "boolean"},
            },
            "required": [
                "root_cause_function_id", "root_cause_qualified_name",
                "suspicious_value", "value_origin", "relative_path", "line",
                "explanation", "confidence", "external_boundary"
            ],
            "additionalProperties": False,
        },
    },
]
