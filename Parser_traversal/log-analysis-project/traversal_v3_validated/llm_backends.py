"""Provider-neutral LLM backends for the traversal agent.

Primary provider:
    Amazon Bedrock Converse API

Optional future provider:
    Local OpenAI-compatible servers such as Ollama, LM Studio, vLLM,
    llama.cpp server, and text-generation-webui.

The Investigator and Critic should depend only on:
    backend.send(user_content)
    backend.send_tool_results(results)

Environment variables:
    LLM_PROVIDER       bedrock or local (default: bedrock)
    LLM_MODEL          Bedrock model/inference-profile ID or local model name
    AWS_REGION         Bedrock Region (default: us-east-1)
    BEDROCK_MAX_TOKENS Maximum output tokens (default: 2048)
    BEDROCK_TEMPERATURE Temperature (default: 0.0)

Local-only variables:
    LLM_BASE_URL       e.g. http://localhost:11434/v1
    LLM_API_KEY        optional; defaults to not-needed-for-local

No AWS credentials are stored here. Boto3 uses the normal AWS credential chain.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class StepResult:
    tool_calls: list[ToolCall] = field(default_factory=list)
    text: str = ""
    stop_reason: str = ""
    usage: dict[str, int] = field(default_factory=dict)


def _bedrock_tools(canonical_tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "toolSpec": {
                "name": tool["name"],
                "description": tool["description"],
                "inputSchema": {"json": tool["parameters"]},
            }
        }
        for tool in canonical_tools
    ]


def _openai_tools(canonical_tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["parameters"],
            },
        }
        for tool in canonical_tools
    ]


def _normalize_user_content(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"text": content}]
    if isinstance(content, list):
        return content
    return [{"text": json.dumps(content, ensure_ascii=False, default=str)}]


class BedrockBackend:
    """Amazon Bedrock backend using the provider-neutral Converse API."""

    def __init__(
        self,
        model: str,
        canonical_tools: list[dict[str, Any]],
        region: str = "us-east-1",
        max_tokens: int = 2048,
        temperature: float = 0.0,
        system_prompt: str | None = None,
    ) -> None:
        import boto3
        from botocore.config import Config

        self.client = boto3.client(
            "bedrock-runtime",
            region_name=region,
            config=Config(
                connect_timeout=20,
                read_timeout=180,
                retries={"max_attempts": 3, "mode": "adaptive"},
            ),
        )
        self.model = model
        self.tools = _bedrock_tools(canonical_tools)
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.system = [{"text": system_prompt}] if system_prompt else None
        self.messages: list[dict[str, Any]] = []

    def send(self, user_content: Any) -> StepResult:
        self.messages.append(
            {"role": "user", "content": _normalize_user_content(user_content)}
        )
        return self._call()

    def send_tool_results(self, results: list[dict[str, Any]]) -> StepResult:
        content: list[dict[str, Any]] = []
        for result in results:
            tool_use_id = result.get("tool_use_id") or result.get("tool_call_id")
            if not tool_use_id:
                raise ValueError("Each tool result must contain tool_use_id.")
            raw_content = result.get("content", "")
            if isinstance(raw_content, str):
                result_content = [{"text": raw_content}]
            else:
                result_content = [
                    {
                        "json": raw_content
                        if isinstance(raw_content, (dict, list))
                        else {"value": raw_content}
                    }
                ]
            block: dict[str, Any] = {
                "toolResult": {
                    "toolUseId": tool_use_id,
                    "content": result_content,
                    "status": "error" if result.get("is_error") else "success",
                }
            }
            content.append(block)
        self.messages.append({"role": "user", "content": content})
        return self._call()

    def _call(self) -> StepResult:
        request: dict[str, Any] = {
            "modelId": self.model,
            "messages": self.messages,
            "inferenceConfig": {
                "maxTokens": self.max_tokens,
                "temperature": self.temperature,
            },
        }
        if self.tools:
            request["toolConfig"] = {"tools": self.tools}
        if self.system:
            request["system"] = self.system

        response = self.client.converse(**request)
        assistant_message = response["output"]["message"]
        self.messages.append(assistant_message)

        tool_calls: list[ToolCall] = []
        text_parts: list[str] = []
        for block in assistant_message.get("content", []):
            if "text" in block:
                text_parts.append(block["text"])
            if "toolUse" in block:
                tool_use = block["toolUse"]
                tool_calls.append(
                    ToolCall(
                        id=tool_use["toolUseId"],
                        name=tool_use["name"],
                        input=tool_use.get("input", {}),
                    )
                )

        raw_usage = response.get("usage", {})
        usage = {
            "input_tokens": int(raw_usage.get("inputTokens", 0)),
            "output_tokens": int(raw_usage.get("outputTokens", 0)),
            "total_tokens": int(raw_usage.get("totalTokens", 0)),
        }
        return StepResult(
            tool_calls=tool_calls,
            text="".join(text_parts),
            stop_reason=response.get("stopReason", ""),
            usage=usage,
        )


class LocalOpenAICompatibleBackend:
    """Optional local backend for any OpenAI-compatible chat server."""

    def __init__(
        self,
        model: str,
        canonical_tools: list[dict[str, Any]],
        base_url: str,
        api_key: str | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.0,
        system_prompt: str | None = None,
    ) -> None:
        import openai

        if not base_url:
            raise ValueError("LLM_BASE_URL is required when LLM_PROVIDER=local.")
        self.client = openai.OpenAI(
            api_key=api_key or "not-needed-for-local",
            base_url=base_url,
        )
        self.model = model
        self.tools = _openai_tools(canonical_tools)
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.messages: list[dict[str, Any]] = []
        if system_prompt:
            self.messages.append({"role": "system", "content": system_prompt})

    def send(self, user_content: Any) -> StepResult:
        content = (
            user_content
            if isinstance(user_content, str)
            else json.dumps(user_content, ensure_ascii=False, default=str)
        )
        self.messages.append({"role": "user", "content": content})
        return self._call()

    def send_tool_results(self, results: list[dict[str, Any]]) -> StepResult:
        for result in results:
            tool_use_id = result.get("tool_use_id") or result.get("tool_call_id")
            if not tool_use_id:
                raise ValueError("Each tool result must contain tool_use_id.")
            content = result.get("content", "")
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False, default=str)
            self.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_use_id,
                    "content": content,
                }
            )
        return self._call()

    def _call(self) -> StepResult:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=self.messages,
            tools=self.tools or None,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
        )
        message = response.choices[0].message
        self.messages.append(message.model_dump(exclude_none=True))

        tool_calls: list[ToolCall] = []
        for tool_call in message.tool_calls or []:
            try:
                arguments = json.loads(tool_call.function.arguments or "{}")
            except json.JSONDecodeError:
                arguments = {"_invalid_json": tool_call.function.arguments}
            tool_calls.append(
                ToolCall(
                    id=tool_call.id or str(uuid.uuid4()),
                    name=tool_call.function.name,
                    input=arguments,
                )
            )

        prompt_tokens = int(getattr(response.usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(response.usage, "completion_tokens", 0) or 0)
        return StepResult(
            tool_calls=tool_calls,
            text=message.content or "",
            stop_reason=response.choices[0].finish_reason or "",
            usage={
                "input_tokens": prompt_tokens,
                "output_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        )


def get_backend(
    canonical_tools: list[dict[str, Any]],
    *,
    system_prompt: str | None = None,
    config: dict[str, Any] | None = None,
):
    """Build the configured backend while keeping callers provider-neutral."""

    config = config or {}
    provider = str(
        config.get("provider") or os.getenv("LLM_PROVIDER", "bedrock")
    ).strip().lower()
    model = str(config.get("model") or os.getenv("LLM_MODEL", "")).strip()
    if not model:
        raise ValueError("Set llm.model in config.json or set LLM_MODEL.")

    max_tokens = int(
        config.get("max_tokens")
        or os.getenv("BEDROCK_MAX_TOKENS", "2048")
    )
    temperature = float(
        config.get("temperature")
        if config.get("temperature") is not None
        else os.getenv("BEDROCK_TEMPERATURE", "0.0")
    )

    if provider == "bedrock":
        region = str(
            config.get("region") or os.getenv("AWS_REGION", "us-east-1")
        )
        return BedrockBackend(
            model=model,
            canonical_tools=canonical_tools,
            region=region,
            max_tokens=max_tokens,
            temperature=temperature,
            system_prompt=system_prompt,
        )

    if provider in {"local", "openai-compatible"}:
        base_url = str(
            config.get("base_url") or os.getenv("LLM_BASE_URL", "")
        ).strip()
        api_key = config.get("api_key") or os.getenv("LLM_API_KEY")
        return LocalOpenAICompatibleBackend(
            model=model,
            canonical_tools=canonical_tools,
            base_url=base_url,
            api_key=api_key,
            max_tokens=max_tokens,
            temperature=temperature,
            system_prompt=system_prompt,
        )

    raise ValueError(
        f"Unknown LLM provider {provider!r}. Expected 'bedrock' or 'local'."
    )
