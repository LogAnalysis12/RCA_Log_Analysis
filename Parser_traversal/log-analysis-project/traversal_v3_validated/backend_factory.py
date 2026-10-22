from __future__ import annotations
from traversal.llm_backends import get_backend

def make_backend_factory(llm_config: dict):
    def factory(*, system_prompt: str, tools: list[dict]):
        return get_backend(tools, system_prompt=system_prompt, config=llm_config)
    return factory
