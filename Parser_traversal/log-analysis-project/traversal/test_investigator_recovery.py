from collections import Counter
from types import SimpleNamespace

from traversal.investigator import (
    _build_recovery_checkpoint,
    _no_tool_recovery_prompt,
    _tool_failure_recovery_instruction,
)


def _sample_checkpoint():
    issue = {
        "fingerprint_id": "fp_test",
        "occurrence_count": 3,
        "qualified_function": "Worker::run",
        "normalized_message": "resource unavailable",
        "representative_error": {"message": "resource unavailable", "qualified_function": "Worker::run"},
    }
    anchor = {"qualified_name": "Worker::run", "relative_path": "src/worker.cpp", "start_line": 10, "end_line": 20}
    state = SimpleNamespace(
        read_functions={"f1": {"qualified_name": "Resource::release", "relative_path": "src/worker.cpp", "start_line": 30, "end_line": 40, "status": "SOURCE_VERIFIED"}},
        tool_history=[
            {"step": 1, "tool": "read_function_source", "input": {"function_id": "f1"}, "result": {"status": "SOURCE_VERIFIED", "qualified_name": "Resource::release", "relative_path": "src/worker.cpp", "start_line": 30}},
            {"step": 2, "tool": "find_field_writers", "input": {"field_id": "current"}, "result": {"status": "OK", "field_name": "current", "writer_count": 1}},
        ],
    )
    return issue, anchor, state


def test_length_recovery_prompt_is_specific_and_keeps_checkpoint():
    issue, anchor, state = _sample_checkpoint()
    checkpoint = _build_recovery_checkpoint(issue, anchor, state, Counter(), "length")
    prompt = _no_tool_recovery_prompt("length", checkpoint, "partial response")
    assert "RESPONSE TRUNCATED" in prompt
    assert "Do not continue or defend the partial prose" in prompt
    assert "Resource::release" in prompt
    assert "NOT_ESTABLISHED" in prompt


def test_stop_recovery_prompt_differs_from_truncation_prompt():
    issue, anchor, state = _sample_checkpoint()
    checkpoint = _build_recovery_checkpoint(issue, anchor, state, Counter(), "stop")
    stop_prompt = _no_tool_recovery_prompt("stop", checkpoint)
    length_prompt = _no_tool_recovery_prompt("length", checkpoint)
    assert "PROSE WITHOUT A TOOL CALL" in stop_prompt
    assert "RESPONSE TRUNCATED" in length_prompt
    assert stop_prompt != length_prompt


def test_checkpoint_records_completed_and_repeated_actions():
    issue, anchor, state = _sample_checkpoint()
    repeats = Counter({"read_function_source:f1": 3})
    checkpoint = _build_recovery_checkpoint(issue, anchor, state, repeats, "stop")
    assert checkpoint["root_cause_status"] == "NOT_ESTABLISHED"
    assert len(checkpoint["recent_tool_actions"]) == 2
    assert checkpoint["repeated_actions"][0]["count"] == 3
    assert checkpoint["verified_source_functions"][0]["qualified_name"] == "Resource::release"


def test_invalid_argument_recovery_prompt_is_specific():
    issue, anchor, state = _sample_checkpoint()
    checkpoint = _build_recovery_checkpoint(issue, anchor, state, Counter(), "invalid_tool_arguments")
    prompt = _tool_failure_recovery_instruction(
        "find_field_writers",
        {"status": "INVALID_TOOL_ARGUMENTS", "error": "Malformed JSON tool arguments"},
        checkpoint,
    )
    assert "INVALID ARGUMENTS FOR find_field_writers" in prompt
    assert "correct only the invalid or missing arguments" in prompt
    assert "CURRENT INVESTIGATION CHECKPOINT" in prompt
