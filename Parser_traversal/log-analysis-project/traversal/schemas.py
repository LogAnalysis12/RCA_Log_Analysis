from __future__ import annotations
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any

class ValueOrigin(str, Enum):
    FUNCTION_PARAMETER = "FUNCTION_PARAMETER"
    MEMBER_FIELD = "MEMBER_FIELD"
    GLOBAL_FIELD = "GLOBAL_FIELD"
    RETURN_VALUE = "RETURN_VALUE"
    LOCAL_ASSIGNMENT = "LOCAL_ASSIGNMENT"
    EXTERNAL_RESULT = "EXTERNAL_RESULT"
    UNKNOWN = "UNKNOWN"

class CriticVerdict(str, Enum):
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    NEEDS_MORE_EVIDENCE = "NEEDS_MORE_EVIDENCE"

@dataclass
class Evidence:
    function_id: str
    qualified_name: str
    relative_path: str
    line: int
    observation: str
    source_verified: bool

@dataclass
class InvestigationState:
    issue: dict[str, Any]
    anchor: dict[str, Any]
    read_functions: dict[str, dict[str, Any]] = field(default_factory=dict)
    tool_history: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    total_usage: dict[str, int] = field(default_factory=lambda: {
        "input_tokens": 0, "output_tokens": 0, "total_tokens": 0
    })
    critic_feedback: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
