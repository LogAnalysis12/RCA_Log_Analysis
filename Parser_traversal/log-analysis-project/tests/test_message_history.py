from traversal.message_history import trim_message_history


def test_trimming_keeps_tool_call_with_matching_result():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "initial task"},
        {"role": "assistant", "content": [{"type": "text", "text": "old"}]},
        {"role": "assistant", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "result"},
        {"role": "assistant", "tool_calls": [{"id": "c2", "type": "function", "function": {"name": "g", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c2", "content": "latest"},
    ]
    out = trim_message_history(messages, max_chars=2000, keep_recent_cycles=1)
    # Message history is already under minimum clamp (2000), so it's returned unchanged.
    assert out == messages


def test_oversized_history_keeps_first_task_and_recent_pairs():
    messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "task"}]
    for i in range(6):
        messages.extend([
            {"role": "assistant", "tool_calls": [{"id": f"c{i}", "type": "function", "function": {"name": "f", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": f"c{i}", "content": "x" * 800},
        ])
    out = trim_message_history(messages, max_chars=3000, keep_recent_cycles=2)
    assert out[0]["role"] == "system"
    assert out[1] == {"role": "user", "content": "task"}
    call_ids = {c["id"] for m in out for c in m.get("tool_calls", [])}
    result_ids = {m["tool_call_id"] for m in out if m.get("role") == "tool"}
    assert call_ids == result_ids
    assert len(call_ids) <= 2
