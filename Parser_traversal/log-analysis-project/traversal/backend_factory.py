from __future__ import annotations

from traversal.budget import BudgetedBackend
from traversal.llm_backends import get_backend


def make_backend_factory(llm_config: dict, budget_manager=None, trace_recorder=None):
    def factory(*, system_prompt: str, tools: list[dict]):
        role = "investigator" if tools else "critic"
        backend = get_backend(
            tools,
            system_prompt=system_prompt,
            config=llm_config,
            trace_recorder=trace_recorder,
            stage=role,
        )
        if budget_manager is None:
            return backend
        return BudgetedBackend(backend, budget_manager, role)

    return factory
