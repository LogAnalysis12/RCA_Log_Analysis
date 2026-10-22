from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from traversal.investigator import run_investigation
from traversal.llm_backends import StepResult, ToolCall

ANCHOR = "fn_anchor"
CALLER = "fn_caller"
ANCHOR_PATH = "DServerCommunication/DServerCommunication.cpp"
CALLER_PATH = "DServerCommunication/ExecuteService.cpp"


def source_result(function_id, name, path, start, end, text):
    return {
        "status": "SOURCE_VERIFIED",
        "function_id": function_id,
        "function": {"function_id": function_id, "qualified_name": name, "relative_path": path},
        "source": {"start_line": start, "actual_end_line": end, "numbered_source": text},
    }


class MockTools:
    def read_function_source(self, function_id: str, **kwargs):
        function_id = function_id.removeprefix("Function:")
        if function_id == ANCHOR:
            return source_result(
                ANCHOR,
                "CDServerCommunication::CreateDiagComPrimitiveByName",
                ANCHOR_PATH,
                801,
                842,
                "809 | p_MCDLogicalLinkPtr->createDiagComPrimitiveByName(p_strServiceName);",
            )
        if function_id == CALLER:
            return source_result(
                CALLER,
                "CExecuteService::ExecuteService",
                CALLER_PATH,
                7,
                70,
                "16 | CreateDiagComPrimitiveByName(p_MCDLogicalLinkObj, p_diagServiceInfo.strServiceName);",
            )
        return {"status": "FUNCTION_NOT_FOUND", "function_id": function_id}

    def find_callers(self, function_id: str, limit: int = 30, **kwargs):
        return {
            "status": "OK",
            "target_function_id": ANCHOR,
            "caller_count": 1,
            "callers": [{
                "function_id": CALLER,
                "qualified_name": "CExecuteService::ExecuteService",
                "relative_path": CALLER_PATH,
                "start_line": 7,
                "end_line": 70,
                "call_sites": [{"line": 16}],
            }],
        }

    def find_callees(self, function_id: str, limit: int = 40, **kwargs):
        return {
            "status": "OK",
            "caller_function_id": function_id.removeprefix("Function:"),
            "project_callees": [],
            "external_callees": [],
            "external_boundary_present": False,
        }

    def find_fields_read(self, function_id: str, include_ignored: bool = False, **kwargs):
        return {"status": "OK", "function_id": function_id, "fields": []}

    def find_field_writers(self, field_id: str, exclude_function_id=None, limit: int = 30, **kwargs):
        return {"status": "OK", "field_id": field_id, "writers": [], "writer_count": 0}


class InvestigatorBackend:
    def __init__(self):
        self.result_ids: Counter[str] = Counter()
        self.rejection_seen = False

    def send(self, content):
        return step("t1", "read_function_source", {"function_id": ANCHOR})

    def send_tool_results(self, results):
        for result in results:
            self.result_ids[result["tool_use_id"]] += 1
            assert self.result_ids[result["tool_use_id"]] == 1
        tool_id = results[-1]["tool_use_id"]
        if tool_id == "t1":
            return step("t2", "propose_conclusion", conclusion())
        if tool_id == "t2":
            assert results[-1]["content"]["source"] == "DETERMINISTIC_VERIFIER"
            self.rejection_seen = True
            return step("t3", "find_callers", {"function_id": ANCHOR})
        if tool_id == "t3":
            return step("t4", "read_function_source", {"function_id": CALLER})
        if tool_id == "t4":
            return step("t5", "propose_conclusion", conclusion())
        raise AssertionError(f"Unexpected tool result: {tool_id}")


class CriticBackend:
    def send(self, content):
        payload = json.loads(content)
        assert payload["deterministic_verification"]["passed"]
        evidence_ids = {item["function_id"] for item in payload["verified_evidence"]}
        assert {ANCHOR, CALLER}.issubset(evidence_ids)
        return StepResult(
            tool_calls=[],
            text=json.dumps({
                "verdict": "ACCEPT",
                "reasons": ["Verified caller evidence is present."],
                "continuation_instruction": "",
                "confidence": 0.9,
            }),
            stop_reason="end_turn",
            usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )

    def send_tool_results(self, results):
        raise AssertionError("Critic must not receive tool results")


class BackendFactory:
    def __init__(self):
        self.investigator = InvestigatorBackend()
        self.critic_calls = 0

    def __call__(self, *, system_prompt: str, tools: list[dict]):
        if tools:
            return self.investigator
        self.critic_calls += 1
        return CriticBackend()


def step(tool_id: str, name: str, data: dict[str, Any]):
    return StepResult(
        tool_calls=[ToolCall(id=tool_id, name=name, input=data)],
        text="",
        stop_reason="tool_use",
        usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )


def conclusion():
    return {
        "root_cause_function_id": f"Function:{CALLER}",
        "root_cause_qualified_name": "CExecuteService::ExecuteService",
        "suspicious_value": "p_MCDLogicalLinkObj",
        "value_origin": "FUNCTION_PARAMETER",
        "relative_path": CALLER_PATH,
        "line": 16,
        "explanation": "The verified caller supplies the suspicious parameter at line 16.",
        "confidence": 0.7,
        "external_boundary": False,
    }


def issue():
    return {
        "fingerprint_id": "fp_mock",
        "occurrence_count": 2,
        "qualified_function": "CDServerCommunication::CreateDiagComPrimitiveByName",
        "normalized_message": "Exception occured: Unknown exception",
        "representative_error": {"level": "ERROR", "message": "Unknown exception"},
        "anchor_resolution": {
            "status": "RESOLVED",
            "selected_function": {
                "function_id": ANCHOR,
                "qualified_name": "CDServerCommunication::CreateDiagComPrimitiveByName",
                "relative_path": ANCHOR_PATH,
                "start_line": 801,
                "end_line": 842,
            },
        },
    }


def main() -> int:
    factory = BackendFactory()
    result = run_investigation(
        issue=issue(),
        tools=MockTools(),
        backend_factory=factory,
        max_steps=10,
        max_critic_rounds=1,
        repeated_tool_limit=2,
        runtime_config={
            "compact_tool_results": True,
            "prevent_equivalent_conclusions": True,
            "max_source_characters_per_function": 12_000,
        },
    )
    history = result.get("state", {}).get("tool_history", [])
    names = [item.get("tool") for item in history]
    validation = {
        "accepted": result.get("status") == "ACCEPTED",
        "deterministic_rejection_observed": factory.investigator.rejection_seen,
        "caller_route_used": "find_callers" in names,
        "two_sources_read": names.count("read_function_source") >= 2,
        "critic_called_once": factory.critic_calls == 1,
        "one_result_per_tool_use_id": all(
            count == 1 for count in factory.investigator.result_ids.values()
        ),
        "verification_passed": result.get("verification", {}).get("passed") is True,
    }
    output = Path("./output/agent_mock_validation.json").resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"validation": validation, "result": result}, indent=2), encoding="utf-8")
    print("\n========== AGENT MOCK VALIDATION ==========")
    for name, passed in validation.items():
        print(f"{name}: {passed}")
    print(f"Report: {output}")
    print("===========================================")
    return 0 if all(validation.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
