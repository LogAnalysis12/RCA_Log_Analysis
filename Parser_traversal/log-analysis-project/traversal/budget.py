from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


class BudgetExceeded(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class BudgetState:
    bedrock_calls: int = 0
    investigator_calls: int = 0
    critic_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0


class BudgetManager:
    def __init__(self, config: dict[str, Any]):
        pricing = config.get("pricing", {})
        self.max_calls = int(config.get("max_bedrock_calls", 10))
        self.max_input = int(config.get("max_total_input_tokens", 100_000))
        self.max_output = int(config.get("max_total_output_tokens", 15_000))
        self.max_total = int(config.get("max_total_tokens", 115_000))
        self.max_cost = float(config.get("max_estimated_cost_usd", 0.35))
        self.input_rate = float(pricing.get("input_per_million_tokens_usd", 3.30))
        self.output_rate = float(pricing.get("output_per_million_tokens_usd", 16.50))
        self.state = BudgetState()

    def before_call(self) -> None:
        if self.state.bedrock_calls >= self.max_calls:
            raise BudgetExceeded("MAX_BEDROCK_CALLS")
        self._check_usage()

    def after_call(self, usage: dict[str, int], role: str) -> None:
        self.state.bedrock_calls += 1
        if role == "critic":
            self.state.critic_calls += 1
        else:
            self.state.investigator_calls += 1

        call_input = int(usage.get("input_tokens", 0))
        call_output = int(usage.get("output_tokens", 0))
        call_total = int(usage.get("total_tokens", call_input + call_output))
        self.state.input_tokens += call_input
        self.state.output_tokens += call_output
        self.state.total_tokens += call_total
        self.state.estimated_cost_usd = round(
            self.state.input_tokens / 1_000_000 * self.input_rate
            + self.state.output_tokens / 1_000_000 * self.output_rate,
            6,
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
                raise BudgetExceeded(reason)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self.state)


class BudgetedBackend:
    def __init__(self, backend, manager: BudgetManager, role: str):
        self.backend = backend
        self.manager = manager
        self.role = role

    def send(self, content):
        self.manager.before_call()
        result = self.backend.send(content)
        self.manager.after_call(result.usage, self.role)
        return result

    def send_tool_results(self, results):
        self.manager.before_call()
        result = self.backend.send_tool_results(results)
        self.manager.after_call(result.usage, self.role)
        return result
