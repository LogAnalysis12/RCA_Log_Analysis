from __future__ import annotations

from collections import Counter

from traversal.llm_backends import StepResult, ToolCall
from traversal.frontier import CausalFrontier, QuestionStatus
from traversal.investigator import run_investigation


class FakeLLMBackend:
	def __init__(self, policy):
		self.policy = list(policy)
		self.actions = Counter()
		self.turn = 0

	def send(self, content):
		return self._next()

	def send_tool_results(self, results):
		return self._next()

	def _next(self):
		if not self.policy:
			return StepResult()
		name, arguments = self.policy.pop(0)
		self.turn += 1
		self.actions[(name, str(arguments))] += 1
		return StepResult(tool_calls=[ToolCall(str(self.turn), name, arguments)])


class S7Graph:
	workers = [f"runT{worker}" for worker in range(1, 9)]

	def __init__(self):
		self.calls = []

	@staticmethod
	def _id(value):
		return str(value).removeprefix("Function:")

	def read_function_source(self, function_id):
		function_id = self._id(function_id)
		self.calls.append(("read_function_source", function_id, {}))
		if function_id == "runT3" and getattr(self, "unreadable_t3", False):
			return {"status": "SOURCE_READ_FAILED", "function_id": function_id}
		digest = f"sha256:{function_id}"
		return {
			"status": "SOURCE_VERIFIED",
			"function_id": function_id,
			"function": {"qualified_name": function_id, "relative_path": "src/scenario_s7.cpp"},
			"source": {
				"numbered_source": f"1 | {function_id}\\n",
				"content_hash_matches": True,
				"expected_content_hash": digest,
				"matched_content_hash": digest,
				"hash_match_variant": "LF_FINAL_NEWLINE",
				"source_snapshot_id": digest,
				"start_line": 1,
				"actual_end_line": 1,
			},
		}

	def find_worker_launchers(self, function_id, limit=100):
		function_id = self._id(function_id)
		self.calls.append(("find_worker_launchers", function_id, {"limit": limit}))
		launchers = [{"function_id": "runS7", "qualified_name": "runS7"}] if function_id == "runT2" else []
		return {
			"status": "OK", "worker_function_id": function_id,
			"worker_qualified_name": "CS7Workers::runT2" if function_id == "runT2" else function_id,
			"launcher_count": len(launchers), "launchers": launchers,
		}

	def find_spawned_workers(self, function_id, limit=100):
		function_id = self._id(function_id)
		self.calls.append(("find_spawned_workers", function_id, {"limit": limit}))
		workers = [{"function_id": name, "qualified_name": name} for name in self.workers] if function_id == "runS7" else []
		return {
			"status": "OK", "launcher_function_id": function_id,
			"spawned_worker_count": len(workers), "spawned_workers": workers,
		}

	def find_callees(self, function_id, limit=40):
		function_id = self._id(function_id)
		self.calls.append(("find_callees", function_id, {"limit": limit}))
		mapping = {
			"runT6": ["forward"], "forward": ["releaseCurrent"],
			"runT2": ["observe"],
		}
		callees = [{"function_id": name, "qualified_name": name} for name in mapping.get(function_id, [])]
		return {
			"status": "OK", "caller_function_id": function_id,
			"project_callees": callees, "external_callees": [],
		}

	def find_callers(self, function_id, limit=30):
		function_id = self._id(function_id)
		self.calls.append(("find_callers", function_id, {"limit": limit}))
		return {"status": "OK", "target_function_id": function_id, "caller_count": 0, "callers": []}


def _issue():
	return {
		"anchor_resolution": {
			"selected_function": {
				"function_id": "runT2",
				"qualified_name": "CS7Workers::runT2",
				"relative_path": "src/scenario_s7.cpp",
			}
		}
	}


def _run(graph, backend):
	return run_investigation(
		_issue(),
		graph,
		lambda **kwargs: backend,
		max_steps=2,
		repeated_tool_limit=4,
		runtime_config={
			"function_memory_enabled": False,
			"investigation_ledger_enabled": False,
			"compact_tool_results": False,
			"max_frontier_dispatches": 100,
		},
	)


def test_run_s7_workers_are_dispatched_before_caller_traversal():
	graph = S7Graph()
	backend = FakeLLMBackend([
		("find_worker_launchers", {"function_id": "runT2"}),
		("find_callers", {"function_id": "releaseCurrent"}),
	])

	result = _run(graph, backend)

	calls = graph.calls
	launcher_index = calls.index(("find_worker_launchers", "runT2", {"limit": 100}))
	inspect_s7_index = calls.index(("read_function_source", "runS7", {}))
	spawned_index = calls.index(("find_spawned_workers", "runS7", {"limit": 100}))
	caller_index = calls.index(("find_callers", "releaseCurrent", {"limit": 30}))
	sibling_source_indices = [
		index for index, call in enumerate(calls)
		if call[0] == "read_function_source" and call[1].startswith("runT")
	]

	assert launcher_index < inspect_s7_index < spawned_index
	assert len(sibling_source_indices) == 8
	assert max(sibling_source_indices) < caller_index
	assert all(call[0] != "find_spawned_workers" for call, count in backend.actions.items() if count)
	assert ("find_callees", "runT6", {"limit": 40}) in calls
	assert ("find_callees", "forward", {"limit": 40}) in calls
	assert ("read_function_source", "releaseCurrent", {}) in calls
	assert ("find_callees", "runT2", {"limit": 40}) in calls
	assert ("read_function_source", "observe", {}) in calls

	frontier = CausalFrontier.from_dict(result["state"]["causal_frontier"])
	gate = next(
		item for item in frontier.questions.values()
		if item.kind == "sibling_worker_gate"
	)
	assert gate is not None
	assert gate.status == QuestionStatus.CLOSED
	assert gate.evidence_refs


def test_sibling_gate_blocks_callers_until_all_worker_sources_validate():
	graph = S7Graph()
	graph.unreadable_t3 = True
	backend = FakeLLMBackend([
		("find_worker_launchers", {"function_id": "runT2"}),
		("find_callers", {"function_id": "releaseCurrent"}),
	])

	result = _run(graph, backend)

	assert not any(call[0] == "find_callers" for call in graph.calls)
	blocked = [
		item for item in result["state"]["tool_history"]
		if item["tool"] == "find_callers"
	]
	assert blocked
	assert blocked[0]["execution_status"] == "BLOCKED"
	assert blocked[0]["result"]["status"] == "FRONTIER_GATE_BLOCKED"
	frontier = CausalFrontier.from_dict(result["state"]["causal_frontier"])
	gate = frontier.first_open("sibling_worker_gate")
	assert gate is not None
	assert gate.status != QuestionStatus.CLOSED


def test_cached_sibling_source_closes_question_without_new_evidence():
	graph = S7Graph()
	backend = FakeLLMBackend([
		("read_function_source", {"function_id": "runT2"}),
		("find_worker_launchers", {"function_id": "runT2"}),
	])

	result = _run(graph, backend)

	cached_sibling = next(
		item for item in result["state"]["tool_history"]
		if item.get("frontier_question_id")
		and item["tool"] == "read_function_source"
		and item["input"].get("function_id") == "runT2"
	)
	assert cached_sibling["execution_status"] == "CACHED"
	assert cached_sibling["question_closed"] is True
	assert cached_sibling["new_evidence"] is False
	assert len([
		call for call in graph.calls
		if call[0] == "read_function_source" and call[1] == "runT2"
	]) == 1
