import json
from pathlib import Path

from traversal.function_memory import ContextBuilder, FunctionUnderstandingMemory


def source(fid="fn1", text="1 | return x;"):
    return {
        "status": "SOURCE_VERIFIED",
        "function_id": fid,
        "function": {"qualified_name": "Thing::f", "relative_path": "src/thing.cpp", "signature": "int f()"},
        "source": {"start_line": 1, "actual_end_line": 1, "numbered_source": text},
    }


def test_persists_only_verified_metadata(tmp_path):
    path = tmp_path / "memory.jsonl"
    memory = FunctionUnderstandingMemory(path, "repo-a")
    assert not memory.remember_source({"status": "FAILED", "function_id": "fn1"})
    assert memory.remember_source(source(), evidence=[{"tool": "find_callees", "status": "OK", "observation": "calls Thing::g", "verified": False}])
    raw = path.read_text()
    assert "return x" not in raw
    assert "calls Thing::g" in raw
    assert len([line for line in raw.splitlines() if line.strip()]) == 1


def test_source_hash_changes_on_edit(tmp_path):
    memory = FunctionUnderstandingMemory(tmp_path / "memory.jsonl", "repo-a")
    memory.remember_source(source(text="1 | return x;"))
    memory.remember_source(source(text="1 | return y;"))
    rows = [json.loads(line) for line in memory.path.read_text().splitlines()]
    assert rows[0]["source_hash"] != rows[-1]["source_hash"]


def test_repeated_source_read_preserves_existing_notes(tmp_path):
    memory = FunctionUnderstandingMemory(tmp_path / "memory.jsonl", "repo-a")
    item = source()
    memory.remember_source(item, evidence=[{"tool": "find_callees", "status": "OK", "observation": "calls g", "verified": False}])
    memory.remember_source(item)
    assert "calls g" in memory.render(["fn1"])


def test_context_builder_scope_and_cap(tmp_path):
    memory = FunctionUnderstandingMemory(tmp_path / "memory.jsonl", "repo-a")
    memory.remember_source(source("fn1"), evidence=[{"tool": "graph", "observation": "relevant note", "verified": False}])
    memory.remember_source(source("fn2", "2 | return z;"), evidence=[{"tool": "graph", "observation": "other note", "verified": False}])
    rendered = ContextBuilder(memory, max_chars=300, max_functions=1).build(active_function_id="fn1")
    assert "Thing::f" in rendered
    assert "relevant note" in rendered
    assert len(rendered) <= 320
