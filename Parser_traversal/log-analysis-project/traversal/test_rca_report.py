from __future__ import annotations

import json

import pytest

from traversal.rca_report import generate_human_report
from traversal.run_traversal import generate_reports_safely


def result_fixture(status="ACCEPTED"):
    return {
        "status": status,
        "selected_issue": {
            "fingerprint_id": "FP-1",
            "occurrence_count": 2,
            "qualified_function": "Reader::run",
            "normalized_message": "Unknown exception",
            "relative_path": "src/reader.cpp",
        },
        "conclusion": {
            "root_cause_function_id": "fn-writer",
            "root_cause_qualified_name": "Settings::setMode",
            "relative_path": "src/settings.cpp",
            "line": 42,
            "confidence": 0.8,
            "suspicious_value": "mode",
            "value_origin": "MEMBER_FIELD",
            "explanation": "The writer stores a negative mode that the reader may observe.",
        },
        "verification": {"passed": True, "reasons": []},
        "critic": {"verdict": "ACCEPT", "reasons": ["Evidence supports the proposal."]},
        "critic_rejection_count": 0,
        "execution_metrics": {
            "started_at_utc": "2026-10-16T10:00:00Z",
            "completed_at_utc": "2026-10-16T10:00:02Z",
            "elapsed_seconds": 2,
            "investigator_calls": 2,
            "critic_calls": 1,
            "bedrock_calls": 3,
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "estimated_cost_usd": 0.001,
        },
        "state": {
            "issue": {"anchor_resolution": {"status": "RESOLVED"}},
            "read_functions": {
                "fn-writer": {
                    "status": "SOURCE_VERIFIED",
                    "function_id": "fn-writer",
                    "function": {"qualified_name": "Settings::setMode", "relative_path": "src/settings.cpp"},
                    "source": {
                        "start_line": 40,
                        "actual_end_line": 45,
                        "relative_path": "src/settings.cpp",
                        "source": "void Settings::setMode() {\n    mode = next_mode;\n}",
                        "numbered_source": "40 | void Settings::setMode() {\n42 |     mode = next_mode;\n45 | }",
                    },
                },
                "fn-reader": {
                    "status": "SOURCE_VERIFIED",
                    "function_id": "fn-reader",
                    "function": {"qualified_name": "Reader::run", "relative_path": "src/reader.cpp"},
                    "source": {
                        "start_line": 10,
                        "actual_end_line": 15,
                        "source": "int mode = settings.mode;",
                        "numbered_source": "12 | int mode = settings.mode;",
                    },
                },
            },
            "tool_history": [
                {"step": 1, "tool": "read_function_source", "input": {"function_id": "fn-reader"}, "result": {"status": "SOURCE_VERIFIED", "function": {"qualified_name": "Reader::run", "relative_path": "src/reader.cpp"}, "source": {"start_line": 10, "actual_end_line": 15}}},
                {"step": 2, "tool": "list_fields_read", "input": {"function_id": "fn-reader"}, "result": {"status": "OK", "function_id": "fn-reader", "fields": [{"qualified_name": "Settings::mode", "field_name": "mode", "ignored": False, "access": {"type": "READS", "line": 12}}]}},
                {"step": 3, "tool": "find_field_writers", "input": {"field_id": "field-mode"}, "result": {"status": "OK", "qualified_name": "Settings::mode", "writers": [{"qualified_name": "Settings::setMode", "relative_path": "src/settings.cpp", "call_sites": [{"line": 42}]}]}},
                {"step": 4, "tool": "propose_conclusion", "input": {"root_cause_qualified_name": "Settings::setMode"}, "result": {"submitted_for_review": True}},
            ],
        },
    }


def report(tmp_path, result, **kwargs):
    destination = tmp_path / "result_report.md"
    generate_human_report(result, destination, **kwargs)
    return destination.read_text(encoding="utf-8")


def test_accepted_root_cause_and_cited_source_render(tmp_path):
    text = report(tmp_path, result_fixture())
    assert "**Workflow outcome:** Accepted" in text
    assert "Settings::setMode" in text
    assert "src/settings.cpp:42" in text
    assert "| 40-45 |" in text
    assert "Stored source at cited line 42: mode = next_mode;" in text
    assert "Critic verdict: **ACCEPT**" in text
    assert "not independent proof" in text


def test_rejected_and_verifier_failure_are_explicit(tmp_path):
    result = result_fixture("NOT_ACCEPTED")
    result["verification"] = {"passed": False, "reasons": ["Citation is out of range."]}
    result["critic"] = {"verdict": "REJECT", "reasons": ["Evidence is insufficient."]}
    result["critic_rejection_count"] = 1
    text = report(tmp_path, result)
    assert "**Workflow outcome:** Rejected" in text
    assert "not an established root cause" in text
    assert "Deterministic verifier status: **Failed**" in text
    assert "Citation is out of range." in text
    assert "Critic verdict: **REJECT**" in text
    assert "Evidence is insufficient." in text


@pytest.mark.parametrize("status", ["STALLED", "MAX_STEPS_REACHED", "BUDGET_EXCEEDED", "RUNTIME_FAILED"])
def test_incomplete_status_does_not_assert_root_cause(tmp_path, status):
    result = result_fixture(status)
    result.pop("conclusion")
    result.pop("critic")
    text = report(tmp_path, result)
    assert "**Workflow outcome:** Inconclusive or failed" in text
    assert "does not establish a root cause" in text
    assert "No conclusion object was recorded" in text


def test_missing_source_and_optional_metrics_are_not_invented(tmp_path):
    result = {"status": "STALLED", "state": {"read_functions": {}, "tool_history": []}}
    text = report(tmp_path, result)
    assert "Budget-accounted input tokens: Not available" in text
    assert "No source or graph evidence recorded" in text
    assert "Runtime reproduction or execution evidence: not represented" in text
    assert "No tool history was recorded." in text


def test_unread_conclusion_source_is_labeled_unverified(tmp_path):
    result = result_fixture()
    result["state"]["read_functions"].pop("fn-writer")
    text = report(tmp_path, result)
    assert "source status in the result: `not found in read_functions`" in text
    assert "Investigator conclusion (inference)" in text


def test_trace_metrics_are_distinct_from_budget_metrics(tmp_path):
    text = report(
        tmp_path,
        result_fixture(),
        trace_summary={
            "provider": "openrouter",
            "model": "google/gemini-2.5-flash",
            "actual_api_calls": 4,
            "total_input_tokens": 110,
            "total_output_tokens": 25,
            "provider_reported_total_tokens": 135,
            "estimated_total_cost_usd": 0.0009,
        },
    )
    assert "Budget-accounted input tokens: 100" in text
    assert "Trace provider-reported input tokens: 110" in text
    assert "Trace provider-reported output tokens: 25" in text
    assert "Trace provider-reported total tokens: 135" in text
    assert "Trace estimated cost (USD): 0.0009" in text
    assert "Total model calls (actual HTTP attempts): 4" in text
    assert "Actual HTTP API attempts in trace: 4" in text
    assert "Provider: openrouter" in text
    assert "Model: google/gemini-2.5-flash" in text


def test_generation_failure_preserves_json_and_warns(tmp_path, monkeypatch, capsys):
    path = tmp_path / "investigation.json"
    original = json.dumps(result_fixture(), indent=2)
    path.write_text(original, encoding="utf-8")

    def fail(*args, **kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr("traversal.run_traversal.generate_human_report", fail)
    assert generate_reports_safely(path) is None
    assert path.read_text(encoding="utf-8") == original
    assert "WARNING: RCA JSON was saved" in capsys.readouterr().out


def test_adjacent_and_trace_run_reports_are_written(tmp_path):
    result_path = tmp_path / "run_result.json"
    result_path.write_text(json.dumps(result_fixture()), encoding="utf-8")
    trace_directory = tmp_path / "run_result_20261016T100000Z_ab12cd34"
    trace_directory.mkdir()
    (trace_directory / "rca_result.json").write_text(json.dumps(result_fixture()), encoding="utf-8")
    (trace_directory / "llm_trace_summary.json").write_text(json.dumps({"actual_api_calls": 4}), encoding="utf-8")

    report_path = generate_reports_safely(result_path, trace_directory)
    assert report_path == tmp_path / "run_result_report.md"
    assert report_path.is_file()
    run_report = trace_directory / report_path.name
    assert run_report.is_file()
    assert "Actual HTTP API attempts in trace: 4" in run_report.read_text(encoding="utf-8")