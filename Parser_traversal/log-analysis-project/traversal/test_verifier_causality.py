from types import SimpleNamespace

from traversal.verifier import verify_conclusion


Q2 = "fn_q2"
Q1 = "fn_q1"
RUN = "fn_run_s1"
Q3 = "fn_q3"


def read_result(fid, name, path, signature, source, start=1):
    return {
        "status": "SOURCE_VERIFIED",
        "function_id": fid,
        "function": {
            "function_id": fid,
            "qualified_name": name,
            "relative_path": path,
            "signature": signature,
        },
        "source": {
            "source": source,
            "start_line": start,
            "actual_end_line": start + len(source.splitlines()) - 1,
        },
    }


def caller(fid, name, signature, expression, relationship="CALLS"):
    return {
        "function_id": fid,
        "qualified_name": name,
        "signature": signature,
        "call_sites": [
            {"expression": expression, "relationship_type": relationship}
        ],
    }


def setup(full=False):
    signature = "void (Resource *&)"

    q2_source = """void CWorkerService::q2(Resource*& var) {
    if (var == nullptr) { throw CDemoException(DemoError::VAR_NOT_AVAILABLE); }
    try_use(var->name);
    catch_all_log_unknown();
}"""

    q1_source = """void CWorkerService::q1(Resource*& var) {
    SleepMs(100);
    q2(var);
}"""

    reads = {
        Q2: read_result(
            Q2, "CWorkerService::q2", "src/file1_service.cpp",
            signature, q2_source,
        ),
        Q1: read_result(
            Q1, "CWorkerService::q1", "src/file1_service.cpp",
            signature, q1_source,
        ),
    }

    history = [{
        "tool": "find_callers",
        "input": {"function_id": Q2},
        "result": {
            "callers": [
                caller(Q1, "CWorkerService::q1", signature, "q2(var)")
            ]
        },
    }]

    if full:
        run_source = """void CScenarioRunner::runS1() {
    Resource* var = new Resource{};
    std::thread t1(&CWorkerService::q1, &service, std::ref(var));
    std::thread t2(&CResourceOwner::q3, &owner, std::ref(var));
    t1.join(); t2.join();
}"""

        q3_source = """void CResourceOwner::q3(Resource*& var) {
    SleepMs(20);
    delete var;
    var = nullptr;
}"""

        reads[RUN] = read_result(
            RUN, "CScenarioRunner::runS1", "src/main.cpp",
            "void ()", run_source, 50,
        )
        reads[Q3] = read_result(
            Q3, "CResourceOwner::q3", "src/file3_owner.cpp",
            signature, q3_source, 5,
        )

        history.extend([
            {
                "tool": "find_callers",
                "input": {"function_id": Q1},
                "result": {
                    "callers": [
                        caller(
                            RUN, "CScenarioRunner::runS1", "void ()",
                            "std::thread t1(&CWorkerService::q1, &service, std::ref(var))",
                            "SPAWNS",
                        )
                    ]
                },
            },
            {
                "tool": "find_callees",
                "input": {"function_id": RUN},
                "result": {
                    "project_callees": [
                        caller(
                            Q1, "CWorkerService::q1", signature,
                            "std::thread t1(&CWorkerService::q1, &service, std::ref(var))",
                            "SPAWNS",
                        ),
                        caller(
                            Q3, "CResourceOwner::q3", signature,
                            "std::thread t2(&CResourceOwner::q3, &owner, std::ref(var))",
                            "SPAWNS",
                        ),
                    ]
                },
            },
        ])

    state = SimpleNamespace(
        anchor={
            "function_id": Q2,
            "qualified_name": "CWorkerService::q2",
            "signature": signature,
        },
        read_functions=reads,
        tool_history=history,
    )

    conclusion = {
        "root_cause_function_id": Q2,
        "root_cause_qualified_name": "CWorkerService::q2",
        "suspicious_value": "var (Resource*&) ",
        "value_origin": "FUNCTION_PARAMETER",
        "relative_path": "src/file1_service.cpp",
        "line": 2,
        "explanation": "The null guard throws an exception and the catch-all logs Unknown exception.",
        "external_boundary": False,
    }

    return state, conclusion


def test_rejects_conclusion_before_pass_through_parameter_origin_is_traced():
    state, conclusion = setup(full=False)

    result = verify_conclusion(conclusion, state)

    assert not result["passed"]
    assert any(
        "forwards parameter 'var' unchanged" in reason
        for reason in result["reasons"]
    )


def test_requires_reconciliation_with_verified_state_modifier():
    state, conclusion = setup(full=True)

    result = verify_conclusion(conclusion, state)

    assert not result["passed"]
    assert any(
        "CResourceOwner::q3" in reason and "changes parameter 'var'" in reason
        for reason in result["reasons"]
    )


def test_allows_conclusion_at_verified_state_modifier():
    state, conclusion = setup(full=True)

    conclusion.update({
        "root_cause_function_id": Q3,
        "root_cause_qualified_name": "CResourceOwner::q3",
        "relative_path": "src/file3_owner.cpp",
        "line": 7,
        "explanation": (
            "CResourceOwner::q3 deletes var and sets the shared pointer "
            "to nullptr while the worker uses the same reference."
        ),
    })

    result = verify_conclusion(conclusion, state)

    assert result["passed"], result["reasons"]
