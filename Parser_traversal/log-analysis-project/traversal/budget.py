from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any


class BudgetExceeded(RuntimeError):
    def __init__(self, reason: str, diagnostic: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.diagnostic = diagnostic or {"reason": reason}


@dataclass
class BudgetState:
    # Legacy field name retained; counts actual request attempts.
    bedrock_calls: int = 0
    investigator_calls: int = 0
    critic_calls: int = 0
    successful_calls: int = 0
    failed_calls: int = 0
    rejected_input_requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0


class BudgetManager:
    def __init__(self, config: dict[str, Any]):
        pricing = config.get("pricing", {})
        self.max_calls = int(config.get("max_total_llm_calls", config.get("max_bedrock_calls", 50)))
        self.max_investigator_calls = int(config.get("max_investigator_calls", self.max_calls))
        self.max_input = int(config.get("max_total_input_tokens", 100_000))
        self.max_input_per_request = int(config.get("max_input_tokens_per_request", 16_000))
        self.max_output = int(config.get("max_total_output_tokens", 15_000))
        self.max_total = int(config.get("max_total_tokens", 115_000))
        self.max_cost = float(config.get("max_estimated_cost_usd", 0.35))
        self.limits = {
            "max_total_llm_calls": self.max_calls,
            "max_investigator_calls": self.max_investigator_calls,
            "max_total_input_tokens": self.max_input,
            "max_input_tokens_per_request": self.max_input_per_request,
            "max_total_output_tokens": self.max_output,
            "max_total_tokens": self.max_total,
            "max_estimated_cost_usd": self.max_cost,
            "requests_per_minute": int(config.get("requests_per_minute", 0) or 0),
        }
        self.input_rate = float(pricing.get("input_per_million_tokens_usd", 3.30))
        self.output_rate = float(pricing.get("output_per_million_tokens_usd", 16.50))
        rpm = int(config.get("requests_per_minute", 0) or 0)
        self.min_interval = 60.0 / rpm if rpm > 0 else 0.0
        self._rate_lock = threading.Lock()
        self._last_request_started = 0.0
        self.state = BudgetState()

    def before_call(self) -> None:
        self.reserve_attempt("investigator")

    def reserve_attempt(self, role: str) -> None:
        self._check_usage()
        if self.state.bedrock_calls >= self.max_calls:
            self._reject("MAX_TOTAL_LLM_CALLS", "max_total_llm_calls")
        if role != "critic" and self.state.investigator_calls >= self.max_investigator_calls:
            self._reject("MAX_INVESTIGATOR_CALLS", "max_investigator_calls")
        with self._rate_lock:
            if self.min_interval > 0 and self._last_request_started > 0:
                remaining = self.min_interval - (time.monotonic() - self._last_request_started)
                if remaining > 0:
                    time.sleep(remaining)
            self._last_request_started = time.monotonic()
        self.state.bedrock_calls += 1
        if role == "critic":
            self.state.critic_calls += 1
        else:
            self.state.investigator_calls += 1

    def finish_attempt(self, success: bool) -> None:
        if success:
            self.state.successful_calls += 1
        else:
            self.state.failed_calls += 1

    def output_allowance(self, requested: int, estimated_input: int = 0) -> int:
        remaining_output = self.max_output - self.state.output_tokens
        remaining_total = self.max_total - self.state.total_tokens - estimated_input
        allowance = min(requested, remaining_output, remaining_total)
        if allowance <= 0:
            if remaining_output <= 0:
                self._reject("OUTPUT_BUDGET_EXHAUSTED", "max_total_output_tokens", requested_output_tokens=requested)
            self._reject("OUTPUT_BUDGET_EXHAUSTED", "max_total_tokens", requested_output_tokens=requested, estimated_request_input=estimated_input)
        return allowance

    def check_estimated_input(self, estimated_tokens: int, *, check_request_limit: bool = True) -> None:
        if check_request_limit and estimated_tokens > self.max_input_per_request:
            self.state.rejected_input_requests += 1
            self._reject(
                "INPUT_REQUEST_BUDGET_EXCEEDED",
                "max_input_tokens_per_request",
                estimated_request_input=estimated_tokens,
                input_limit_exceeded=True,
                rejected_before_provider_execution=True,
            )
        if estimated_tokens > self.max_input - self.state.input_tokens:
            self._reject("ESTIMATED_INPUT_BUDGET_EXHAUSTED", "max_total_input_tokens", estimated_request_input=estimated_tokens)
        if estimated_tokens >= self.max_total - self.state.total_tokens:
            self._reject("ESTIMATED_TOTAL_BUDGET_EXHAUSTED", "max_total_tokens", estimated_request_input=estimated_tokens)

    def preflight_request(self, estimated_tokens: int, role: str) -> None:
        try:
            self.check_estimated_input(estimated_tokens)
        except BudgetExceeded as error:
            error.diagnostic.update({
                "stage": role,
                "estimated_input_tokens": estimated_tokens,
                "input_limit": self.max_input_per_request,
            })
            raise

    def after_call(self, usage: dict[str, int], role: str) -> None:
        call_input = int(usage.get("input_tokens", 0))
        call_output = int(usage.get("output_tokens", 0))
        call_total = int(usage.get("total_tokens", call_input + call_output))
        self.state.input_tokens += call_input
        self.state.output_tokens += call_output
        self.state.total_tokens += call_total
        self.state.estimated_cost_usd = round(
            self.state.input_tokens / 1_000_000 * self.input_rate
            + self.state.output_tokens / 1_000_000 * self.output_rate, 6
        )
        self._check_usage()

    def _check_usage(self) -> None:
        limits = (
            (self.state.input_tokens, self.max_input, "MAX_TOTAL_INPUT_TOKENS"),
            (self.state.output_tokens, self.max_output, "MAX_TOTAL_OUTPUT_TOKENS"),
            (self.state.total_tokens, self.max_total, "MAX_TOTAL_TOKENS"),
            (self.state.estimated_cost_usd, self.max_cost, "MAX_ESTIMATED_COST_USD"),
        )
        for actual, maximum, reason in limits:
            if actual >= maximum:
                category = {
                    "MAX_TOTAL_INPUT_TOKENS": "max_total_input_tokens",
                    "MAX_TOTAL_OUTPUT_TOKENS": "max_total_output_tokens",
                    "MAX_TOTAL_TOKENS": "max_total_tokens",
                    "MAX_ESTIMATED_COST_USD": "max_estimated_cost_usd",
                }[reason]
                self._reject(reason, category)

    def _reject(self, reason: str, category: str, **pending: Any) -> None:
        limit = self.limits[category]
        used = self.to_dict().get({
            "max_total_llm_calls": "bedrock_calls",
            "max_investigator_calls": "investigator_calls",
            "max_total_input_tokens": "input_tokens",
            "max_input_tokens_per_request": "rejected_input_requests",
            "max_total_output_tokens": "output_tokens",
            "max_total_tokens": "total_tokens",
            "max_estimated_cost_usd": "estimated_cost_usd",
        }[category])
        diagnostic = {
            "reason": reason,
            "budget": category,
            "limit": limit,
            "used": used,
            "remaining": max(0, limit - used),
            "investigator_calls": self.state.investigator_calls,
            "critic_calls": self.state.critic_calls,
            "actual_api_attempts": self.state.bedrock_calls,
            "successful_calls": self.state.successful_calls,
            "failed_calls": self.state.failed_calls,
            "rejected_before_provider_execution": category == "max_input_tokens_per_request",
            "rejected_input_requests": self.state.rejected_input_requests,
            "active_limits": dict(self.limits),
            **pending,
        }
        raise BudgetExceeded(reason, diagnostic)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self.state)
        result["total_llm_calls"] = self.state.bedrock_calls
        return result


class BudgetedBackend:
    def __init__(self, backend, manager: BudgetManager, role: str):
        self.backend = backend
        self.manager = manager
        self.role = role
        self._attempt_hook = bool(getattr(backend, "supports_attempt_budget_hook", False))
        if self._attempt_hook:
            backend._budget_before_attempt = lambda: manager.reserve_attempt(role)
            backend._budget_after_attempt = manager.finish_attempt
            backend._budget_before_request = lambda estimate: manager.preflight_request(estimate, role)
            recorder = getattr(backend, "trace_recorder", None)
            if recorder is not None:
                recorder.input_limit = manager.max_input_per_request
                backend._budget_record_rejection = recorder.record_preflight_rejection

    def _prepare_call(self, pending: Any) -> None:
        messages = list(getattr(self.backend, "messages", []) or [])
        candidate = messages + [{"role": "user", "content": pending}]
        estimated_input = estimate_input_tokens(candidate)
        try:
            if self._attempt_hook:
                self.manager.check_estimated_input(estimated_input, check_request_limit=False)
            else:
                self.manager.preflight_request(estimated_input, self.role)
        except BudgetExceeded:
            recorder = getattr(self.backend, "trace_recorder", None)
            if recorder is not None:
                recorder.input_limit = self.manager.max_input_per_request
                recorder.record_preflight_rejection(
                    stage=self.role,
                    model=getattr(self.backend, "model", "unknown"),
                    estimated_input_tokens=estimated_input,
                    input_limit=self.manager.max_input_per_request,
                )
            raise
        requested = int(getattr(self.backend, "max_tokens", 2048))
        self.backend.max_tokens = self.manager.output_allowance(requested, estimated_input)

    def _invoke(self, method, payload):
        original_max_tokens = getattr(self.backend, "max_tokens", None)
        reserved = False
        try:
            self._prepare_call(payload)
            if not self._attempt_hook:
                self.manager.reserve_attempt(self.role)
                reserved = True
            result = method(payload)
            if not self._attempt_hook:
                self.manager.finish_attempt(True)
                reserved = False
            self.manager.after_call(result.usage, self.role)
            return result
        except Exception:
            if reserved:
                self.manager.finish_attempt(False)
            raise
        finally:
            if original_max_tokens is not None:
                self.backend.max_tokens = original_max_tokens

    def send(self, content):
        return self._invoke(self.backend.send, content)

    def send_tool_results(self, results):
        return self._invoke(self.backend.send_tool_results, results)


def estimate_input_tokens(payload: Any) -> int:
    """Conservative dependency-free estimate: serialized UTF-8 bytes / 3, rounded up."""
    serialized = json.dumps(payload, ensure_ascii=False, default=str)
    return math.ceil(len(serialized.encode("utf-8")) / 3)
