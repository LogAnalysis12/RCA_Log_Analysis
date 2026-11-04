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
            "matched_content_hash": "hash-a",
            "hash_match_variant": "LF_NO_FINAL_NEWLINE",
            "source_snapshot_id": "snapshot-a",
        },
    }
    assert _meaningful_evidence_fingerprint("read_function_source", {}, source_result, {})
    assert _meaningful_evidence_fingerprint(
        "read_function_source", {}, {**source_result, "cache_hit": True}, {},
    ) is None
    source_result["source"]["content_hash_matches"] = False
    assert _meaningful_evidence_fingerprint("read_function_source", {}, source_result, {}) is None


def test_lf_final_newline_source_has_valid_progress_fingerprint():
    result = {
        "status": "SOURCE_VERIFIED",
        "function_id": "f1",
        "function": {"relative_path": "src/a.cpp"},
        "source": {
            "content_hash_matches": True,
            "expected_content_hash": "final-newline-hash",
            "computed_content_hash": "no-final-newline-hash",
            "matched_content_hash": "final-newline-hash",
            "hash_match_variant": "LF_FINAL_NEWLINE",
            "source_snapshot_id": "snapshot-a",
            "start_line": 5,
            "actual_end_line": 8,
        },
    }

    fingerprint = _meaningful_evidence_fingerprint(
        "read_function_source", {}, result, {}
    )

    assert fingerprint is not None
    assert "final-newline-hash" in fingerprint
    assert "LF_FINAL_NEWLINE" in fingerprint
    assert "snapshot-a" in fingerprint


def test_unverified_and_integrity_error_sources_never_count_as_progress():
    result = {
        "status": "SOURCE_VERIFIED",
        "function_id": "f1",
        "function": {"relative_path": "src/a.cpp"},
        "source": {
            "content_hash_matches": True,
            "expected_content_hash": "hash-a",
            "matched_content_hash": "hash-a",
            "hash_match_variant": "LF_NO_FINAL_NEWLINE",
            "source_snapshot_id": "snapshot-a",
        },
    }

    for status in (
        "SOURCE_READ_UNVERIFIED",
        "SOURCE_HASH_MISMATCH",
        "CACHE_INTEGRITY_ERROR",
    ):
        failed_result = {**result, "status": status, "error": "not verified"}
        assert _meaningful_evidence_fingerprint(
            "read_function_source", {}, failed_result, {}
        ) is None


def test_changed_verified_source_snapshot_is_new_evidence():
    result = {
        "status": "SOURCE_VERIFIED",
        "function_id": "f1",
        "function": {"relative_path": "src/a.cpp"},
        "source": {
            "content_hash_matches": True,
            "expected_content_hash": "hash-a",
            "computed_content_hash": "hash-a",
            "matched_content_hash": "hash-a",
            "hash_match_variant": "LF_NO_FINAL_NEWLINE",
            "source_snapshot_id": "snapshot-a",
            "start_line": 5,
            "actual_end_line": 8,
        },
    }
    original = _meaningful_evidence_fingerprint(
        "read_function_source", {}, result, {}
    )
    changed = {
        **result,
        "source": {**result["source"], "source_snapshot_id": "snapshot-b"},
    }

    assert _meaningful_evidence_fingerprint(
        "read_function_source", {}, changed, {}
    ) != original


def test_investigation_progress_resets_for_first_source_but_not_cache_replay():
    class Tools:
        def find_callers(self, function_id, limit):
            return {"status": "OK", "caller_count": 0, "callers": []}

        def read_function_source(self, function_id):
            return {
                "status": "SOURCE_VERIFIED",
                "function_id": function_id,
                "function": {"relative_path": "src/a.cpp"},
                "source": {
                    "content_hash_matches": True,
                    "expected_content_hash": "hash-a",
                    "computed_content_hash": "hash-a",
                    "matched_content_hash": "hash-a",
                    "hash_match_variant": "LF_NO_FINAL_NEWLINE",
                    "source_snapshot_id": "snapshot-a",
                    "start_line": 1,
                    "actual_end_line": 2,
                    "numbered_source": "1 | a\n2 | b",
                },
            }

        def find_callees(self, function_id, limit):
            return {"status": "OK", "project_callees": [], "external_callees": []}

    class Backend:
        response_count = 0

        def send(self, content):
            return StepResult(tool_calls=[
                ToolCall("a", "find_callers", {"function_id": "f1"})
            ])

        def send_tool_results(self, results):
            self.response_count += 1
            if self.response_count == 1:
                return StepResult(tool_calls=[ToolCall(
                        "b", "read_function_source", {
                            "function_id": "f1",
                                "decision": {
                                    "question_to_resolve": "Verify source for f1",
                                    "backtrack_reason": "Validate the source cache",
                                },
                        }
                )])
            if self.response_count == 2:
                return StepResult(tool_calls=[ToolCall(
                    "c", "find_callees", {"function_id": "f1"}
                )])
            if self.response_count == 3:
                return StepResult(tool_calls=[ToolCall(
                    "d", "read_function_source", {
                        "function_id": "f1",
                        "decision": {
                            "question_to_resolve": "Replay cached source",
                            "backtrack_reason": "Validate the source cache",
                        },
                    }
                )])
            return StepResult(tool_calls=[ToolCall(
                "e", "find_callers", {"function_id": "f2"}
            )])

    backend = Backend()
    result = run_investigation(
        {"anchor_resolution": {"selected_function": {"function_id": "f1"}}},
        Tools(),
        lambda **kwargs: backend,
        max_steps=5,
        repeated_tool_limit=3,
        runtime_config={
            "function_memory_enabled": False,
            "investigation_ledger_enabled": False,
            "no_progress_prompt_after": 1,
            "compact_tool_results": False,
        },
    )

    history = result["state"]["tool_history"]
    assert history[1]["new_evidence"] is True
    assert history[2]["execution_status"] == "EXECUTED"
    assert history[3]["result"]["cache_hit"] is True
    assert history[3]["new_evidence"] is False
    assert history[4]["execution_status"] == "BLOCKED"
    assert "Causal-progress gate" in history[4]["result"]["error"]


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
