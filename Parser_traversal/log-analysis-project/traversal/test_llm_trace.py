from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from traversal.backend_factory import make_backend_factory
from traversal.budget import BudgetExceeded, BudgetManager, BudgetedBackend
from traversal.llm_backends import LocalOpenAICompatibleBackend
from traversal.llm_trace import LLMTraceRecorder


MODEL = "google/gemini-2.5-flash"
API_KEY = "mock-openrouter-secret"
BASE_URL = "https://openrouter.ai/api/v1?discard=this-secret"
PRICING = {
    "input_per_million_tokens_usd": 3.3,
    "output_per_million_tokens_usd": 16.5,
}


def response_payload(*, usage: dict | None = None, message: dict | None = None) -> dict:
    payload = {
        "id": "resp_mock_1",
        "object": "chat.completion",
        "model": MODEL,
        "choices": [
            {
                "index": 0,
                "message": message or {"role": "assistant", "content": "Captured reply"},
                "finish_reason": "tool_calls" if message and message.get("tool_calls") else "stop",
            }
        ],
    }
    if usage is not None:
        payload["usage"] = usage
    return payload


def read_records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def make_recorder(tmp_path: Path) -> LLMTraceRecorder:
    return LLMTraceRecorder(
        tmp_path,
        run_id="run_mock",
        provider="openai-compatible",
        model=MODEL,
        base_url=BASE_URL,
        pricing=PRICING,
    )


def make_backend(
    recorder: LLMTraceRecorder,
    handler,
    *,
    stage: str = "investigator",
    tools: list[dict] | None = None,
) -> LocalOpenAICompatibleBackend:
    return LocalOpenAICompatibleBackend(
        model=MODEL,
        canonical_tools=tools or [],
        base_url=BASE_URL,
        api_key=API_KEY,
        max_tokens=128,
        temperature=0.25,
        system_prompt="System prompt exact",
        trace_recorder=recorder,
        stage=stage,
        http_transport=httpx.MockTransport(handler),
    )


def test_success_records_exact_request_response_usage_and_cost(tmp_path):
    actual_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        actual_requests.append(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            headers={"x-request-id": "req_mock_1"},
            json=response_payload(
                usage={"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17},
                message={
                    "role": "assistant",
                    "content": "I will inspect callers.",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "find_callers",
                                "arguments": '{"function_id":"fn_anchor"}',
                            },
                        }
                    ],
                },
            ),
        )

    recorder = make_recorder(tmp_path)
    backend = make_backend(
        recorder,
        handler,
        tools=[
            {
                "name": "find_callers",
                "description": "Find callers",
                "parameters": {"type": "object", "properties": {"function_id": {"type": "string"}}},
            }
        ],
    )
    result = backend.send("Exact user content")
    records = read_records(recorder.trace_path)

    assert len(records) == 1
    record = records[0]
    sent_payload = json.loads(actual_requests[0])
    assert record["request"]["serialized_body"] == actual_requests[0]
    assert record["request"]["payload"] == sent_payload
    assert record["request"]["payload"]["messages"] == [
        {"role": "system", "content": "System prompt exact"},
        {"role": "user", "content": "Exact user content"},
    ]
    assert record["request"]["payload"]["tools"] == sent_payload["tools"]
    assert record["provider"] == "openrouter"
    assert record["model"] == MODEL
    assert record["stage"] == "investigator"
    assert record["request_id"] == "req_mock_1"
    assert record["response"]["provider_response_id"] == "resp_mock_1"
    assert record["response"]["finish_reason"] == "tool_calls"
    assert record["response"]["assistant_message"]["tool_calls"][0]["function"]["name"] == "find_callers"
    assert result.text == "I will inspect callers."
    assert result.tool_calls[0].name == "find_callers"
    assert result.tool_calls[0].input == {"function_id": "fn_anchor"}
    assert result.usage == {"input_tokens": 12, "output_tokens": 5, "total_tokens": 17}
    assert record["usage"]["provider_reported"] == {
        "input_tokens": 12,
        "output_tokens": 5,
        "total_tokens": 17,
    }
    assert record["estimated_input_tokens"] > 0
    assert record["actual_input_tokens"] == 12
    assert record["actual_output_tokens"] == 5
    assert record["actual_total_tokens"] == 17
    assert record["input_limit"] is None
    assert record["input_limit_exceeded"] is False
    assert record["cost"]["estimated_total_cost_usd"] == pytest.approx(0.0001221)

    summary = recorder.write_summary()
    assert summary["actual_api_calls"] == 1
    assert summary["successful_calls"] == 1
    assert summary["total_input_tokens"] == 12
    assert summary["total_output_tokens"] == 5
    assert summary["provider_reported_total_tokens"] == 17
    assert summary["max_estimated_input_tokens_per_request"] == record["estimated_input_tokens"]
    assert summary["max_actual_input_tokens_per_request"] == 12
    assert summary["average_actual_input_tokens_per_successful_request"] == 12
    assert summary["highest_input_consumption_call_index"] == 1
    assert summary["highest_input_consumption_stage"] == "investigator"
    assert summary["model"] == MODEL
    assert json.loads(recorder.summary_path.read_text(encoding="utf-8"))["actual_api_calls"] == 1


def test_missing_provider_usage_is_unavailable_not_fabricated(tmp_path):
    recorder = make_recorder(tmp_path)
    backend = make_backend(
        recorder,
        lambda request: httpx.Response(200, json=response_payload()),
    )

    result = backend.send("No usage response")
    record = read_records(recorder.trace_path)[0]
    summary = recorder.write_summary()

    assert result.usage == {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    assert record["usage"] == {"source": "unavailable", "provider_reported": None}
    assert record["actual_input_tokens"] is None
    assert record["actual_output_tokens"] is None
    assert record["actual_total_tokens"] is None
    assert record["cost"]["estimated_total_cost_usd"] is None
    assert summary["total_input_tokens"] is None
    assert summary["total_output_tokens"] is None
    assert summary["total_tokens"] is None
    assert summary["usage_unavailable_call_indexes"] == [1]


def test_errors_are_recorded_without_credentials_or_authorization_headers(tmp_path):
    recorder = make_recorder(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {API_KEY}"
        return httpx.Response(
            401,
            headers={"x-request-id": "req_rejected"},
            json={"error": {"message": f"Invalid key {API_KEY}; Bearer another-secret"}},
        )

    backend = make_backend(recorder, handler)
    with pytest.raises(Exception):
        backend.send("Request that receives an API error")

    raw_trace = recorder.trace_path.read_text(encoding="utf-8")
    record = read_records(recorder.trace_path)[0]
    assert API_KEY not in raw_trace
    assert "another-secret" not in raw_trace
    assert "authorization" not in raw_trace.lower()
    assert record["response"]["http_status"] == 401
    assert record["response"]["error"] is not None
    assert record["success"] is False
    assert record["duration_ms"] >= 0
    assert record["base_url"] == "https://openrouter.ai/api/v1"


def test_call_indexes_track_each_http_request(tmp_path):
    recorder = make_recorder(tmp_path)
    backend = make_backend(
        recorder,
        lambda request: httpx.Response(200, json=response_payload()),
    )

    backend.send("First")
    backend.send("Second")

    records = read_records(recorder.trace_path)
    assert [record["call_index"] for record in records] == [1, 2]
    assert [record["request"]["payload"]["messages"][-1]["content"] for record in records] == [
        "First",
        "Second",
    ]


def test_sdk_retry_attempts_are_each_traced_and_budgeted(tmp_path, monkeypatch):
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        if request_count == 1:
            return httpx.Response(500, json={"error": {"message": "temporary failure"}})
        return httpx.Response(200, json=response_payload())

    recorder = make_recorder(tmp_path)
    backend = make_backend(recorder, handler)
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    budget = BudgetManager({
        "max_total_llm_calls": 5,
        "max_investigator_calls": 5,
        "max_total_input_tokens": 1000,
        "max_total_output_tokens": 1000,
        "max_total_tokens": 2000,
        "max_input_tokens_per_request": 500,
        "max_estimated_cost_usd": 10,
        "requests_per_minute": 0,
        "pricing": PRICING,
    })

    result = BudgetedBackend(backend, budget, "investigator").send("Retry me")
    records = read_records(recorder.trace_path)

    assert result.text == "Captured reply"
    assert request_count == 2
    assert [record["call_index"] for record in records] == [1, 2]
    assert [record["success"] for record in records] == [False, True]
    assert recorder.write_summary()["actual_api_calls"] == 2
    assert budget.state.bedrock_calls == 2
    assert budget.state.failed_calls == 1
    assert budget.state.successful_calls == 1


def test_oversized_input_is_traced_without_provider_execution(tmp_path):
    recorder = make_recorder(tmp_path)
    requests = []
    backend = make_backend(
        recorder,
        lambda request: requests.append(request) or httpx.Response(200, json=response_payload()),
        stage="critic",
    )
    budget = BudgetManager({
        "max_total_llm_calls": 5,
        "max_investigator_calls": 5,
        "max_total_input_tokens": 1000,
        "max_total_output_tokens": 1000,
        "max_total_tokens": 2000,
        "max_input_tokens_per_request": 5,
        "max_estimated_cost_usd": 10,
        "requests_per_minute": 0,
        "pricing": PRICING,
    })

    with pytest.raises(BudgetExceeded):
        BudgetedBackend(backend, budget, "critic").send("too much input")

    record = read_records(recorder.trace_path)[0]
    summary = recorder.write_summary()
    assert requests == []
    assert budget.state.bedrock_calls == 0
    assert record["stage"] == "critic"
    assert record["input_limit"] == 5
    assert record["input_limit_exceeded"] is True
    assert record["rejected_before_provider_execution"] is True
    assert record["actual_input_tokens"] is None
    assert summary["actual_api_calls"] == 0
    assert summary["requests_rejected_before_provider_execution"] == 1


def test_factory_labels_investigator_and_critic(monkeypatch):
    stages = []

    def fake_get_backend(*args, **kwargs):
        stages.append(kwargs["stage"])
        return object()

    monkeypatch.setattr("traversal.backend_factory.get_backend", fake_get_backend)
    factory = make_backend_factory({}, trace_recorder=object())

    factory(system_prompt="investigator", tools=[{"name": "tool"}])
    factory(system_prompt="critic", tools=[])

    assert stages == ["investigator", "critic"]


def test_budget_rejection_does_not_create_an_extra_trace_or_count_usage_twice(tmp_path):
    recorder = make_recorder(tmp_path)
    backend = make_backend(
        recorder,
        lambda request: httpx.Response(
            200,
            json=response_payload(
                usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
            ),
        ),
    )
    budget = BudgetManager(
        {
            "max_bedrock_calls": 1,
            "max_total_input_tokens": 100,
            "max_total_output_tokens": 100,
            "max_total_tokens": 100,
            "max_estimated_cost_usd": 1,
            "pricing": PRICING,
        }
    )
    wrapped = BudgetedBackend(backend, budget, "investigator")

    response = wrapped.send("Count exactly once")
    with pytest.raises(BudgetExceeded):
        wrapped.send("Rejected before API call")

    assert response.text == "Captured reply"
    assert budget.state.bedrock_calls == 1
    assert budget.state.input_tokens == 3
    assert budget.state.output_tokens == 2
    records = read_records(recorder.trace_path)
    assert len(records) == 2
    assert sum(record.get("actual_api_call", True) for record in records) == 1
    assert records[1]["rejected_before_provider_execution"] is True
    assert records[1]["actual_input_tokens"] is None


def test_trace_artifacts_are_private_and_result_copy_keeps_legacy_shape(tmp_path):
    recorder = make_recorder(tmp_path / "run")
    result = {"status": "ACCEPTED", "execution_metrics": {"bedrock_calls": 1}}

    recorder.write_result_copy(result)
    recorder.write_summary()

    assert json.loads((tmp_path / "run" / "rca_result.json").read_text()) == result
    assert (tmp_path / "run" / "llm_trace.jsonl").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "run" / "llm_trace_summary.json").stat().st_mode & 0o777 == 0o600