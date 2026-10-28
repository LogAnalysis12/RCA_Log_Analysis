from collections import Counter

from traversal.investigator import (
    _evidence_fingerprint,
    _no_progress_recovery_prompt,
    _tool_turn_fingerprint,
)
from traversal.llm_backends import ToolCall


def test_evidence_fingerprint_ignores_cache_and_continuation_metadata():
    first = {
        "status": "SOURCE_VERIFIED",
        "qualified_name": "Resource::release",
        "relative_path": "src/resource.cpp",
        "cache_hit": False,
    }
    cached = {
        "status": "SOURCE_VERIFIED",
        "qualified_name": "Resource::release",
        "relative_path": "src/resource.cpp",
        "cache_hit": True,
        "continuation_instruction": "Do not reread this source.",
    }
    assert _evidence_fingerprint("read_function_source", first) == _evidence_fingerprint(
        "read_function_source", cached
    )


def test_failed_tool_result_is_not_counted_as_evidence():
    assert _evidence_fingerprint("find_callers", {"status": "TOOL_ERROR", "error": "bad id"}) is None
    assert _evidence_fingerprint("find_callers", {"status": "OK", "caller_count": 0, "callers": []}) is not None


def test_tool_turn_fingerprint_ignores_decision_rationale():
    first = ToolCall(id="c1", name="find_callers", input={
        "function_id": "f1", "decision": {"why_this_step": "first rationale"}
    })
    second = ToolCall(id="c2", name="find_callers", input={
        "function_id": "f1", "decision": {"why_this_step": "changed rationale"}
    })
    assert _tool_turn_fingerprint([first]) == _tool_turn_fingerprint([second])


def test_no_progress_prompt_names_loop_and_embeds_checkpoint():
    checkpoint = {"root_cause_status": "NOT_ESTABLISHED", "recent_tool_actions": []}
    prompt = _no_progress_recovery_prompt(checkpoint, repeated_turn=True, no_progress_steps=2)
    assert "LOOP DETECTED" in prompt
    assert "NOT_ESTABLISHED" in prompt
    assert "Do not repeat the same tool-call sequence" in prompt


def test_no_progress_prompt_distinguishes_unproductive_turns():
    prompt = _no_progress_recovery_prompt(
        {"root_cause_status": "NOT_ESTABLISHED"}, repeated_turn=False, no_progress_steps=3
    )
    assert "3 consecutive tool-call turns" in prompt
    assert "LOOP DETECTED" not in prompt
