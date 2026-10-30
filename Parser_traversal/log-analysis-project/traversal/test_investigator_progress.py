from collections import Counter

from traversal.investigator import (
    _evidence_fingerprint,
    _meaningful_evidence_fingerprint,
    _no_progress_recovery_prompt,
    run_investigation,
    _tool_turn_fingerprint,
)
from traversal.llm_backends import StepResult, ToolCall


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
    assert _meaningful_evidence_fingerprint("find_callers", {"function_id": "f1"}, {"status": "OK", "caller_count": 0, "callers": []}, {}) is None


def test_meaningful_progress_requires_new_relevant_evidence():
    decision = {"question_to_resolve": "Which functions call the observer?"}
    result = {"status": "OK", "callers": [{"function_id": "f2", "call_site": {"line": 8}}]}

    first = _meaningful_evidence_fingerprint("find_callers", {"function_id": "f1"}, result, decision)
    reordered = _meaningful_evidence_fingerprint(
        "find_callers", {"function_id": "f1"},
        {"callers": [{"call_site": {"line": 8}, "function_id": "f2"}], "status": "OK"}, decision,
    )

    assert first is not None
    assert first == reordered


def test_empty_result_counts_only_when_tied_to_an_unresolved_question():
    result = {"status": "OK", "writers": [], "writer_count": 0}

    assert _meaningful_evidence_fingerprint("find_field_writers", {"field_id": "d1"}, result, {}) is None
    assert _meaningful_evidence_fingerprint(
        "find_field_writers", {"field_id": "d1"}, result,
        {"question_to_resolve": "Does any function write this observed field?"},
    ) is not None


def test_cached_source_and_unverified_source_do_not_count_as_progress():
    source_result = {
        "status": "SOURCE_VERIFIED", "function_id": "f1",
        "function": {"relative_path": "src/a.cpp"},
        "source": {
            "content_hash_matches": True,
            "expected_content_hash": "hash-a",
            "computed_content_hash": "hash-a",
        },
    }
    assert _meaningful_evidence_fingerprint("read_function_source", {}, source_result, {})
    assert _meaningful_evidence_fingerprint(
        "read_function_source", {}, {**source_result, "cache_hit": True}, {},
    ) is None
    source_result["source"]["computed_content_hash"] = "hash-b"
    assert _meaningful_evidence_fingerprint("read_function_source", {}, source_result, {}) is None


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


def test_causal_progress_gate_blocks_unscoped_action_after_no_progress():
    class Tools:
        caller_queries = 0
        callee_queries = 0

        def find_callers(self, function_id, limit):
            self.caller_queries += 1
            return {"status": "OK", "caller_count": 0, "callers": []}

        def find_callees(self, function_id, limit):
            self.callee_queries += 1
            return {"status": "OK", "project_callees": [], "external_callees": []}

    class Backend:
        result_count = 0

        def send(self, content):
            return StepResult(tool_calls=[ToolCall("first", "find_callers", {"function_id": "f1"})])

        def send_tool_results(self, results):
            self.result_count += 1
            if self.result_count == 1:
                return StepResult(tool_calls=[ToolCall("second", "find_callees", {"function_id": "f1"})])
            return StepResult()

    tools = Tools()
    backend = Backend()
    issue = {"anchor_resolution": {"selected_function": {"function_id": "f1"}}}
    result = run_investigation(
        issue,
        tools,
        lambda **kwargs: backend,
        max_steps=2,
        repeated_tool_limit=3,
        runtime_config={
            "function_memory_enabled": False,
            "investigation_ledger_enabled": False,
            "no_progress_prompt_after": 1,
            "compact_tool_results": False,
        },
    )

    assert result["state"]["tool_history"][1]["execution_status"] == "BLOCKED"
    assert "Causal-progress gate" in result["state"]["tool_history"][1]["result"]["error"]
    assert tools.caller_queries == 1
    assert tools.callee_queries == 0
