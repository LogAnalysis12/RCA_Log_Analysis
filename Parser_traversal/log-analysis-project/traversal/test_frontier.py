from traversal.frontier import CausalFrontier, QuestionStatus


def test_frontier_ids_are_stable_and_tasks_are_deduplicated():
    frontier = CausalFrontier()
    first = frontier.add(
        kind="enumerate_workers",
        question="Enumerate runS7 workers",
        tool="find_spawned_workers",
        arguments={"function_id": "runS7"},
        priority=90,
    )
    duplicate = frontier.add(
        kind="enumerate_workers",
        question="Enumerate runS7 workers again",
        tool="find_spawned_workers",
        arguments={"function_id": "runS7"},
        priority=100,
    )

    assert first is duplicate
    assert first.question_id == duplicate.question_id
    assert first.priority == 100


def test_frontier_dependencies_and_evidence_validated_closure():
    frontier = CausalFrontier()
    launcher = frontier.add(
        kind="inspect_launcher",
        question="Inspect runS7",
        tool="read_function_source",
        arguments={"function_id": "runS7"},
        priority=100,
    )
    workers = frontier.add(
        kind="enumerate_workers",
        question="Enumerate runS7 workers",
        tool="find_spawned_workers",
        arguments={"function_id": "runS7"},
        dependencies=[launcher.question_id],
        priority=100,
    )
    assert frontier.ready() == [launcher]
    assert not frontier.complete(workers.question_id, {"status": "OK", "spawned_workers": []}, "graph:workers")
    assert workers.status == QuestionStatus.BLOCKED

    frontier.reopen(workers.question_id)
    source_result = {
        "status": "SOURCE_VERIFIED",
        "source": {
            "content_hash_matches": True,
            "expected_content_hash": "source-hash",
            "matched_content_hash": "source-hash",
            "hash_match_variant": "LF_FINAL_NEWLINE",
            "source_snapshot_id": "snapshot",
        },
    }
    assert frontier.complete(launcher.question_id, source_result, "source:runS7")
    assert frontier.ready() == [workers]
    worker_result = {
        "status": "OK",
        "spawned_workers": [{"function_id": "runT6"}],
        "spawned_worker_count": 1,
    }
    assert frontier.complete(workers.question_id, worker_result, "graph:workers")
    assert workers.status == QuestionStatus.CLOSED
    assert workers.evidence_refs == ["graph:workers"]


def test_frontier_serialization_round_trip_preserves_lifecycle():
    frontier = CausalFrontier()
    question = frontier.add(
        kind="inspect_source",
        question="Inspect source",
        tool="read_function_source",
        arguments={"function_id": "f1"},
        priority=5,
    )
    result = {
        "status": "SOURCE_VERIFIED",
        "source": {
            "content_hash_matches": True,
            "expected_content_hash": "hash",
            "matched_content_hash": "hash",
            "hash_match_variant": "LF_NO_FINAL_NEWLINE",
            "source_snapshot_id": "snapshot",
        },
    }
    frontier.complete(question.question_id, result, "source:f1")

    restored = CausalFrontier.from_dict(frontier.to_dict())

    assert restored.to_dict() == frontier.to_dict()
    assert restored.get(question.question_id).status == QuestionStatus.CLOSED