from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from traversal.budget import BudgetExceeded, BudgetManager, BudgetedBackend

def make_limits(**overrides):
    config = {
        "max_total_llm_calls": 2, "max_investigator_calls": 1,
        "max_total_input_tokens": 1000, "max_total_output_tokens": 1000,
        "max_total_tokens": 2000, "max_estimated_cost_usd": 10,
        "requests_per_minute": 0,
        "pricing": {"input_per_million_tokens_usd": 0, "output_per_million_tokens_usd": 0},
    }
    config.update(overrides)
    return config

def test_investigator_limit_is_separate():
    manager = BudgetManager(make_limits())
    manager.reserve_attempt("investigator")
    with pytest.raises(BudgetExceeded, match="MAX_INVESTIGATOR_CALLS"):
        manager.reserve_attempt("investigator")
    assert manager.state.bedrock_calls == 1

def test_total_limit_counts_failed_attempts():
    manager = BudgetManager(make_limits(max_investigator_calls=10))
    manager.reserve_attempt("investigator")
    manager.finish_attempt(False)
    manager.reserve_attempt("critic")
    with pytest.raises(BudgetExceeded, match="MAX_TOTAL_LLM_CALLS"):
        manager.reserve_attempt("critic")
    assert manager.state.bedrock_calls == 2
    assert manager.state.failed_calls == 1

def test_failed_non_hook_backend_counts_attempt():
    manager = BudgetManager(make_limits(max_investigator_calls=10))
    backend = SimpleNamespace(max_tokens=32, messages=[], send=Mock(side_effect=TimeoutError("timeout")), send_tool_results=Mock())
    wrapped = BudgetedBackend(backend, manager, "investigator")
    with pytest.raises(TimeoutError):
        wrapped.send("request")
    assert manager.state.bedrock_calls == 1
    assert manager.state.failed_calls == 1

def test_success_records_usage_once():
    manager = BudgetManager(make_limits(max_investigator_calls=10))
    result = SimpleNamespace(text="ok", usage={"input_tokens": 2, "output_tokens": 1, "total_tokens": 3})
    backend = SimpleNamespace(max_tokens=32, messages=[], send=Mock(return_value=result), send_tool_results=Mock())
    BudgetedBackend(backend, manager, "investigator").send("request")
    assert manager.state.bedrock_calls == 1
    assert manager.state.successful_calls == 1
    assert manager.state.input_tokens == 2
    assert manager.state.output_tokens == 1

def test_input_rejection_diagnostic_has_active_limit_and_pending_size_without_attempt():
    manager = BudgetManager(make_limits(max_total_input_tokens=20))
    manager.state.input_tokens = 18
    backend = SimpleNamespace(max_tokens=32, messages=[], send=Mock(), send_tool_results=Mock())
    wrapped = BudgetedBackend(backend, manager, "investigator")

    with pytest.raises(BudgetExceeded) as caught:
        wrapped.send("request that exceeds the remaining input allowance")

    diagnostic = caught.value.diagnostic
    assert diagnostic["reason"] == "ESTIMATED_INPUT_BUDGET_EXHAUSTED"
    assert diagnostic["budget"] == "max_total_input_tokens"
    assert diagnostic["limit"] == 20
    assert diagnostic["used"] == 18
    assert diagnostic["remaining"] == 2
    assert diagnostic["estimated_request_input"] > 2
    assert diagnostic["active_limits"]["max_total_input_tokens"] == 20
    assert diagnostic["actual_api_attempts"] == 0
    assert backend.send.call_count == 0
    assert manager.state.bedrock_calls == 0

def test_per_request_input_limit_rejects_before_provider_attempt():
    manager = BudgetManager(make_limits(max_input_tokens_per_request=12))
    backend = SimpleNamespace(max_tokens=32, messages=[], send=Mock(), send_tool_results=Mock())

    with pytest.raises(BudgetExceeded) as caught:
        BudgetedBackend(backend, manager, "investigator").send("x" * 80)

    assert caught.value.reason == "INPUT_REQUEST_BUDGET_EXCEEDED"
    assert caught.value.diagnostic["budget"] == "max_input_tokens_per_request"
    assert caught.value.diagnostic["input_limit"] == 12
    assert caught.value.diagnostic["estimated_input_tokens"] > 12
    assert caught.value.diagnostic["rejected_before_provider_execution"] is True
    assert backend.send.call_count == 0
    assert manager.state.bedrock_calls == 0
    assert manager.state.rejected_input_requests == 1

def test_per_request_input_limit_allows_small_payload():
    manager = BudgetManager(make_limits(max_input_tokens_per_request=100))
    result = SimpleNamespace(text="ok", usage={"input_tokens": 2, "output_tokens": 1, "total_tokens": 3})
    backend = SimpleNamespace(max_tokens=32, messages=[], send=Mock(return_value=result), send_tool_results=Mock())

    BudgetedBackend(backend, manager, "investigator").send("small")

    assert backend.send.call_count == 1
    assert manager.state.bedrock_calls == 1

def test_retry_capable_backend_checks_request_limit_after_history_compaction():
    manager = BudgetManager(make_limits(max_input_tokens_per_request=50))
    backend = SimpleNamespace(
        max_tokens=32,
        messages=[{"role": "user", "content": "old " * 100}],
        supports_attempt_budget_hook=True,
        send=Mock(return_value=SimpleNamespace(
            text="ok", usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
        )),
        send_tool_results=Mock(),
    )
    BudgetedBackend(backend, manager, "investigator").send("new")

    assert backend.send.call_count == 1

def test_output_and_total_token_rejections_report_the_relevant_category():
    output_manager = BudgetManager(make_limits(max_total_output_tokens=10))
    output_manager.state.output_tokens = 10
    with pytest.raises(BudgetExceeded) as output_error:
        output_manager.output_allowance(32)
    assert output_error.value.reason == "OUTPUT_BUDGET_EXHAUSTED"
    assert output_error.value.diagnostic["budget"] == "max_total_output_tokens"
    assert output_error.value.diagnostic["limit"] == 10
    assert output_error.value.diagnostic["requested_output_tokens"] == 32

    total_manager = BudgetManager(make_limits(max_total_tokens=10))
    total_manager.state.total_tokens = 8
    with pytest.raises(BudgetExceeded) as total_error:
        total_manager.check_estimated_input(3)
    assert total_error.value.reason == "ESTIMATED_TOTAL_BUDGET_EXHAUSTED"
    assert total_error.value.diagnostic["budget"] == "max_total_tokens"
    assert total_error.value.diagnostic["limit"] == 10
    assert total_error.value.diagnostic["used"] == 8
    assert total_error.value.diagnostic["remaining"] == 2
    assert total_error.value.diagnostic["estimated_request_input"] == 3

def test_call_limit_diagnostic_identifies_configured_call_budget():
    manager = BudgetManager(make_limits(max_total_llm_calls=1, max_investigator_calls=10))
    manager.reserve_attempt("investigator")
    manager.finish_attempt(True)

    with pytest.raises(BudgetExceeded) as caught:
        manager.reserve_attempt("critic")

    diagnostic = caught.value.diagnostic
    assert caught.value.reason == "MAX_TOTAL_LLM_CALLS"
    assert diagnostic["budget"] == "max_total_llm_calls"
    assert diagnostic["limit"] == 1
    assert diagnostic["used"] == 1
    assert diagnostic["remaining"] == 0
    assert diagnostic["investigator_calls"] == 1
    assert diagnostic["critic_calls"] == 0
    assert diagnostic["successful_calls"] == 1
    assert diagnostic["failed_calls"] == 0
    assert diagnostic["actual_api_attempts"] == 1

def test_investigator_call_limit_diagnostic_identifies_role_limit():
    manager = BudgetManager(make_limits(max_investigator_calls=1))
    manager.reserve_attempt("investigator")

    with pytest.raises(BudgetExceeded) as caught:
        manager.reserve_attempt("investigator")

    assert caught.value.reason == "MAX_INVESTIGATOR_CALLS"
    assert caught.value.diagnostic["budget"] == "max_investigator_calls"
    assert caught.value.diagnostic["limit"] == 1
    assert caught.value.diagnostic["used"] == 1
    assert caught.value.diagnostic["remaining"] == 0
    assert caught.value.diagnostic["actual_api_attempts"] == 1

def test_failed_network_attempt_remains_in_diagnostic_accounting():
    manager = BudgetManager(make_limits(max_total_llm_calls=1, max_investigator_calls=10))
    manager.reserve_attempt("investigator")
    manager.finish_attempt(False)

    with pytest.raises(BudgetExceeded) as caught:
        manager.reserve_attempt("critic")

    diagnostic = caught.value.diagnostic
    assert diagnostic["actual_api_attempts"] == 1
    assert diagnostic["successful_calls"] == 0
    assert diagnostic["failed_calls"] == 1
