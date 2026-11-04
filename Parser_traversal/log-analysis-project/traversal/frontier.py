from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class QuestionStatus(StrEnum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    CLOSED = "CLOSED"
    BLOCKED = "BLOCKED"


@dataclass
class FrontierQuestion:
    question_id: str
    kind: str
    question: str
    tool: str | None
    arguments: dict[str, Any]
    dependencies: list[str]
    priority: int
    status: QuestionStatus = QuestionStatus.PENDING
    evidence_refs: list[str] = field(default_factory=list)
    mandatory: bool = True


def _stable_id(kind: str, tool: str | None, arguments: dict[str, Any]) -> str:
    identity = json.dumps(
        {"kind": kind, "tool": tool, "arguments": arguments},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return "q_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


def _valid_result(question: FrontierQuestion, result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    if "error" in result:
        return False

    if question.tool == "read_function_source":
        source = result.get("source")
        return bool(
            result.get("status") == "SOURCE_VERIFIED"
            and isinstance(source, dict)
            and source.get("content_hash_matches") is True
            and source.get("expected_content_hash")
            and source.get("expected_content_hash")
            == source.get("matched_content_hash")
            and source.get("hash_match_variant")
            and source.get("source_snapshot_id")
        )

    if result.get("status") != "OK":
        return False

    list_keys = {
        "find_worker_launchers": ("launchers", "launcher_count"),
        "find_spawned_workers": ("spawned_workers", "spawned_worker_count"),
        "find_callees": ("project_callees", None),
        "find_callers": ("callers", "caller_count"),
        "list_fields_read": ("fields", "field_access_count"),
        "find_field_writers": ("writers", "writer_count"),
    }
    list_key, count_key = list_keys.get(question.tool or "", (None, None))
    if list_key is None or not isinstance(result.get(list_key), list):
        return False
    if count_key is not None:
        return result.get(count_key) == len(result[list_key])
    return isinstance(result.get("external_callees", []), list)


class CausalFrontier:
    """Orchestrator-owned causal questions and deterministic tool tasks."""

    def __init__(self, questions: list[FrontierQuestion] | None = None):
        self.questions: dict[str, FrontierQuestion] = {
            item.question_id: item for item in questions or []
        }

    @classmethod
    def from_dict(cls, value: Any) -> CausalFrontier:
        if not isinstance(value, dict):
            return cls()
        questions = []
        for item in value.get("questions", []):
            if not isinstance(item, dict):
                continue
            try:
                questions.append(FrontierQuestion(
                    question_id=str(item["question_id"]),
                    kind=str(item["kind"]),
                    question=str(item["question"]),
                    tool=item.get("tool"),
                    arguments=dict(item.get("arguments") or {}),
                    dependencies=[str(dep) for dep in item.get("dependencies", [])],
                    priority=int(item.get("priority", 0)),
                    status=QuestionStatus(item.get("status", "PENDING")),
                    evidence_refs=[str(ref) for ref in item.get("evidence_refs", [])],
                    mandatory=bool(item.get("mandatory", True)),
                ))
            except (KeyError, TypeError, ValueError):
                continue
        return cls(questions)

    def add(
        self,
        *,
        kind: str,
        question: str,
        tool: str | None,
        arguments: dict[str, Any] | None = None,
        dependencies: list[str] | None = None,
        priority: int = 0,
        mandatory: bool = True,
    ) -> FrontierQuestion:
        arguments = dict(arguments or {})
        question_id = _stable_id(kind, tool, arguments)
        existing = self.questions.get(question_id)
        if existing is not None:
            existing.dependencies = sorted(set(existing.dependencies) | set(dependencies or []))
            existing.priority = max(existing.priority, int(priority))
            return existing
        item = FrontierQuestion(
            question_id=question_id,
            kind=kind,
            question=question,
            tool=tool,
            arguments=arguments,
            dependencies=sorted(set(dependencies or [])),
            priority=int(priority),
            mandatory=mandatory,
        )
        self.questions[question_id] = item
        return item

    def ready(self) -> list[FrontierQuestion]:
        ready_questions = [
            item for item in self.questions.values()
            if item.tool is not None
            and item.status == QuestionStatus.PENDING
            and all(
                dependency in self.questions
                and self.questions[dependency].status == QuestionStatus.CLOSED
                for dependency in item.dependencies
            )
        ]
        return sorted(ready_questions, key=lambda item: (-item.priority, item.question_id))

    def mark_in_progress(self, question_id: str) -> None:
        item = self.questions[question_id]
        if item.status == QuestionStatus.PENDING:
            item.status = QuestionStatus.IN_PROGRESS

    def complete(
        self,
        question_id: str,
        result: Any,
        evidence_ref: str,
    ) -> bool:
        item = self.questions[question_id]
        if not evidence_ref or not _valid_result(item, result):
            item.status = QuestionStatus.BLOCKED
            return False
        item.status = QuestionStatus.CLOSED
        if evidence_ref not in item.evidence_refs:
            item.evidence_refs.append(evidence_ref)
        return True

    def close_aggregate(self, question_id: str) -> bool:
        item = self.questions[question_id]
        if item.tool is not None or not item.dependencies:
            return False
        dependencies = [self.questions.get(dep) for dep in item.dependencies]
        if any(dependency is None or dependency.status != QuestionStatus.CLOSED for dependency in dependencies):
            return False
        evidence_refs = sorted({
            evidence_ref
            for dependency in dependencies if dependency is not None
            for evidence_ref in dependency.evidence_refs
        })
        if not evidence_refs:
            return False
        item.status = QuestionStatus.CLOSED
        item.evidence_refs = evidence_refs
        return True

    def reopen(self, question_id: str) -> None:
        item = self.questions[question_id]
        item.status = QuestionStatus.PENDING

    def matching(self, tool: str, arguments: dict[str, Any]) -> FrontierQuestion | None:
        for item in self.questions.values():
            if item.tool == tool and item.arguments == arguments and item.status != QuestionStatus.BLOCKED:
                return item
        return None

    def get(self, question_id: str) -> FrontierQuestion | None:
        return self.questions.get(question_id)

    def first_open(self, kind: str) -> FrontierQuestion | None:
        return next((
            item for item in self.questions.values()
            if item.kind == kind and item.status != QuestionStatus.CLOSED
        ), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "questions": [
                asdict(item) | {"status": item.status.value}
                for item in sorted(self.questions.values(), key=lambda q: q.question_id)
            ],
        }


def _function_id(item: dict[str, Any]) -> str:
    value = item.get("function_id") or item.get("neptune_node_id") or ""
    return str(value).removeprefix("Function:")


def _function_name(item: dict[str, Any]) -> str:
    return str(
        item.get("qualified_name")
        or item.get("function_name")
        or item.get("function_id")
        or ""
    ).rsplit("::", 1)[-1]


def expand_s7_frontier(
    frontier: CausalFrontier,
    *,
    tool: str,
    arguments: dict[str, Any],
    result: dict[str, Any],
    evidence_ref: str,
) -> list[FrontierQuestion]:
    """Create mandatory S7 follow-up tasks from validated graph/source results."""
    opened: list[FrontierQuestion] = []

    if tool == "find_worker_launchers" and result.get("status") == "OK":
        if not _valid_result(
            FrontierQuestion("", "", "", tool, arguments, [], 0), result
        ):
            return opened
        if (
            _function_name({"function_id": arguments.get("function_id", "")}) != "runT2"
            and _function_name({"qualified_name": result.get("worker_qualified_name", "")}) != "runT2"
        ):
            return opened
        for launcher in result.get("launchers", []):
            if _function_name(launcher) != "runS7":
                continue
            launcher_id = _function_id(launcher)
            inspect_launcher = frontier.add(
                kind="inspect_s7_launcher",
                question="Inspect runS7 source before enumerating its workers.",
                tool="read_function_source",
                arguments={"function_id": launcher_id},
                priority=120,
            )
            enumerate_workers = frontier.add(
                kind="enumerate_s7_spawned_workers",
                question="Enumerate every worker spawned by runS7.",
                tool="find_spawned_workers",
                arguments={"function_id": launcher_id},
                dependencies=[inspect_launcher.question_id],
                priority=120,
            )
            opened.extend((inspect_launcher, enumerate_workers))

    elif tool == "find_spawned_workers" and result.get("status") == "OK":
        question = frontier.matching(tool, arguments)
        if question is None or not _valid_result(question, result):
            return opened
        workers = result.get("spawned_workers", [])
        worker_tasks = []
        worker_ids_by_name: dict[str, str] = {}
        for worker in workers:
            worker_id = _function_id(worker)
            if not worker_id:
                continue
            worker_name = _function_name(worker)
            worker_ids_by_name[worker_name] = worker_id
            priority = 110 if worker_name == "runT6" else 80
            task = frontier.add(
                kind="inspect_sibling_worker_source",
                question=f"Inspect spawned sibling worker {worker_name} source.",
                tool="read_function_source",
                arguments={"function_id": worker_id},
                dependencies=[question.question_id],
                priority=priority,
            )
            worker_tasks.append(task)
            opened.append(task)

        gate = frontier.add(
            kind="sibling_worker_gate",
            question="Complete source inspection for every spawned sibling worker.",
            tool=None,
            arguments={
                "launcher_function_id": arguments.get("function_id"),
                "t6_function_id": worker_ids_by_name.get("runT6", "runT6"),
                "observer_function_id": worker_ids_by_name.get("runT2", "runT2"),
            },
            dependencies=[task.question_id for task in worker_tasks],
            priority=105,
        )
        opened.append(gate)

    elif tool == "read_function_source" and result.get("status") == "SOURCE_VERIFIED":
        question = frontier.matching(tool, arguments)
        if question is None or not frontier.complete(
            question.question_id, result, evidence_ref
        ):
            return opened
        if question.kind == "inspect_s7_launcher":
            return opened

    elif tool == "find_callees" and result.get("status") == "OK":
        question = frontier.matching(tool, arguments)
        if question is None or not frontier.complete(
            question.question_id, result, evidence_ref
        ):
            return opened
        callees = result.get("project_callees", [])
        if question.kind == "trace_runT6_callees":
            for callee in callees:
                if _function_name(callee) == "forward":
                    forward_id = _function_id(callee)
                    forward_source = frontier.add(
                        kind="inspect_transport_forward",
                        question="Inspect transport.forward() on the runT6 path.",
                        tool="read_function_source",
                        arguments={"function_id": forward_id},
                        dependencies=[question.question_id],
                        priority=110,
                    )
                    opened.append(forward_source)
                    opened.append(frontier.add(
                        kind="trace_transport_forward_callees",
                        question="Trace project callees of transport.forward().",
                        tool="find_callees",
                        arguments={"function_id": forward_id},
                        dependencies=[forward_source.question_id],
                        priority=110,
                    ))
        elif question.kind == "trace_transport_forward_callees":
            for callee in callees:
                if _function_name(callee) == "releaseCurrent":
                    opened.append(frontier.add(
                        kind="inspect_release_current",
                        question="Inspect releaseCurrent() on the T6 forwarding path.",
                        tool="read_function_source",
                        arguments={"function_id": _function_id(callee)},
                        dependencies=[question.question_id],
                        priority=110,
                    ))
        elif question.kind == "trace_runT2_callees":
            for callee in callees:
                if _function_name(callee) == "observe":
                    opened.append(frontier.add(
                        kind="inspect_observer_source",
                        question="Inspect the observer worker reached from runT2.",
                        tool="read_function_source",
                        arguments={"function_id": _function_id(callee)},
                        dependencies=[question.question_id],
                        priority=85,
                    ))
    return opened