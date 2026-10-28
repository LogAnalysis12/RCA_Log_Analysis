from traversal.compact_results import compact_tool_result
from traversal.investigator import _cached_source_replay


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
            "numbered_source": (
                "22 | void releaseCurrent(S7State& state) {\n"
                "27 |     state.current.reset();\n"
                "33 | }\n"
            ),
        },
    }


def test_verified_source_cache_replays_once():
    key = "read_function_source:fn_test123"
    replayed = set()
    cached = _verified_source()

    first = _cached_source_replay(key, cached, replayed)

    assert first is not None
    assert first["status"] == "SOURCE_VERIFIED"
    assert first["function_id"] == "Function:fn_test123"
    assert first["function"]["qualified_name"] == (
        "S7ResourceManager::releaseCurrent"
    )
    assert first["source"]["start_line"] == 22
    assert first["source"]["actual_end_line"] == 33
    assert first["cache_hit"] is True
    assert "different unresolved causal question" in (
        first["continuation_instruction"]
    )

    # Further identical cache replays must not keep returning the same source.
    assert _cached_source_replay(key, cached, replayed) is None


def test_cache_replay_refuses_unverified_source():
    replayed = set()
    cached = {
        **_verified_source(),
        "status": "TOOL_ERROR",
    }

    assert _cached_source_replay("read:fn_test123", cached, replayed) is None


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
    assert "state.current.reset()" in compacted["numbered_source"]
    assert compacted["cache_hit"] is True
    assert "Do not reread source" in compacted["continuation_instruction"]
