import json

from traversal.investigation_ledger import InvestigationLedger


def read_events(path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_records_decision_before_result(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger = InvestigationLedger(path, run_id="test-run")

    step_id = ledger.plan(
        tool="find_field_writers",
        target={"field_id": "field-1"},
        decision={
            "why_this_step": "The function reads shared state.",
            "question_to_resolve": "Who writes this field?",
            "expected_evidence": "Writer function IDs",
        },
    )

    events = read_events(path)
    assert len(events) == 1
    assert events[0]["event"] == "decision"
    assert events[0]["step_id"] == step_id
    assert events[0]["status"] == "PLANNED"

    ledger.finish(
        step_id,
        status="COMPLETED",
        result={"status": "OK", "count": 2, "function_ids": ["fn-a", "fn-b"]},
    )

    events = read_events(path)
    assert [event["event"] for event in events] == ["decision", "result"]
    assert events[1]["step_id"] == step_id
    assert events[1]["status"] == "COMPLETED"
    assert "fn-a" in events[1]["result_summary"]


def test_does_not_persist_source_code(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger = InvestigationLedger(path, run_id="safe-run")

    step_id = ledger.plan(
        tool="read_function_source",
        target={"function_id": "fn-1"},
        decision={"why_this_step": "Verify the relevant implementation."},
    )
    ledger.finish(
        step_id,
        status="COMPLETED",
        result={
            "status": "SOURCE_VERIFIED",
            "function_id": "fn-1",
            "source": {"numbered_source": "SECRET_SOURCE_CODE"},
        },
    )

    raw = path.read_text(encoding="utf-8")
    assert "SECRET_SOURCE_CODE" not in raw
    assert "SOURCE_VERIFIED" in raw


def test_separate_runs_are_identified(tmp_path):
    path = tmp_path / "ledger.jsonl"
    first = InvestigationLedger(path, run_id="run-a")
    second = InvestigationLedger(path, run_id="run-b")

    first.plan(tool="find_callers", target={"function_id": "fn-1"})
    second.plan(tool="find_callees", target={"function_id": "fn-2"})

    events = read_events(path)
    assert [event["run_id"] for event in events] == ["run-a", "run-b"]


def test_recent_summary_includes_decision_and_result(tmp_path):
    ledger = InvestigationLedger(tmp_path / "ledger.jsonl", run_id="summary-run")

    step_id = ledger.plan(
        tool="find_field_writers",
        target={"field_id": "field-2"},
        decision={
            "why_this_step": "Trace shared-state modifications.",
            "question_to_resolve": "Which functions write the field?",
        },
    )
    ledger.finish(
        step_id,
        status="COMPLETED",
        result={"status": "OK", "count": 1},
    )

    summary = ledger.render_recent()
    assert "find_field_writers" in summary
    assert "Trace shared-state modifications." in summary
    assert "Which functions write the field?" in summary
    assert "COMPLETED" in summary


def test_missing_rationale_is_safe(tmp_path):
    ledger = InvestigationLedger(tmp_path / "ledger.jsonl", run_id="missing-rationale")
    step_id = ledger.plan(tool="find_callers", target={"function_id": "fn-3"})

    assert step_id == 1
    assert "not supplied" in ledger.render_recent()
