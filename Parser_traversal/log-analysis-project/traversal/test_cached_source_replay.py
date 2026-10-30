from types import SimpleNamespace

from traversal.compact_results import compact_tool_result
from traversal.investigator import _cached_source_replay, _tool_key, run_investigation
from traversal.llm_backends import StepResult, ToolCall


def _verified_source():
    return {
        "status": "SOURCE_VERIFIED",
        "function_id": "Function:fn_test123",
        "function": {
            "qualified_name": "S7ResourceManager::releaseCurrent",
            "signature": "void (S7State &)",
            "relative_path": "src/scenario_s7.cpp",
        },
        "source": {
            "start_line": 22,
            "actual_end_line": 33,
            "expected_content_hash": "verified-hash",
            "computed_content_hash": "verified-hash",
            "content_hash_matches": True,
            "hash_match_variant": "LF_NO_FINAL_NEWLINE",
            "numbered_source": (
                "22 | void releaseCurrent(S7State& state) {\n"
                "27 |     state.current.reset();\n"
                "33 | }\n"
            ),
        },
    }


def test_verified_source_cache_replays_identical_evidence():
    key = "read_function_source:fn_test123"
    cached = _verified_source()

    first = _cached_source_replay(key, cached)

    assert first is not None
    assert first["status"] == "SOURCE_VERIFIED"
    assert first["function_id"] == "Function:fn_test123"
    assert first["function"]["qualified_name"] == (
        "S7ResourceManager::releaseCurrent"
    )
    assert first["source"]["start_line"] == 22
    assert first["source"]["actual_end_line"] == 33
    assert first["source"]["content_hash_matches"] is True
    assert first["source"]["computed_content_hash"] == "verified-hash"
    assert "numbered_source" not in first["source"]
    assert first["source_reused"] is True
    assert first["cache_hit"] is True
    assert "different unresolved causal question" in (
        first["continuation_instruction"]
    )

    second = _cached_source_replay(key, cached)
    assert second is not None
    assert second["status"] == "SOURCE_VERIFIED"
    assert second["source"]["start_line"] == cached["source"]["start_line"]
    assert second["source"]["actual_end_line"] == cached["source"]["actual_end_line"]
    assert second["source"]["computed_content_hash"] == cached["source"]["computed_content_hash"]
    assert second["function"] == cached["function"]
    assert second["cache_hit"] is True


def test_cache_replay_refuses_unverified_source():
    cached = {
        **_verified_source(),
        "status": "TOOL_ERROR",
    }

    assert _cached_source_replay("read:fn_test123", cached) is None

    missing_hash_proof = _verified_source()
    missing_hash_proof["source"].pop("content_hash_matches")
    assert _cached_source_replay("read:fn_test123", missing_hash_proof) is None

    mismatched_hash = _verified_source()
    mismatched_hash["source"]["content_hash_matches"] = False
    assert _cached_source_replay("read:fn_test123", mismatched_hash) is None

    inconsistent_hashes = _verified_source()
    inconsistent_hashes["source"]["computed_content_hash"] = "different-hash"
    assert _cached_source_replay("read:fn_test123", inconsistent_hashes) is None


def test_tool_request_key_distinguishes_arguments_and_normalizes_argument_order():
    first = _tool_key("find_callers", {"function_id": "fn_a", "limit": 4})
    reordered = _tool_key(" FIND_CALLERS ", {"limit": 4, "function_id": "fn_a"})
    different = _tool_key("find_callers", {"function_id": "fn_b", "limit": 4})

    assert first == reordered
    assert first != different


def test_identical_source_requests_do_not_reread_the_file():
    class Tools:
        read_count = 0

        def read_function_source(self, function_id):
            self.read_count += 1
            return _verified_source()

    class Backend:
        results = []

        def send(self, content):
            return _read_step("first")

        def send_tool_results(self, results):
            self.results.append(results[0]["content"])
            if len(self.results) == 1:
                return _read_step("second")
            return StepResult(usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})

    tools = Tools()
    backend = Backend()
    issue = {"anchor_resolution": {"selected_function": {"function_id": "fn_test123"}}}

    result = run_investigation(
        issue,
        tools,
        lambda **kwargs: backend,
        max_steps=2,
        stall_retries=0,
        runtime_config={
            "function_memory_enabled": False,
            "investigation_ledger_enabled": False,
            "compact_tool_results": False,
        },
    )

    assert result["status"] == "MAX_STEPS_REACHED"
    assert tools.read_count == 1
    assert backend.results[0]["status"] == "SOURCE_VERIFIED"
    replay = backend.results[1].get("tool_result", backend.results[1])
    assert replay["status"] == "SOURCE_VERIFIED"
    assert replay["cache_hit"] is True


def test_repeated_source_cache_hits_still_obey_the_action_limit():
    class Tools:
        read_count = 0

        def read_function_source(self, function_id):
            self.read_count += 1
            return _verified_source()

    class Backend:
        results = []

        def send(self, content):
            return _read_step("first")

        def send_tool_results(self, results):
            self.results.append(results[0]["content"])
            if len(self.results) < 3:
                return _read_step(f"read-{len(self.results) + 1}")
            return StepResult(usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})

    tools = Tools()
    backend = Backend()
    issue = {"anchor_resolution": {"selected_function": {"function_id": "fn_test123"}}}

    result = run_investigation(
        issue,
        tools,
        lambda **kwargs: backend,
        max_steps=3,
        stall_retries=0,
        repeated_tool_limit=1,
        runtime_config={
            "function_memory_enabled": False,
            "investigation_ledger_enabled": False,
            "compact_tool_results": False,
        },
    )

    assert result["status"] == "MAX_STEPS_REACHED"
    assert tools.read_count == 1
    recorded = result["state"]["tool_history"]
    assert recorded[1]["result"]["status"] == "SOURCE_VERIFIED"
    assert recorded[1]["result"]["cache_hit"] is True
    assert recorded[1]["new_evidence"] is False
    assert recorded[2]["result"]["status"] == "TOOL_ERROR"
    assert "allowed replays" in recorded[2]["result"]["error"]


def test_failed_source_reads_are_not_cached():
    class Tools:
        read_count = 0

        def read_function_source(self, function_id):
            self.read_count += 1
            return {"status": "TOOL_ERROR", "error": "source unavailable"}

    class Backend:
        results = []

        def send(self, content):
            return _read_step("failed-first")

        def send_tool_results(self, results):
            self.results.append(results[0]["content"])
            if len(self.results) == 1:
                return _read_step("failed-second")
            return StepResult()

    tools = Tools()
    backend = Backend()
    issue = {"anchor_resolution": {"selected_function": {"function_id": "fn_test123"}}}

    result = run_investigation(
        issue,
        tools,
        lambda **kwargs: backend,
        max_steps=2,
        stall_retries=0,
        repeated_tool_limit=2,
        runtime_config={
            "function_memory_enabled": False,
            "investigation_ledger_enabled": False,
            "compact_tool_results": False,
        },
    )

    assert result["status"] == "MAX_STEPS_REACHED"
    assert tools.read_count == 2
    assert all("cache_hit" not in item for item in result["state"]["tool_history"])


def _read_step(call_id):
    return StepResult(
        tool_calls=[ToolCall(call_id, "read_function_source", {"function_id": "fn_test123"})],
        usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )


def test_source_compaction_preserves_cache_guidance():
    result = _verified_source()
    result["cache_hit"] = True
    result["continuation_instruction"] = (
        "Do not reread source. Investigate a different unresolved question."
    )

    compacted = compact_tool_result(
        "read_function_source",
        result,
        {
            "max_source_characters_per_function": 12000,
            "max_tool_result_characters": 16000,
        },
    )

    assert compacted["status"] == "SOURCE_VERIFIED"
    assert compacted["function_id"] == "fn_test123"
    assert compacted["qualified_name"] == (
        "S7ResourceManager::releaseCurrent"
    )
    assert compacted["relative_path"] == "src/scenario_s7.cpp"
    assert compacted["start_line"] == 22
    assert compacted["end_line"] == 33
    assert compacted["content_hash_matches"] is True
    assert compacted["expected_content_hash"] == "verified-hash"
    assert compacted["computed_content_hash"] == "verified-hash"
    assert "state.current.reset()" in compacted["numbered_source"]
    assert compacted["cache_hit"] is True
    assert "Do not reread source" in compacted["continuation_instruction"]
