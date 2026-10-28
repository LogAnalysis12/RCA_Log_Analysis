import json

from traversal.investigator import _guidance_first_tool_result_content


def test_loop_guidance_survives_large_tool_result_compaction():
    guidance = (
        "LOOP DETECTED: stop repeating completed queries.\n"
        "CURRENT INVESTIGATION CHECKPOINT (JSON): {\"root_cause_status\": \"NOT_ESTABLISHED\"}"
    )
    large_tool_result = {"payload": "x" * 7000}

    content = _guidance_first_tool_result_content(guidance, large_tool_result)
    serialized = json.dumps(content, ensure_ascii=False, default=str)
    # The active history compactor keeps only the first 4000 chars of large tool results.
    compacted = serialized[:4000]

    assert list(content.keys()) == ["controller_guidance", "tool_result"]
    assert "LOOP DETECTED" in compacted
    assert "CURRENT INVESTIGATION CHECKPOINT" in compacted


def test_original_tool_result_is_preserved_after_guidance():
    result = {"status": "OK", "caller_count": 0, "callers": []}
    content = _guidance_first_tool_result_content("NO NEW EVIDENCE", result)
    assert content["tool_result"] == result
