from traversal.neptune_tools import NeptuneTraversalTools
from traversal.tool_definitions import TOOLS
from traversal.investigator import _execute_tool


def test_find_worker_launchers_uses_reverse_threadspawn_pattern():
    tools = NeptuneTraversalTools.__new__(NeptuneTraversalTools)
    captured = {}

    def fake_execute(query, params):
        captured["query"] = query
        captured["params"] = params
        return [{
            "worker_neptune_node_id": "worker-1",
            "worker_qualified_name": "CS7Workers::runT6",
            "launcher_neptune_node_id": "launcher-1",
            "launcher_qualified_name": "runS7",
            "launcher_function_name": "runS7",
            "launcher_class_name": "",
            "launcher_signature": "void runS7()",
            "launcher_relative_path": "src/scenario_s7.cpp",
            "launcher_start_line": 110,
            "launcher_end_line": 130,
            "launcher_content_hash": "hash",
            "spawn_line": 120,
            "spawn_relative_path": "src/scenario_s7.cpp",
            "spawn_expression": "std::thread(&CS7Workers::runT6, ...)",
            "spawn_evidence": {},
            "launcher_spawn_relationship_properties": {},
            "spawn_worker_relationship_properties": {},
        }]

    tools._execute = fake_execute

    result = tools.find_worker_launchers("Function:worker-1")

    assert "MATCH (launcher:Function)-[r1:SPAWNS_THREAD]->(spawn:ThreadSpawn)" in captured["query"]
    assert "-[r2:SPAWNS_THREAD]->(worker:Function)" in captured["query"]
    assert "WHERE worker.node_id = $id" in captured["query"]
    assert result["status"] == "OK"
    assert result["launcher_count"] == 1
    assert result["launchers"][0]["qualified_name"] == "runS7"
    assert result["launchers"][0]["spawn_site"]["line"] == 120


def test_worker_launcher_tool_is_registered_and_dispatched():
    names = [tool["name"] for tool in TOOLS]
    assert "find_worker_launchers" in names

    class FakeTools:
        def find_worker_launchers(self, function_id, limit=100):
            return {
                "status": "OK",
                "worker_function_id": function_id,
                "launcher_count": 0,
                "launchers": [],
            }

    result = _execute_tool(
        FakeTools(),
        "find_worker_launchers",
        {"function_id": "worker-1"},
        {},
    )
    assert result["status"] == "OK"
    assert result["worker_function_id"] == "worker-1"
